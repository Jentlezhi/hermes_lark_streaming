"""编排层 — 串联领域、渲染与传输.

本层是唯一同时依赖 L2/L3/L4 的地方，core/ 保持纯领域逻辑不被污染。

**线程模型**：Hermes 的回调来自两类线程——Agent worker 线程（同步回调）
与 gateway 事件循环（异步路径）。本层对外暴露**同步**接口供回调直接调用，
内部把所有飞书 API 操作调度到事件循环执行。同步接口只做「写内存 + 触发调度」，
绝不阻塞 worker 线程。

**失败哲学**：任何一步失败都不得阻断 Hermes。失败时把 turn 标记为 FALLBACK，
让 Hermes 走原生输出——宁可用户看到朴素文本，也不能丢消息。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__, render
from .config import Config, hermes_home
from .core import (
    REASON_EVICTED,
    REASON_EXPIRED,
    REASON_TIMEOUT,
    TIMEOUT_REASONS,
    Delivery,
    InteractionKind,
    InteractionStatus,
    Segment,
    SegmentType,
    Turn,
    TurnRegistry,
    TurnState,
)
from .events import NoticeLevel, strip_reasoning_tags
from .observability import METRICS, log_turn, logger, short
from .selfheal import SelfHealer, get_healer, write_activity
from .transport import (
    Action,
    CircuitBreaker,
    ClientConfig,
    FeishuAPIError,
    FeishuClient,
    FlushScheduler,
    MessageGuard,
    classify,
)

#: 等待卡片创建完成的上限。超时即判定建卡失败，交还 Hermes 原生输出
CARD_CREATE_TIMEOUT_SEC = 10.0
#: 终态收卡的重试次数
FINALIZE_ATTEMPTS = 3
#: 空闲守护的扫描间隔
WATCH_INTERVAL_SEC = 15.0
#: 同时降级的能力数达此值即判定为系统性故障，升级为全局熔断。
#: 单类失效是回调签名/语义问题，多类同时失效只可能是凭据、网络或权限。
SYSTEMIC_DEGRADE_COUNT = 3
#: 收纳被拒的统一描述，写入自愈层的失败记录。没有异常对象可用时用它，
#: 让 ``doctor`` 报告里的「最后错误」仍然是可读的因果说明。
#: 定义在本模块而非 bridge/：语义上它描述的是「编排层拒绝了这次收纳」，
#: 且 bridge 的两个模块都 import 本模块，放这里可避免字面量重复。
PUSH_REJECTED = "卡片拒绝收纳（turn 已终态或渲染失败）"


class _TurnRuntime:
    """turn 的运行时附属对象（调度器、守卫、建卡任务、图片缓存）.

    与 Turn 分开存放：Turn 是纯领域状态（可单测），运行时对象带 IO 依赖。
    """

    __slots__ = ("create_task", "guard", "images", "scheduler")

    def __init__(self, *, scheduler: FlushScheduler, guard: MessageGuard) -> None:
        self.scheduler = scheduler
        self.guard = guard
        self.images: dict[str, str] = {}
        # 建卡是异步的，而短对话可能在建卡完成前就抵达终态。
        # 保留任务引用，收卡前先等它落地，否则会误判为「没有卡片」而退回原生文本。
        self.create_task: Any = None


class _IdleWatcher:
    """空闲守护 — 保证卡片不会永久停在「处理中」.

    这是与 Hermes 完全解耦的安全网：不读 Hermes 任何符号，只看 turn 自身的
    最后更新时间。即使所有织入都失效，它仍能让卡片走到终态，用户不会看到
    一张永远转圈的卡片。
    """

    __slots__ = ("_orch", "_stop", "_task")

    def __init__(self, orch: Orchestrator) -> None:
        self._orch = orch
        self._task: asyncio.Task[None] | None = None
        self._stop = False

    def start(self, loop: asyncio.AbstractEventLoop) -> bool:
        if self._task is not None:
            return True
        try:
            self._task = loop.create_task(self._run())
        except Exception:
            logger.debug("空闲守护启动失败", exc_info=True)
            return False
        logger.info(
            "空闲守护已启动（扫描间隔 %.0fs，空闲阈值 %ds）",
            WATCH_INTERVAL_SEC,
            self._orch.config.idle_finalize_sec,
        )
        return True

    def stop(self) -> None:
        self._stop = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._task = None

    async def _run(self) -> None:
        while not self._stop:
            try:
                await asyncio.sleep(WATCH_INTERVAL_SEC)
                await self._sweep()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.debug("空闲守护扫描异常", exc_info=True)

    async def _sweep(self) -> None:
        now = time.time()
        self._orch.registry.prune()
        # prune 可能摘掉尚未终态的 turn（典型是审批长期无人处理）。它们的卡片
        # 还在转圈，必须收尾——只丢内存对象就是 HLS 那个线上故障的形状
        self._orch.drain_pending_finalize()
        # 每轮扫描都落一份活动心跳：gateway 内存里的活跃 turn，独立的 CLI
        # 进程无从得知，只能靠落盘传递（见 Orchestrator.publish_activity）
        self._orch.publish_activity()

        idle_limit = self._orch.config.idle_finalize_sec
        for turn in self._orch.registry.active_turns():
            # 尚未建卡的不管；等待用户确认的也不管——审批本来就可能等很久
            if turn.state in (TurnState.IDLE, TurnState.CREATING, TurnState.WAITING):
                continue
            if now - turn.updated_at < idle_limit:
                continue
            # 有工具还在执行：这不是卡死，是在干活。工具执行期间 Hermes 不产生
            # 任何回调，updated_at 一动不动，跟真卡死在时间上完全同形——不看这
            # 一眼，一次跑几分钟的编译或测试就会被判成超时。工具事件真丢了的
            # 极端情况由注册表的 turn_ttl_sec 兜底（prune 就在本方法开头）
            if turn.has_running_tool:
                continue

            logger.warning(
                "turn 空闲超过 %ds 且无工具在执行，强制收卡: turn=%s state=%s",
                idle_limit,
                turn.turn_key[:12],
                turn.state.value,
            )
            METRICS.incr("turn.idle_finalized")
            try:
                # 走中断语义而非 complete：真相是「一直没动静」，不是「完成了」。
                # 渲染成 ✅ 已完成会让用户以为任务成功，比不收卡更误导
                await self._orch.abort_turn(turn.turn_key, reason=REASON_TIMEOUT)
            except Exception:
                logger.debug("空闲收卡失败: turn=%s", turn.turn_key[:12], exc_info=True)


class Orchestrator:
    """流式卡片编排器（每个 Hermes profile 一个实例）."""

    def __init__(self, profile_home: Path | None = None) -> None:
        self._home = (profile_home or hermes_home()).resolve()
        self._cfg = Config(self._home)
        self._registry = TurnRegistry(self._cfg)
        self._client: FeishuClient | None = None
        self._client_lock = threading.Lock()
        # 附加 bot 的 client 缓存（bot_id -> client）。未配置多 bot 时恒为空
        self._clients: dict[str, FeishuClient] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runtimes: dict[str, _TurnRuntime] = {}
        self._runtime_lock = threading.Lock()
        self._breaker = CircuitBreaker(self._cfg.bypass_after_failures)
        self._healer = get_healer(
            self._home,
            __version__,
            enabled=self._safe_selfheal_enabled(),
            degrade_threshold=self._cfg.degrade_after_failures,
            probe_interval=self._cfg.selfheal_probe_interval,
        )
        self._watcher: _IdleWatcher | None = None
        self._watcher_lock = threading.Lock()
        # 订阅额度查询器，由 bridge 在启动时注入（见 set_usage_provider）
        self._usage_provider: Any = None
        # 适配器织入实况查询器，同样由 bridge 注入（见 set_weave_reporter）
        self._weave_reporter: Any = None

    def _safe_selfheal_enabled(self) -> bool:
        """读自愈开关。配置损坏时按开启处理——自愈层本身是旁路的，
        它的失败不会传导到卡片链路，因此默认值取「开」比「关」更有用。
        """
        try:
            return self._cfg.selfheal_enabled
        except Exception:
            logger.debug("读取自愈配置失败，按默认开启处理", exc_info=True)
            return True

    # ── 基础设施 ──────────────────────────────────────────────────

    @property
    def config(self) -> Config:
        return self._cfg

    @property
    def registry(self) -> TurnRegistry:
        return self._registry

    @property
    def enabled(self) -> bool:
        """插件是否应当工作.

        熔断打开后一律返回 False，让所有消息回到 Hermes 原生路径。
        """
        if self._breaker.is_open:
            return False
        try:
            return self._cfg.enabled and self._cfg.has_credentials
        except Exception:
            logger.debug("读取配置失败", exc_info=True)
            return False

    def remember_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """记录事件循环引用，供 worker 线程跨线程调度使用."""
        if loop is not None:
            self._loop = loop
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        try:
            loop = asyncio.get_running_loop()
            if self._loop is not loop:
                self._loop = loop
            self._ensure_watcher(loop)
            return loop
        except RuntimeError:
            pass
        loop = self._loop
        if loop is not None and not loop.is_closed():
            return loop
        return None

    def ensure_watcher(self, loop: asyncio.AbstractEventLoop | None = None) -> bool:
        """确保空闲守护已启动.

        守护需要事件循环，而插件加载可能早于循环启动，因此除了启动时尝试一次，
        每次拿到循环时也会补启动。
        """
        target = loop or self._get_loop()
        if target is None:
            return False
        self._ensure_watcher(target)
        return self._watcher is not None

    def _ensure_watcher(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._watcher is not None:
            return
        with self._watcher_lock:
            if self._watcher is not None:
                return
            watcher = _IdleWatcher(self)
            if watcher.start(loop):
                self._watcher = watcher

    def _ensure_client(self, chat_id: str | None = None) -> FeishuClient:
        """取发卡用的客户端；``chat_id`` 命中绑定时用对应 bot 的凭据.

        多 bot 是 HFC ``BotRegistry`` 里值得拿过来的设计：同一个 Hermes 可能
        服务多个飞书应用，卡片必须由该会话所属的应用发出，否则用户看到的是
        另一个机器人在说话。

        未配置 ``feishu.bots`` 或该会话无绑定时走默认单套凭据——与不带这个
        特性时完全一致，连 client 实例都是同一个。
        """
        bot_id = self._cfg.chat_bindings.get(chat_id, "") if chat_id else ""
        # 查表放在锁外：锁内再调 _ensure_client 会自锁（Lock 不可重入）
        bot = self._cfg.bots.get(bot_id) if bot_id else None
        if bot is not None:
            with self._client_lock:
                existing = self._clients.get(bot_id)
                if existing is not None:
                    return existing
                client = FeishuClient(
                    ClientConfig(
                        app_id=bot["app_id"], app_secret=bot["app_secret"], base_url=bot["base_url"]
                    )
                )
                self._clients[bot_id] = client
                return client

        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            app_id = self._cfg.app_id or self._cfg.env_app_id
            app_secret = self._cfg.app_secret or self._cfg.env_app_secret
            if not app_id or not app_secret:
                raise RuntimeError("飞书凭据未配置")
            self._client = FeishuClient(
                ClientConfig(app_id=app_id, app_secret=app_secret, base_url=self._cfg.base_url)
            )
            return self._client

    def is_native_chat(self, chat_id: str | None) -> bool:
        """该会话是否配置为完全不接管（走 Hermes 原生输出）."""
        if not chat_id:
            return False
        try:
            return chat_id in self._cfg.native_chats
        except Exception:
            logger.debug("读取 native_chats 失败，按接管处理", exc_info=True)
            return False

    def _runtime(self, turn_key: str) -> _TurnRuntime | None:
        with self._runtime_lock:
            return self._runtimes.get(turn_key)

    def _drop_runtime(self, turn_key: str) -> None:
        with self._runtime_lock:
            runtime = self._runtimes.pop(turn_key, None)
        if runtime is not None:
            runtime.scheduler.close()

    def spawn(self, coro: Any) -> Any:
        """把协程投递到事件循环执行（跨线程安全）.

        供 L0 桥接层在 Agent worker 线程中调度异步收卡等操作。
        返回可等待句柄：同线程为 ``asyncio.Task``，跨线程为
        ``concurrent.futures.Future``，两者都能被 :meth:`_await_task` 消化。
        """
        return self._spawn(coro)

    def _spawn(self, coro: Any) -> Any:
        loop = self._get_loop()
        if loop is None:
            coro.close()
            return None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            task = loop.create_task(coro)
            task.add_done_callback(self._on_task_done)
            return task
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            logger.debug("跨线程调度失败", exc_info=True)
            return None
        future.add_done_callback(self._on_task_done)
        return future

    @staticmethod
    async def _await_task(task: Any, timeout: float) -> bool:
        """等待 spawn 返回的句柄完成，返回是否在超时内完成.

        统一处理两种句柄类型：``asyncio.Task`` 直接 await，
        ``concurrent.futures.Future`` 先用 ``wrap_future`` 转换。
        用 shield 包裹，超时只放弃等待、不取消底层任务——建卡中途被取消
        会留下一张永远不会被更新的空卡。
        """
        if task is None:
            return True
        try:
            if isinstance(task, asyncio.Future):
                awaitable: Any = task
            else:
                awaitable = asyncio.wrap_future(task)
            if awaitable.done():
                return True
            await asyncio.wait_for(asyncio.shield(awaitable), timeout=timeout)
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return False
        except Exception:
            logger.debug("等待任务失败", exc_info=True)
            return True

    async def _await_card_creation(self, turn: Turn) -> None:
        """收卡前先等建卡落地.

        短对话可能在建卡完成前就抵达终态，不等就会误判「没有卡片」
        而退回原生文本——短回答恰恰是最常见的场景。
        """
        runtime = self._runtime(turn.turn_key)
        if runtime is None or runtime.create_task is None:
            return
        if not await self._await_task(runtime.create_task, CARD_CREATE_TIMEOUT_SEC):
            logger.warning(
                "等待建卡超时（%.0fs），本轮交还 Hermes 原生输出: turn=%s",
                CARD_CREATE_TIMEOUT_SEC,
                turn.turn_key[:12],
            )
            METRICS.incr("card.create_timeout")

    @staticmethod
    def _on_task_done(future: Any) -> None:
        try:
            future.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("后台任务失败", exc_info=True)

    # ── Turn 生命周期 ─────────────────────────────────────────────

    def start_turn(
        self,
        *,
        turn_key: str,
        message_id: str,
        chat_id: str,
        anchor_id: str | None = None,
        session_key: str | None = None,
    ) -> bool:
        """开始一轮运行：建 turn 并异步创建卡片."""
        if not self.enabled or not turn_key or not chat_id:
            return False

        loop = self._get_loop()
        if loop is None:
            logger.warning("无可用事件循环，跳过建卡: turn=%s", turn_key[:12])
            return False

        turn = self._registry.create(
            turn_key=turn_key,
            message_id=message_id,
            chat_id=chat_id,
            anchor_id=anchor_id,
            session_key=session_key,
        )
        # 建 turn 可能触发容量淘汰。被淘汰者若尚未终态，它的卡片还在转圈，
        # 必须收尾。放在 create 之后立刻处理，不等下一次空闲扫描
        self.drain_pending_finalize()
        if turn is None:
            return False

        scheduler = FlushScheduler(lambda: self._flush(turn), loop=loop)
        guard = MessageGuard(
            anchor_id=anchor_id or message_id,
            card_msg_id_getter=lambda: turn.card_msg_id,
            on_terminate=turn.mark_fallback,
        )
        with self._runtime_lock:
            self._runtimes[turn_key] = _TurnRuntime(scheduler=scheduler, guard=guard)

        task = self._spawn(self._create_card(turn))
        runtime = self._runtime(turn_key)
        if runtime is not None:
            runtime.create_task = task
        return True

    async def _create_card(self, turn: Turn) -> None:
        """创建流式占位卡并挂到用户消息上."""
        if not turn.transition(TurnState.CREATING):
            return

        runtime = self._runtime(turn.turn_key)
        if runtime is None:
            return

        try:
            client = self._ensure_client(turn.chat_id)
            card = render.build_streaming_card(
                header_enabled=self._cfg.header_enabled,
                width_mode=self._cfg.width_mode,
                summary=turn.summary_text(self._cfg),
            )
            card_id = await client.create_card(card)
            anchor = turn.anchor_id or turn.message_id
            try:
                card_msg_id = await client.reply_with_card(anchor, card_id)
            except FeishuAPIError:
                # 回复失败（如原消息已删）时退回直接发到会话，保证内容能送达
                card_msg_id = await client.send_card(turn.chat_id, {"type": "card", "data": {"card_id": card_id}})

            turn.bind_card(card_id=card_id, card_msg_id=card_msg_id)
            turn.element_count = 1  # loading 占位元素
            runtime.scheduler.set_ready(True)
            turn.transition(TurnState.STREAMING)

            METRICS.incr("card.created")
            log_turn(20, turn.turn_key, "卡片已创建 card=%s", card_id[:12])

            # 建卡期间可能已积累内容，立即刷一次
            if turn.segment_state.has_dirty:
                runtime.scheduler.schedule()
        except Exception as error:
            METRICS.record_error("create_card", error)
            logger.warning("建卡失败，交还 Hermes 原生输出: turn=%s", turn.turn_key[:12], exc_info=True)
            turn.mark_fallback()
            runtime.scheduler.close()

    # ── 内容写入（同步接口，供 Hermes 回调直接调用）────────────────

    def push_answer(self, turn_key: str, text: str) -> bool:
        turn = self._active(turn_key)
        if turn is None:
            return False
        cleaned = strip_reasoning_tags(text)
        if not cleaned:
            return False
        if not turn.add_answer(cleaned):
            return False
        self._schedule(turn)
        return True

    def push_reasoning(self, turn_key: str, text: str) -> bool:
        turn = self._active(turn_key)
        if turn is None or not text:
            return False
        if not self._cfg.show_reasoning:
            # 未开启推理展示时，仍返回 True 表示已接管，避免 Hermes 另发一条
            return turn.state.accepts_content
        if not turn.add_reasoning(text):
            return False
        self._schedule(turn)
        return True

    def push_tool_start(self, turn_key: str, name: str, detail: str = "") -> bool:
        turn = self._active(turn_key)
        if turn is None:
            return False
        if not turn.add_tool_start(name, detail):
            return False
        self._schedule(turn)
        return True

    def push_tool_end(self, turn_key: str, name: str, *, error: str = "", output: str = "") -> bool:
        turn = self._active(turn_key)
        if turn is None:
            return False
        if not turn.add_tool_end(name, error=error, output=output):
            return False
        self._schedule(turn)
        return True

    def push_notice(
        self,
        turn_key: str,
        text: str,
        level: NoticeLevel = NoticeLevel.INFO,
        *,
        as_review: bool = False,
    ) -> bool:
        """收纳游离消息 —— 治理「审批/review/工作轮消息跑到卡片外」的核心入口."""
        turn = self._active(turn_key)
        if turn is None:
            return False
        if not turn.add_notice(text, level, as_review=as_review):
            return False
        self._schedule(turn)
        METRICS.incr("notice.captured_review" if as_review else "notice.captured")
        return True

    def open_interaction(self, turn_key: str, kind: InteractionKind, title: str, detail: str = "") -> bool:
        turn = self._active(turn_key)
        if turn is None:
            return False
        if not turn.open_interaction(kind, title, detail):
            return False
        # 进入等待态前强制刷一次，让用户立刻看到「等待确认」
        self._schedule(turn, force=True)
        METRICS.incr(f"interaction.{kind.value}.opened")
        return True

    def close_interaction(
        self,
        turn_key: str,
        kind: InteractionKind,
        *,
        status: InteractionStatus = InteractionStatus.RESOLVED,
        result: str = "",
    ) -> bool:
        turn = self._registry.get(turn_key)
        if turn is None:
            return False
        changed = turn.close_interaction(kind, status, result)
        if changed:
            self._schedule(turn)
            METRICS.incr(f"interaction.{kind.value}.closed")
        return changed

    def _active(self, turn_key: str) -> Turn | None:
        if not self.enabled:
            return None
        turn = self._registry.get_active(turn_key)
        if turn is None:
            return None
        runtime = self._runtime(turn.turn_key)
        if runtime is not None and runtime.guard.should_skip("push"):
            return None
        return turn

    def _schedule(self, turn: Turn, *, force: bool = False) -> None:
        runtime = self._runtime(turn.turn_key)
        if runtime is None:
            return
        if not force and not turn.state.allows_flush:
            # WAITING 态只收内容不刷新，避免审批期间卡片抖动
            return
        runtime.scheduler.schedule()

    # ── 刷新（卡片增量更新的核心）──────────────────────────────────

    async def _flush(self, turn: Turn) -> None:
        """一次幂等刷新.

        两个阶段：先 batch_update 处理结构性变更（新增元素、更新工具面板），
        再对脏的文本段做 stream_element 增量推送。顺序不能反——元素必须先
        存在，才能对它做流式更新。
        """
        runtime = self._runtime(turn.turn_key)
        if runtime is None or turn.state.is_terminal or not turn.card_id:
            return
        if runtime.guard.should_skip("flush"):
            return

        client = self._ensure_client(turn.chat_id)
        all_steps = turn.tools.build_display_steps()
        segments = turn.segment_state.segments

        # ── 阶段一：结构性变更 ──
        actions: list[dict[str, Any]] = []
        pending_new: dict[str, int] = {}
        touched: set[str] = set()
        finalized: set[str] = set()
        pending_total = 0

        for index in range(turn.split_index, len(segments)):
            seg = segments[index]

            if seg.type == SegmentType.TOOL and not self._cfg.show_tool_use:
                seg.created = True
                seg.dirty = False
                continue

            if not seg.created:
                estimated = render.estimate_segment(seg, all_steps)
                if render.exceeds(
                    turn.element_count + pending_total, estimated, self._cfg.element_threshold
                ) and not turn.split_disabled:
                    if await self._split_card(
                        turn, index, actions, pending_new, touched, finalized, client
                    ):
                        actions = []
                        pending_new = {}
                        touched = set()
                        finalized = set()
                        pending_total = 0
                    else:
                        return
                pending_new[seg.el_id] = estimated
                pending_total += estimated
                actions.append(
                    render.add_segment_action(
                        seg,
                        all_steps,
                        text_size=self._cfg.body_text_size,
                        expanded=True,
                    )
                )
                continue

            # 已创建但内容变了
            if seg.type == SegmentType.TOOL and seg.dirty:
                # 工具面板在同一段内持续追加步骤，真实元素数早已不是创建时估的
                # 那个值。这里先按当前步数重算一次，超预算就在步边界截断——不做
                # 这个判断，面板会一路涨到飞书 200 上限之外，而拆卡只在新段创建
                # 时触发，纯工具增长产生不了新段，于是每次 flush 都失败且不自愈
                base = max(0, turn.element_count - seg.element_estimate) + pending_total
                if render.exceeds(
                    base, render.estimate_segment(seg, all_steps), self._cfg.element_threshold
                ) and self._truncate_tool_segment(turn, index, base, all_steps):
                    # 截断插入了新段，而本轮循环的上界在进入时已固定，
                    # 新段留给下一轮；这里主动排一次补刷，不等下个 delta
                    runtime.scheduler.schedule()
                start = seg.tool_offset
                end = render.tool_segment_end(seg, all_steps)
                actions.append(
                    render.tool_update_action(
                        element_id=seg.el_id, steps=all_steps[start:end], expanded=True
                    )
                )
                touched.add(seg.el_id)
            elif seg.is_notice_kind and seg.dirty:
                actions.append(render.notice_update_action(seg))
                touched.add(seg.el_id)
            elif seg.type == SegmentType.INTERACTION and seg.dirty:
                action = render.interaction_update_action(seg)
                if action is not None:
                    actions.append(action)
                    touched.add(seg.el_id)
            elif seg.type == SegmentType.REASONING and seg.elapsed_ms > 0 and not seg.reasoning_finalized:
                actions.append(render.reasoning_finalize_action(seg))
                finalized.add(seg.el_id)

        if actions and not await self._apply_actions(
            turn, client, actions, pending_new, touched, finalized, segments
        ):
            return

        # ── 阶段二：文本增量（打字机）──
        await self._stream_dirty_text(turn, client, segments)

    async def _apply_actions(
        self,
        turn: Turn,
        client: FeishuClient,
        actions: list[dict[str, Any]],
        pending_new: dict[str, int],
        touched: set[str],
        finalized: set[str],
        segments: list[Segment],
    ) -> bool:
        """提交结构性变更，成功后落地本地标记.

        只对**本次实际提交**的元素落标记（``pending_new`` / ``touched`` /
        ``finalized`` 三个集合），不能按类型批量清标——否则拆卡边界之外、
        或本轮未生成 action 的段会被误判为已同步，内容就再也刷不上去了。
        """
        sequence = turn.next_sequence()
        try:
            await client.batch_update(turn.card_id or "", actions, sequence=sequence)
        except Exception as error:
            return self._handle_flush_error(turn, error, segments)

        tool_steps: list[Any] | None = None
        for seg in segments:
            if seg.el_id in pending_new:
                seg.created = True
                seg.element_estimate = pending_new[seg.el_id]
                turn.element_count += seg.element_estimate
                # 新建的文本元素内容尚未推送，标脏等待阶段二
                seg.dirty = bool(seg.is_text_kind and seg.text)
            elif seg.el_id in touched:
                seg.dirty = False
                if seg.type == SegmentType.TOOL:
                    # 工具段是唯一会在「已创建」之后继续长大的段：本次提交的
                    # 步数才是它的真实占用。不在这里对账，element_count 会永远
                    # 停在创建那一刻，预算判断全部失效
                    if tool_steps is None:
                        tool_steps = turn.tools.build_display_steps()
                    actual = render.estimate_segment(seg, tool_steps)
                    turn.element_count = max(0, turn.element_count + actual - seg.element_estimate)
                    seg.element_estimate = actual
            if seg.el_id in finalized:
                seg.reasoning_finalized = True
        return True

    async def _stream_dirty_text(self, turn: Turn, client: FeishuClient, segments: list[Segment]) -> None:
        """把脏的文本段推送出去 —— 这是打字机效果的实际发生处."""
        runtime = self._runtime(turn.turn_key)
        for seg in segments[turn.split_index :]:
            if not seg.created or not seg.dirty or not seg.is_text_kind:
                continue
            try:
                if seg.type == SegmentType.REASONING:
                    content = render.normalize_markdown(seg.text) or " "
                    element_id = seg.text_el_id or seg.el_id
                else:
                    content = seg.text
                    if runtime is not None and runtime.images:
                        content = render.replace_image_refs(content, runtime.images)
                    content = render.downgrade_wide_tables(render.normalize_markdown(content)) or " "
                    element_id = seg.el_id

                await client.stream_element(
                    turn.card_id or "",
                    element_id,
                    content,
                    sequence=turn.next_sequence(),
                )
                seg.dirty = False
            except Exception as error:
                # 单个元素推送失败不影响其他元素，下一轮刷新会重试
                if runtime is not None and runtime.guard.inspect_error("stream_element", error):
                    return
                logger.debug("元素流式更新失败: el=%s", seg.el_id, exc_info=True)

    def _handle_flush_error(self, turn: Turn, error: BaseException, segments: list[Segment]) -> bool:
        """按错误类型决定后续动作，返回是否可继续本次刷新."""
        runtime = self._runtime(turn.turn_key)
        action = classify(error)
        METRICS.incr(f"flush.{action.value}")

        if action == Action.ABORT_PIPELINE:
            if runtime is not None:
                runtime.guard.inspect_error("flush", error)
            return False

        if action == Action.REBUILD_ELEMENT and isinstance(error, FeishuAPIError):
            missing = error.missing_element_id()
            if missing:
                # 元素在卡片上不存在但本地以为已创建：回滚标记，下轮重建
                for seg in segments[turn.split_index :]:
                    if seg.el_id == missing and seg.created:
                        seg.created = False
                        seg.dirty = True
                        turn.element_count = max(0, turn.element_count - seg.element_estimate)
                        logger.info("元素缺失，下轮重建: el=%s", missing)
                        break
            return False

        if action == Action.SPLIT_CARD:
            turn.split_disabled = False
            logger.info("卡片元素超限，下轮触发拆卡: turn=%s", turn.turn_key[:12])
            return False

        if action == Action.RETRY:
            # 交由下一轮刷新自然重试，不在此处阻塞
            return False

        if action == Action.GIVE_UP:
            return False

        METRICS.record_error("flush", error)
        logger.warning("刷新失败: turn=%s", turn.turn_key[:12], exc_info=True)
        return False

    # ── 拆卡 ──────────────────────────────────────────────────────

    def _truncate_tool_segment(
        self,
        turn: Turn,
        index: int,
        base_count: int,
        all_steps: list[Any],
    ) -> bool:
        """把增长到超预算的工具段在步边界截断，溢出的步交给一个新段.

        **为什么必须有这一步**：工具面板是在**同一个段内**持续追加步骤的
        （见 ``SegmentState.on_tool_event``：相邻同类只标脏不新建段），它的元素
        数一直涨，而拆卡的触发点在 ``_flush`` 的「新段创建」分支里。纯工具增长
        产生不了新段，所以那条路永远走不到——面板越过飞书 200 上限后
        ``batch_update`` 会整次失败，且再也不会自愈。

        截断后：原段被钉死在 ``offset``（``tool_end_offset`` 从 0 变成具体值），
        元素数就此固定；新段承接后续步骤且 ``created=False``，下一轮 flush 会走
        「新段创建」分支，那里的预算判断自然把它拆到新卡。这样就把一个走不通的
        场景接回了已经验证过的拆卡路径。

        返回是否成功截断。一步都装不下时返回 False：此时本卡其余部分已占满，
        只能让本次更新照原样发出（失败后 :meth:`_handle_flush_error` 会重开拆卡
        开关，由下一个新段把内容带去新卡）。
        """
        seg = turn.segment_state.segments[index]
        offset = render.find_tool_split_offset(
            base_count=base_count,
            seg=seg,
            all_steps=all_steps,
            threshold=self._cfg.element_threshold,
        )
        if offset is None:
            logger.warning(
                "工具段超预算但一步都装不下，本卡已满: turn=%s el=%s",
                turn.turn_key[:12],
                seg.el_id,
            )
            METRICS.incr("card.tool_truncate_failed")
            return False

        turn.segment_state.split_tool_segment(index, offset)
        METRICS.incr("card.tool_segment_truncated")
        log_turn(20, turn.turn_key, "工具段超预算，在第 %d 步截断并另起新段", offset)
        return True

    async def _split_card(
        self,
        turn: Turn,
        split_index: int,
        pending_actions: list[dict[str, Any]],
        pending_new: dict[str, int],
        touched: set[str],
        finalized: set[str],
        client: FeishuClient,
    ) -> bool:
        """卡片元素接近上限时拆成新卡.

        顺序：先提交待发变更 → 建新卡 → 封存旧卡 → 切换。
        先建后封是刻意的：建卡失败时旧卡尚未关闭，可以降级继续写旧卡。
        """
        segments = turn.segment_state.segments
        if pending_actions and not await self._apply_actions(
            turn, client, pending_actions, pending_new, touched, finalized, segments
        ):
            return False

        old_card_id = turn.card_id
        seal_segments = [seg for seg in segments[turn.split_index : split_index] if seg.created]

        try:
            new_card = render.build_streaming_card(
                header_enabled=self._cfg.header_enabled,
                width_mode=self._cfg.width_mode,
                summary=turn.summary_text(self._cfg),
            )
            new_card_id = await client.create_card(new_card)
            new_msg_id = await client.reply_with_card(turn.anchor_id or turn.message_id, new_card_id)
        except Exception:
            # 建新卡失败 → 关闭拆卡能力，继续往当前卡写（宁可超限报错也不中断输出）
            turn.split_disabled = True
            logger.warning("拆卡失败，降级继续使用当前卡: turn=%s", turn.turn_key[:12], exc_info=True)
            return True

        # 封存旧卡：先关流式再全量重建，让旧卡定格为完整快照
        if old_card_id:
            try:
                sequence = turn.next_sequence()
                await client.close_streaming(old_card_id, sequence=sequence)
                archived = render.build_archived_card(
                    segments=seal_segments,
                    all_tool_steps=turn.tools.build_display_steps(),
                    body_text_size=self._cfg.body_text_size,
                    panel_expanded=self._cfg.panel_expanded,
                    show_tool_use=self._cfg.show_tool_use,
                    width_mode=self._cfg.width_mode,
                )
                await client.update_card(old_card_id, archived, sequence=turn.next_sequence())
            except Exception:
                logger.warning("封存旧卡失败，继续切换新卡", exc_info=True)

        turn.rebind_card(card_id=new_card_id, card_msg_id=new_msg_id, split_index=split_index)
        for seg in segments[split_index:]:
            seg.created = False
            seg.dirty = True

        METRICS.incr("card.split")
        log_turn(20, turn.turn_key, "已拆卡 new_card=%s split_index=%d", new_card_id[:12], split_index)
        return True

    # ── 终态 ──────────────────────────────────────────────────────

    async def complete_turn(
        self,
        turn_key: str,
        *,
        answer: str = "",
        is_error: bool = False,
        duration: float = 0.0,
        model: str = "",
        tokens: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Delivery:
        """收束 turn，返回投递三态.

        ``TAKEN`` 表示卡片已完整承载本轮输出，Hermes 应抑制原生正文；
        其余两态都要求 Hermes 正常输出，避免丢消息。
        """
        if not self.enabled:
            return Delivery.DECLINED

        turn = self._registry.get(turn_key)
        if turn is None:
            return Delivery.DECLINED

        try:
            await self._await_card_creation(turn)
            if not turn.has_card or turn.state == TurnState.FALLBACK:
                turn.mark_fallback()
                return Delivery.DECLINED

            turn.ensure_final_answer(answer)
            turn.set_footer(
                duration=duration, model=model, tokens=tokens, context=context, usage=self._usage_line()
            )
            if is_error:
                turn.transition(TurnState.FAILED)
            else:
                turn.transition(TurnState.FINALIZING)

            return await self._finalize(turn, is_error=is_error, is_aborted=False)
        except Exception as error:
            METRICS.record_error("complete_turn", error)
            logger.warning("收卡异常: turn=%s", turn_key[:12], exc_info=True)
            return Delivery.UNKNOWN
        finally:
            self._drop_runtime(turn_key)

    async def abort_turn(self, turn_key: str, *, aborted: bool = True, reason: str = "") -> Delivery:
        """中断收束（/stop 或被新消息打断）.

        ``reason`` 取 ``"stopped"``（用户显式 /stop）或 ``"interrupted"``
        （被新消息接续）。两者都进 ABORTED 态，但卡片文案与配色不同——
        用户需要分清是自己停的还是被后一条消息顶掉的。
        """
        turn = self._registry.get(turn_key)
        if turn is None:
            return Delivery.DECLINED
        if reason:
            with turn.lock:
                turn.abort_reason = reason
        try:
            await self._await_card_creation(turn)
            if not turn.has_card:
                turn.mark_fallback()
                return Delivery.DECLINED
            self._note_timeout_reason(turn, reason)
            self._ensure_duration_footer(turn)
            turn.transition(TurnState.ABORTED if aborted else TurnState.FAILED)
            return await self._finalize(turn, is_error=not aborted, is_aborted=aborted)
        except Exception:
            logger.debug("中断收卡失败", exc_info=True)
            return Delivery.UNKNOWN
        finally:
            self._drop_runtime(turn_key)

    # ── 回收路径的收卡（注册表交还）────────────────────────────────

    def _timeout_notice(self, reason: str) -> str:
        """按回收原因给出说明文案.

        必须说清「任务本身可能还在跑」——卡片停止跟踪与任务失败是两件事，
        混为一谈会让用户以为任务挂了而重复提问。
        """
        if reason == REASON_EXPIRED:
            return f"⏱️ 本轮超过 {int(self._cfg.turn_ttl_sec)} 秒无更新，卡片停止跟踪（任务可能仍在运行）"
        if reason == REASON_EVICTED:
            return f"⏱️ 同时进行的会话超过 {int(self._cfg.max_turns)} 个，本轮卡片停止跟踪（任务不受影响）"
        return f"⏱️ 超过 {int(self._cfg.idle_finalize_sec)} 秒无更新，卡片自动收尾（任务可能仍在运行）"

    def _ensure_duration_footer(self, turn: Turn) -> None:
        """中断 / 超时收卡时补上耗时.

        这类收卡拿不到 model 与 token 明细（那些来自对话方法织入的返回值），
        但「跑了多久」是现成的，也是用户最关心的一项。已有 footer 时不覆盖——
        正常收卡路径填的内容更完整。

        刻意不查订阅额度：中断路径要求立刻定格，不引入任何可能的网络等待。
        """
        if turn.footer:
            return
        try:
            turn.set_footer(duration=max(0.0, time.time() - turn.created_at))
        except Exception:
            logger.debug("耗时 footer 写入失败", exc_info=True)

    def _note_timeout_reason(self, turn: Turn, reason: str) -> None:
        """超时族收卡前补一条说明性提示.

        **必须在迁移到终态之前调用**：终态不再接受新内容。用户主动 /stop 与
        被新消息接续不需要这条说明（他们知道自己做了什么），因此只对
        ``TIMEOUT_REASONS`` 生效。
        """
        if reason not in TIMEOUT_REASONS:
            return
        try:
            turn.add_notice(self._timeout_notice(reason), NoticeLevel.WARNING)
        except Exception:
            logger.debug("超时说明写入失败", exc_info=True)

    def drain_pending_finalize(self) -> int:
        """把注册表交还的「待收卡」turn 逐个异步收尾，返回处理条数.

        同步方法，可从任何线程调用；实际收卡在事件循环里执行。
        必须**先取走队列再判断开关**：插件被关时也要清空队列，否则它会一直
        持有 Turn 引用，让防泄漏的机制自己变成泄漏。
        """
        try:
            pending = self._registry.take_pending_finalize()
        except Exception:
            logger.debug("取待收卡队列失败", exc_info=True)
            return 0
        if not pending or not self.enabled:
            return 0

        for turn, reason in pending:
            try:
                self._spawn(self.finalize_detached(turn, reason=reason))
            except Exception:
                logger.debug("回收收卡调度失败: turn=%s", turn.turn_key[:12], exc_info=True)
        return len(pending)

    async def finalize_detached(self, turn: Turn, *, reason: str) -> Delivery:
        """收束已从注册表摘除的 turn（TTL 回收 / 容量淘汰）.

        与 :meth:`abort_turn` 的唯一差别是不查注册表——turn 已经不在表里，
        用 ``turn_key`` 反查必然拿不到。其余流程（排空、关流式、全量重建）
        完全一致，因此这类卡片的呈现不是二等公民。

        **为什么必须有这条路径**：注册表的 LRU 与 TTL 回收会摘掉尚未终态的
        turn，它的卡片此刻正带着 loading 动画。只丢内存对象的话，卡片就会
        永远转圈——参考实现 HLS 的线上故障正是这个形状。
        """
        if turn.state.is_terminal:
            # 入队与取出之间已被正常收卡，不重复改卡
            self._drop_runtime(turn.turn_key)
            return Delivery.DECLINED
        if not turn.has_card:
            self._drop_runtime(turn.turn_key)
            return Delivery.DECLINED

        logger.warning(
            "回收未完成的 turn，强制收卡: turn=%s reason=%s state=%s",
            turn.turn_key[:12],
            reason,
            turn.state.value,
        )
        METRICS.incr(f"turn.detached_finalized.{reason}")
        try:
            with turn.lock:
                turn.abort_reason = reason
            # 说明性提示：FINALIZING 态加不进去（不接受新内容），无妨——那种
            # 情况说明收卡本身卡住了，一条提示也救不回来
            self._note_timeout_reason(turn, reason)
            self._ensure_duration_footer(turn)
            turn.transition(TurnState.ABORTED)
            return await self._finalize(turn, is_error=False, is_aborted=True)
        except Exception:
            logger.debug("回收收卡失败: turn=%s", turn.turn_key[:12], exc_info=True)
            return Delivery.UNKNOWN
        finally:
            self._drop_runtime(turn.turn_key)

    async def _finalize(self, turn: Turn, *, is_error: bool, is_aborted: bool) -> Delivery:
        """终态收卡：排空待刷内容 → 关流式 → 全量重建."""
        runtime = self._runtime(turn.turn_key)
        if runtime is not None:
            try:
                await runtime.scheduler.flush_now()
                await runtime.scheduler.wait_idle()
            except Exception:
                logger.debug("终态前排空失败", exc_info=True)
            runtime.scheduler.close()
            if runtime.guard.terminated:
                # 消息已删除，不再尝试任何更新，但内容已无处可去
                return Delivery.DECLINED

        turn.finalize_segments()
        await self._resolve_images(turn)

        # 目标终态必须先算出来：卡片里的会话列表摘要要写「本次收成什么样」，
        # 而此刻 turn.state 还停在 FINALIZING（真正落定要等 update_card 成功，
        # 见循环内的赋值）。不提前算，已完成的任务会在会话列表里显示成
        # 「✍️ 正在写」，正好破坏 summary 存在的目的
        final_state = (
            TurnState.FAILED if is_error else TurnState.ABORTED if is_aborted else TurnState.COMPLETED
        )

        client = self._ensure_client(turn.chat_id)
        card = render.build_complete_card(
            segments=turn.active_segments(),
            all_tool_steps=turn.tools.build_display_steps(),
            footer_data=turn.footer,
            footer_fields=self._cfg.footer_fields,
            footer_show_label=self._cfg.footer_show_label,
            footer_enabled=self._cfg.footer_enabled,
            footer_text_size=self._cfg.footer_text_size,
            body_text_size=self._cfg.body_text_size,
            panel_expanded=self._cfg.panel_expanded,
            show_tool_use=self._cfg.show_tool_use,
            header_enabled=self._cfg.header_enabled,
            width_mode=self._cfg.width_mode,
            summary=turn.summary_text(self._cfg, state_override=final_state),
            is_error=is_error,
            is_aborted=is_aborted,
            abort_reason=turn.abort_reason,
            tool_dropped=turn.tools.dropped,
        )

        streaming_closed = False
        for attempt in range(FINALIZE_ATTEMPTS):
            try:
                if not streaming_closed:
                    await client.close_streaming(turn.card_id or "", sequence=turn.next_sequence())
                    streaming_closed = True
                await client.update_card(turn.card_id or "", card, sequence=turn.next_sequence())

                turn.state = final_state
                METRICS.incr("card.finalized")
                log_turn(20, turn.turn_key, "卡片已收束 state=%s", final_state.value)
                self._breaker.record_success()
                return Delivery.TAKEN
            except Exception as error:
                if classify(error) == Action.ABORT_PIPELINE:
                    return Delivery.DECLINED
                if attempt < FINALIZE_ATTEMPTS - 1:
                    await asyncio.sleep(2**attempt * 0.3)
                    continue
                METRICS.record_error("finalize", error)
                logger.warning("终态收卡失败: turn=%s", turn.turn_key[:12], exc_info=True)

        turn.mark_fallback()
        # 已经关闭流式但更新失败：卡片状态不明，保守上报 UNKNOWN 让 Hermes 兜底
        return Delivery.UNKNOWN if streaming_closed else Delivery.DECLINED

    async def _resolve_images(self, turn: Turn) -> None:
        """把回答中的 markdown 图片上传飞书，换成 img_key.

        失败的图片保持原链接，不影响其余内容。
        """
        runtime = self._runtime(turn.turn_key)
        if runtime is None:
            return
        urls: list[str] = []
        for seg in turn.active_segments():
            if seg.type == SegmentType.ANSWER and seg.text:
                urls.extend(render.find_image_refs(seg.text))
        pending = [url for url in dict.fromkeys(urls) if url not in runtime.images]
        if not pending:
            return

        client = self._ensure_client(turn.chat_id)
        for url in pending[:10]:  # 单轮最多处理 10 张，防止拖慢收卡
            try:
                image_key = await client.upload_image(url)
                if image_key:
                    runtime.images[url] = image_key
            except Exception:
                logger.debug("图片处理失败: %s", url, exc_info=True)

        if runtime.images:
            for seg in turn.active_segments():
                if seg.type == SegmentType.ANSWER and seg.text:
                    seg.text = render.replace_image_refs(seg.text, runtime.images)

    # ── 独立卡片 ──────────────────────────────────────────────────

    async def deliver_cron(
        self,
        *,
        chat_id: str,
        content: str,
        task_name: str = "",
        run_time: str = "",
    ) -> bool:
        """定时任务结果 —— 不属于任何 turn，独立成卡."""
        if not self.enabled or not chat_id or not content.strip():
            return False
        try:
            client = self._ensure_client(chat_id)
            card = render.build_cron_card(
                content, task_name=task_name, run_time=run_time, width_mode=self._cfg.width_mode
            )
            await client.send_card(chat_id, card)
            METRICS.incr("card.cron")
            return True
        except Exception:
            METRICS.incr("card.cron_failed")
            logger.warning("定时任务卡片发送失败", exc_info=True)
            return False

    async def deliver_background(
        self,
        *,
        chat_id: str,
        preview: str,
        content: str,
        reply_to_message_id: str | None = None,
    ) -> bool:
        """后台任务完成推送."""
        if not self.enabled or not chat_id or not content.strip():
            return False
        try:
            client = self._ensure_client(chat_id)
            card = render.build_background_card(preview, content, width_mode=self._cfg.width_mode)
            await client.send_card(chat_id, card, reply_to_message_id=reply_to_message_id)
            METRICS.incr("card.background")
            return True
        except Exception:
            METRICS.incr("card.background_failed")
            logger.warning("后台任务卡片发送失败", exc_info=True)
            return False

    # ── 熔断与诊断 ────────────────────────────────────────────────

    def record_capture_failure(self, capability: str = "", error: BaseException | str = "") -> None:
        """记录一次收纳失败.

        带 ``capability`` 时走**精准降级**：只让该类消息退回原生，其余能力
        照常工作，且结论落盘供下次启动继承。这取代了原先「任一收纳连续失败
        5 次就全局旁路」的粗粒度行为——一类消息的回调签名变化不该让整个
        插件失效。

        只有当失败**跨越 :data:`SYSTEMIC_DEGRADE_COUNT` 个以上维度**时才升级为
        全局熔断：那种情况不是某个回调的问题，而是凭据失效、网络不可达或
        API 权限缺失，此时退回原生才是正确动作。

        不带 ``capability`` 的失败（核心流式链路）行为不变，直接计入全局熔断。
        """
        if capability:
            try:
                self._healer.record_failure(capability, error)
                degraded = self._healer.degraded_capabilities()
            except Exception:
                logger.debug("自愈层记录失败，退回全局熔断计数", exc_info=True)
            else:
                if len(degraded) < SYSTEMIC_DEGRADE_COUNT:
                    return
                # 已独立判定为系统性故障，直接熔断而不再累计计数：此时
                # 每类都已失败到降级（至少 degrade_after_failures 次），
                # 再慢慢累计只是让 Agent 继续为徒劳的尝试付代价
                if self._breaker.trip():
                    logger.error(
                        "已有 %d 类收纳同时降级（%s），判定为系统性故障（凭据 / 网络 / 权限），"
                        "插件进入旁路模式，本进程后续消息全部走 Hermes 原生路径",
                        len(degraded),
                        "、".join(degraded),
                    )
                    METRICS.incr("selfheal.systemic")
                    METRICS.incr("breaker.opened")
                return
        self._trip_breaker()

    def _trip_breaker(self) -> None:
        if self._breaker.record_failure():
            logger.error(
                "连续 %d 次收纳失败，插件进入旁路模式，本进程后续消息全部走 Hermes 原生路径",
                self._breaker.failures,
            )
            METRICS.incr("breaker.opened")

    def record_capture_success(self, capability: str = "") -> None:
        if capability:
            try:
                self._healer.record_success(capability)
            except Exception:
                logger.debug("自愈层记录成功状态失败", exc_info=True)
        self._breaker.record_success()

    def capture_active(self, kind: str) -> bool:
        """该类游离消息此刻是否应当收纳进卡片.

        优先级严格为 **用户显式配置 > 学到的降级经验 > 默认全开**。
        用户写死 ``capture.notice: true`` 时，即使这一类连续失败一百次也会
        继续尝试——插件不偷改用户设定，只在 ``doctor`` 报告里说明情况。
        """
        try:
            explicit = self._cfg.capture_explicit(kind)
        except Exception:
            explicit = None
        if explicit is not None:
            return explicit
        try:
            return not self._healer.is_degraded(kind)
        except Exception:
            logger.debug("自愈状态读取失败，按未降级处理", exc_info=True)
            return True

    @property
    def healer(self) -> SelfHealer:
        return self._healer

    def publish_activity(self) -> bool:
        """把当前活跃卡片的摘要落盘，供 ``activity`` 命令跨进程读取.

        **为什么必须落盘**：活跃 turn 只存在于 gateway 进程的内存里，而
        ``python -m hermes_lark_streaming activity`` 是另一个进程，它无从得知。
        升级前想知道「现在有没有任务在跑」，只能靠这份心跳传递。

        写入内容刻意只有标识与状态：turn key 与 chat id 都截断，工具名已在
        采集时脱敏，不含任何消息正文。
        """
        try:
            payload = {
                "at": int(time.time()),
                "pid": os.getpid(),
                "plugin_version": __version__,
                "active": [
                    {
                        "turn": short(turn.turn_key, 8),
                        "chat": short(turn.chat_id, 8),
                        "state": turn.state.value,
                        "age_sec": max(0, int(time.time() - turn.created_at)),
                        "idle_sec": max(0, int(time.time() - turn.updated_at)),
                        "action": turn.tools.current_action(),
                    }
                    for turn in self._registry.active_turns()
                ],
                # 织入实况：适配器方法到底织上了几个。这是跨进程唯一的通道——
                # 一次真机故障里适配器零方法织入却处处报 ok，只能靠读源码倒推
                "weave": self._weave_snapshot(),
            }
            return write_activity(self._home, payload)
        except Exception:
            logger.debug("活动心跳发布失败", exc_info=True)
            return False

    def _weave_snapshot(self) -> dict[str, Any]:
        """取桥接层的织入实况。未注入 reporter 时返回空字典."""
        reporter = self._weave_reporter
        if reporter is None:
            return {}
        try:
            snapshot = reporter()
        except Exception:
            logger.debug("织入实况读取失败", exc_info=True)
            return {}
        return snapshot if isinstance(snapshot, dict) else {}

    def set_weave_reporter(self, reporter: Any) -> None:
        """注入「适配器织入实况」查询器.

        与 :meth:`set_usage_provider` 同一模式：实况住在 L0 桥接层，编排层不能
        反向 import bridge，所以由 bridge 在启动时把读取函数递进来。
        """
        self._weave_reporter = reporter

    def set_usage_provider(self, provider: Any) -> None:
        """注入订阅额度查询器.

        用依赖注入而非直接 import：查询额度要碰 Hermes 的
        ``agent.account_usage``，那是 L0 桥接层的职责，编排层不该反向依赖
        bridge。与 :func:`..bridge.weave.set_observer` 同一模式。
        """
        self._usage_provider = provider

    def _usage_line(self) -> str:
        """取额度展示文本。未注入、未开启或查询失败都返回空字符串."""
        provider = self._usage_provider
        if provider is None:
            return ""
        try:
            return str(provider() or "")
        except Exception:
            logger.debug("额度查询失败", exc_info=True)
            return ""

    @property
    def bypassed(self) -> bool:
        return self._breaker.is_open

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "bypassed": self.bypassed,
            "breaker_failures": self._breaker.failures,
            "turns": self._registry.stats(),
            "metrics": METRICS.snapshot(),
            "selfheal": self._healer.snapshot(),
        }


# ── 单例管理（按 profile home 隔离）─────────────────────────────────

_instances: dict[str, Orchestrator] = {}
_instances_lock = threading.Lock()


def get_orchestrator(profile_home: Path | None = None) -> Orchestrator:
    home = (profile_home or hermes_home()).resolve()
    key = str(home)
    with _instances_lock:
        instance = _instances.get(key)
        if instance is None:
            instance = Orchestrator(home)
            _instances[key] = instance
        return instance


def reset_orchestrators() -> None:
    """仅供测试使用."""
    with _instances_lock:
        _instances.clear()

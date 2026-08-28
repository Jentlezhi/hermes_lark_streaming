"""Turn 状态机 — 一次 Agent 运行对应一张卡片.

**One Turn, One Card**：一轮运行产生的所有内容（回答、思考、工具、提示、
review、交互）全部收敛到同一张卡片，杜绝游离消息把卡片顶飞。

状态迁移图见 docs/02-架构设计.md 5.2。核心规则：

* ``WAITING`` 暂停 flush 但**不封卡**，避免审批期间卡片被提前定格
* 只有 ``TERMINAL`` 允许抑制 Hermes 原生正文；``FALLBACK`` 必须放行
* 状态只允许单向前进，禁止从终态回退
"""

from __future__ import annotations

import threading
import time
from enum import StrEnum
from typing import Any

from ..config import Config
from ..events import NoticeLevel
from .segments import (
    InteractionKind,
    InteractionState,
    InteractionStatus,
    NoticeItem,
    SegmentState,
    SegmentType,
)
from .tooltrack import ToolTracker


#: 中断原因的取值集合 —— 卡片文案与配色据此区分。
#:
#: 前两者是**用户能预期**的：自己按了 /stop，或自己又发了一条消息。
#: 后三者是**插件自我保护**触发的，用户毫不知情，因此卡片必须显式说明
#: 「不是任务失败、也不是任务完成，是卡片停止跟踪了」——这是与参考实现
#: 最关键的差别：它们在这些路径上直接丢弃 turn，卡片永远转圈。
REASON_STOPPED = "stopped"  # 用户显式 /stop
REASON_INTERRUPTED = "interrupted"  # 被新消息接续
REASON_TIMEOUT = "timeout"  # 空闲守护：长时间无更新
REASON_EXPIRED = "expired"  # 注册表 TTL 回收（含审批长期无人处理）
REASON_EVICTED = "evicted"  # 注册表容量上限淘汰

#: 「非用户意图」的中断原因族，卡片统一呈现为「超时收尾」。
#: 具体原因由卡内的说明性 notice 区分，不占用 header 的有限文案空间
TIMEOUT_REASONS: frozenset[str] = frozenset({REASON_TIMEOUT, REASON_EXPIRED, REASON_EVICTED})


class TurnState(StrEnum):
    IDLE = "idle"
    CREATING = "creating"
    STREAMING = "streaming"
    WAITING = "waiting"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    FALLBACK = "fallback"

    @property
    def is_terminal(self) -> bool:
        return self in (TurnState.COMPLETED, TurnState.FAILED, TurnState.ABORTED, TurnState.FALLBACK)

    @property
    def accepts_content(self) -> bool:
        """该状态下是否还接受新内容.

        WAITING 仍然接受内容（工具可能在审批期间继续上报），只是不触发 flush。
        """
        return self in (TurnState.CREATING, TurnState.STREAMING, TurnState.WAITING)

    @property
    def allows_flush(self) -> bool:
        return self == TurnState.STREAMING


class Delivery(StrEnum):
    """投递三态.

    禁止用布尔值表达投递结果：``UNKNOWN`` 必须与 ``DECLINED`` 区分，
    但两者都放行原生输出——宁可用户看到一次重复，也不能丢消息。
    """

    TAKEN = "taken"  # 卡片已确认接管，可抑制 Hermes 原生输出
    DECLINED = "declined"  # 明确未接管，放行原生输出
    UNKNOWN = "unknown"  # 结果不确定，保守放行原生输出

    @property
    def should_suppress_native(self) -> bool:
        return self == Delivery.TAKEN


class Turn:
    """单次 Agent 运行的完整状态.

    **线程安全**：Hermes 的回调来自多个线程（agent worker 线程 + gateway
    事件循环），因此所有状态读写都在 ``lock`` 保护下进行。飞书 API 调用
    一律在锁外发起，避免持锁做网络 IO。
    """

    __slots__ = (
        "abort_reason",
        "anchor_id",
        "card_id",
        "card_msg_id",
        "chat_id",
        "created_at",
        "element_count",
        "footer",
        "lock",
        "message_id",
        "notice_dropped",
        "segment_state",
        "sequence",
        "session_key",
        "split_disabled",
        "split_index",
        "state",
        "text_fallback_needed",
        "tools",
        "turn_key",
        "updated_at",
    )

    def __init__(
        self,
        *,
        turn_key: str,
        message_id: str,
        chat_id: str,
        anchor_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        self.turn_key = turn_key
        self.message_id = message_id
        self.chat_id = chat_id
        # 回复锚点：飞书话题场景下卡片要挂到用户原消息上
        self.anchor_id = anchor_id
        self.session_key = session_key

        self.state = TurnState.IDLE
        self.lock = threading.RLock()
        self.created_at = time.time()
        self.updated_at = self.created_at
        # 中断原因，取值见模块顶部 REASON_* 常量。全部进 ABORTED 态，但文案
        # 与配色分三档：用户主动停止（红）/ 被新消息接续（橙）/ 超时收尾（橙）
        self.abort_reason = ""

        self.segment_state = SegmentState()
        self.tools = ToolTracker()

        # 飞书卡片实体标识
        self.card_id: str | None = None
        self.card_msg_id: str | None = None
        # CardKit 要求 sequence 单调递增，新卡从 1 重新计数
        self.sequence = 1

        # 元素预算
        self.element_count = 0
        self.split_index = 0
        self.split_disabled = False

        self.footer: dict[str, Any] = {}
        self.text_fallback_needed = False
        self.notice_dropped = 0

    # ── 状态迁移 ──────────────────────────────────────────────────

    def transition(self, target: TurnState) -> bool:
        """迁移状态.

        返回是否实际发生迁移。终态不可回退，重复迁移到同一终态视为幂等成功。
        """
        with self.lock:
            if self.state == target:
                return False
            if self.state.is_terminal:
                # 终态之后只允许 FALLBACK 这一种「补记」，且仅当当前不是成功终态
                return False
            self.state = target
            self.updated_at = time.time()
            return True

    def mark_fallback(self) -> None:
        """标记为需要交还 Hermes 原生输出."""
        with self.lock:
            self.text_fallback_needed = True
            if not self.state.is_terminal:
                self.state = TurnState.FALLBACK
                self.updated_at = time.time()

    def consume_text_fallback(self) -> bool:
        """取出并清除「需要原生文本兜底」标记，只生效一次."""
        with self.lock:
            needed = self.text_fallback_needed
            self.text_fallback_needed = False
            return needed

    @property
    def has_card(self) -> bool:
        return bool(self.card_id)

    @property
    def has_running_tool(self) -> bool:
        """是否还有工具正在执行（供空闲守护判定用）.

        持锁访问 :class:`ToolTracker`：那个类本身不加锁，约定由 Turn 持锁后调用
        （见其类文档）。守护跑在事件循环上，而工具事件来自 worker 线程，这里是
        真正的跨线程读取，不能绕过约定。
        """
        with self.lock:
            return self.tools.has_running

    def bind_card(self, *, card_id: str, card_msg_id: str) -> None:
        with self.lock:
            self.card_id = card_id
            self.card_msg_id = card_msg_id
            self.updated_at = time.time()

    def rebind_card(self, *, card_id: str, card_msg_id: str, split_index: int) -> None:
        """拆卡后切换到新卡片，重置与卡片绑定的计数."""
        with self.lock:
            self.card_id = card_id
            self.card_msg_id = card_msg_id
            self.sequence = 1
            self.element_count = 1  # loading 占位元素
            self.split_index = split_index
            self.split_disabled = False
            self.updated_at = time.time()

    def next_sequence(self) -> int:
        with self.lock:
            self.sequence += 1
            return self.sequence

    # ── 内容写入（全部在锁内，保证 segment 顺序与真实时序一致）──────

    def add_answer(self, text: str) -> bool:
        if not text:
            return False
        with self.lock:
            if not self.state.accepts_content:
                return False
            self.segment_state.append_answer(text)
            self.updated_at = time.time()
            return True

    def add_reasoning(self, text: str) -> bool:
        if not text:
            return False
        with self.lock:
            if not self.state.accepts_content:
                return False
            self.segment_state.append_reasoning(text)
            self.updated_at = time.time()
            return True

    def add_notice(self, text: str, level: NoticeLevel, *, as_review: bool = False) -> bool:
        if not text or not text.strip():
            return False
        with self.lock:
            if not self.state.accepts_content:
                return False
            seg_type = SegmentType.REVIEW if as_review else SegmentType.NOTICE
            self.segment_state.append_notice(NoticeItem(text=text.strip(), level=level), seg_type)
            self.updated_at = time.time()
            return True

    def add_tool_start(self, name: str, detail: str) -> bool:
        with self.lock:
            if not self.state.accepts_content:
                return False
            self.tools.record_start(name, detail)
            self.segment_state.on_tool_event(self.tools.step_count)
            self.updated_at = time.time()
            return True

    def add_tool_end(self, name: str, *, error: str = "", output: str = "") -> bool:
        with self.lock:
            if not self.state.accepts_content:
                return False
            self.tools.record_end(name, error=error, output=output)
            self.segment_state.on_tool_event(self.tools.step_count)
            self.updated_at = time.time()
            return True

    def open_interaction(self, kind: InteractionKind, title: str, detail: str = "") -> bool:
        """开启交互并进入等待态（暂停 flush，但保留卡片继续接收内容）."""
        with self.lock:
            if not self.state.accepts_content:
                return False
            self.segment_state.open_interaction(
                InteractionState(kind=kind, title=title, detail=detail)
            )
            if self.state == TurnState.STREAMING:
                self.state = TurnState.WAITING
            self.updated_at = time.time()
            return True

    def close_interaction(
        self,
        kind: InteractionKind,
        status: InteractionStatus = InteractionStatus.RESOLVED,
        result: str = "",
    ) -> bool:
        with self.lock:
            segment = self.segment_state.resolve_interaction(kind, status, result)
            if self.state == TurnState.WAITING:
                self.state = TurnState.STREAMING
            self.updated_at = time.time()
            return segment is not None

    def set_footer(
        self,
        *,
        duration: float = 0.0,
        model: str = "",
        tokens: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        usage: str = "",
    ) -> None:
        with self.lock:
            footer: dict[str, Any] = {"duration": duration, "model": model}
            if tokens:
                footer["input_tokens"] = tokens.get("input_tokens", 0)
                footer["output_tokens"] = tokens.get("output_tokens", 0)
            if context:
                footer["context_used"] = context.get("used_tokens", 0)
                footer["context_max"] = context.get("max_tokens", 0)
            if usage:
                footer["usage"] = usage
            self.footer = footer

    def ensure_final_answer(self, answer: str) -> None:
        """终态兜底：流式全程没收到任何回答时，用最终答案补一段.

        只在确实没有 ANSWER 段时才补，避免与流式内容重复。
        """
        if not answer or not answer.strip():
            return
        with self.lock:
            if not self.segment_state.has_answer:
                self.segment_state.append_answer(answer)

    def finalize_segments(self) -> None:
        with self.lock:
            self.segment_state.finalize(self.tools.step_count)

    def active_segments(self) -> list[Any]:
        """当前卡片承载的段（拆卡后只取归属本卡的部分）."""
        with self.lock:
            return self.segment_state.segments[self.split_index :]

    # ── 会话列表状态摘要（治理「切走后看不出是否完成」）──────────────

    def summary_text(self, cfg: Config, *, state_override: TurnState | None = None) -> str:
        """生成会话列表预览文案.

        飞书把 ``card.config.summary`` 显示在会话列表，这是唯一的跨会话
        状态通道。文案随阶段实时更新，用户不点开也能判断任务是否跑完。

        ``state_override`` 专供终态收卡：那一刻 ``self.state`` 还停在
        ``FINALIZING``（真正的终态要等 ``update_card`` 成功才敢落定），而卡片上
        要写的是本次收卡的**目标**终态。不传它就会把已完成的任务在会话列表里
        显示成「✍️ 正在写」——恰好是本方法存在的理由被自己破坏掉。
        """
        if not cfg.summary_enabled:
            return ""
        limit = cfg.summary_max_chars

        with self.lock:
            state = state_override if state_override is not None else self.state
            pending = self.segment_state.pending_interaction()
            action = self.tools.current_action()
            last_text = self.segment_state.last_text()

        if state in (TurnState.IDLE, TurnState.CREATING):
            return "⏳ 已收到，正在启动…"

        if state == TurnState.WAITING or pending is not None:
            if pending is not None and pending.kind == InteractionKind.APPROVAL:
                return "⏸️ 等待你确认命令执行"
            return "⏸️ 等待你的回复"

        if state in (TurnState.STREAMING, TurnState.FINALIZING):
            if action:
                return _clip(f"🛠️ {action}", limit)
            if last_text.strip():
                return _clip(f"✍️ {_flatten(last_text)}", limit)
            return "💭 思考中…"

        if state == TurnState.COMPLETED:
            if last_text.strip():
                return _clip(f"✅ {_flatten(last_text)}", limit)
            return "✅ 已完成"
        if state == TurnState.FAILED:
            return "❌ 执行失败"
        if state == TurnState.ABORTED:
            if self.abort_reason in TIMEOUT_REASONS:
                return "⏱️ 已超时收尾"
            if self.abort_reason == REASON_INTERRUPTED:
                return "⏭️ 已中断 · 新消息已接续"
            return "⏹️ 已停止"
        return ""


def _flatten(text: str) -> str:
    """压平为单行摘要，去掉会干扰预览的 markdown 结构."""
    return " ".join(text.replace("```", " ").replace("\n", " ").split())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"

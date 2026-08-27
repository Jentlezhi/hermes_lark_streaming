"""节流调度器.

**为什么需要节流**：模型的 delta 可能以每秒上百次的频率到达，逐条推送会
立刻触发 CardKit 频控（230020）。但节流太狠又会让打字机变成跳变。

**本插件的解法**：服务端节流 100ms，客户端按 15ms/字 插值播放。两者解耦，
所以服务端可以从容合并，用户看到的仍是连续打字。

三档策略：

* 距上次刷新 ≥ 100ms — 立即刷
* 距上次刷新 > 2s（长空闲）— 延迟 300ms 再刷，攒一小批让内容更完整
* 仍在窗口内 — 延迟到窗口边界

外加互斥与补偿重刷：刷新期间到达的增量不会丢，会在本次刷新结束后立刻补刷。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..observability import logger

#: 常规节流间隔（秒）
THROTTLE_SEC = 0.100
#: 超过该间隔视为长空闲
LONG_GAP_SEC = 2.000
#: 长空闲后的批量等待，让首屏内容更完整
BATCH_AFTER_GAP_SEC = 0.300

FlushFn = Callable[[], Awaitable[None]]


class FlushScheduler:
    """带互斥与补偿的节流调度器.

    不含任何飞书概念，只负责「何时执行回调」。
    """

    __slots__ = (
        "_closed",
        "_flush_fn",
        "_in_progress",
        "_last_flush_at",
        "_loop",
        "_needs_reflush",
        "_ready",
        "_timer",
        "_waiters",
    )

    def __init__(self, flush_fn: FlushFn, *, loop: asyncio.AbstractEventLoop) -> None:
        self._flush_fn = flush_fn
        self._loop = loop
        self._timer: asyncio.TimerHandle | None = None
        self._in_progress = False
        self._needs_reflush = False
        self._closed = False
        self._ready = False
        self._last_flush_at = 0.0
        self._waiters: list[asyncio.Future[None]] = []

    # ── 状态控制 ──────────────────────────────────────────────────

    def set_ready(self, ready: bool) -> None:
        """标记卡片已就绪.

        卡片创建完成前所有 schedule 都会被忽略——此时还没有 card_id，
        刷新只会失败。
        """
        self._ready = ready
        if ready:
            self._last_flush_at = self._loop.time()

    def close(self) -> None:
        """停止接受新刷新，并唤醒所有等待者."""
        self._closed = True
        self._cancel_timer()
        self._wake_waiters()

    @property
    def closed(self) -> bool:
        return self._closed

    # ── 调度 ──────────────────────────────────────────────────────

    def schedule(self) -> None:
        """请求一次节流后的刷新（非阻塞）."""
        if self._closed or not self._ready:
            return

        now = self._loop.time()
        elapsed = now - self._last_flush_at

        if elapsed >= THROTTLE_SEC:
            if elapsed > LONG_GAP_SEC:
                # 长空闲后不急着刷：稍等一下让这一批内容更完整，
                # 避免用户看到「蹦一个字又停住」
                if self._timer is None:
                    self._arm(BATCH_AFTER_GAP_SEC)
            else:
                self._spawn()
        elif self._timer is None:
            self._arm(THROTTLE_SEC - elapsed)

    async def flush_now(self) -> None:
        """立即刷新并等待完成（终态收尾前调用）."""
        if self._closed or not self._ready:
            return
        self._cancel_timer()
        await self._run()

    async def wait_idle(self) -> None:
        """等待进行中的刷新结束."""
        if not self._in_progress:
            return
        future: asyncio.Future[None] = self._loop.create_future()
        self._waiters.append(future)
        await future

    # ── 内部实现 ──────────────────────────────────────────────────

    def _arm(self, delay: float) -> None:
        self._cancel_timer()
        self._timer = self._loop.call_later(delay, self._spawn)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _spawn(self) -> None:
        """在事件循环上派发一次刷新任务."""
        self._timer = None
        if self._closed:
            return
        task = self._loop.create_task(self._run())
        # 保留引用避免任务被 GC；完成后自动释放
        task.add_done_callback(self._on_task_done)

    @staticmethod
    def _on_task_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.debug("刷新任务异常", exc_info=error)

    async def _run(self) -> None:
        # 已有刷新在跑：标记需要补刷，由当前刷新结束后接力
        if self._in_progress:
            self._needs_reflush = True
            return
        if self._closed:
            return

        self._in_progress = True
        self._needs_reflush = False
        try:
            await self._flush_fn()
        except Exception:
            # 刷新失败不能冒泡：单次刷新失败不应中断整个 turn
            logger.debug("刷新执行失败", exc_info=True)
        finally:
            self._in_progress = False
            self._last_flush_at = self._loop.time()
            self._wake_waiters()

        # 刷新期间又有新内容 → 立刻补刷，保证内容不滞留
        if self._needs_reflush and not self._closed:
            self._needs_reflush = False
            self._spawn()

    def _wake_waiters(self) -> None:
        waiters = self._waiters
        self._waiters = []
        for future in waiters:
            if not future.done():
                future.set_result(None)

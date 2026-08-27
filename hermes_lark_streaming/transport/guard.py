"""消息不可用保护.

消息被用户删除或撤回后，针对它的所有卡片更新都会失败。若不识别这种情况，
插件会在每个 delta 上重试、刷屏错误日志，直到 turn 超时。

本模块在**进程级**缓存已知失效的消息 id：一个 turn 撞上终态错误码后，
同会话的后续操作直接短路，不再发请求。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..observability import METRICS, logger
from .resilience import is_terminal_message_error

#: 失效标记的存活时间。30 分钟足够覆盖一轮长任务，又不会无限增长
_CACHE_TTL_SEC = 30 * 60
#: 缓存容量上限，超出按最早写入淘汰
_CACHE_MAX = 4096


class _UnavailableCache:
    """已知失效消息的进程级缓存."""

    __slots__ = ("_entries", "_lock")

    def __init__(self) -> None:
        self._entries: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, message_id: str | None) -> None:
        if not message_id:
            return
        with self._lock:
            self._entries[message_id] = time.time()
            if len(self._entries) > _CACHE_MAX:
                self._prune_locked(force=True)

    def contains(self, message_id: str | None) -> bool:
        if not message_id:
            return False
        with self._lock:
            stamped = self._entries.get(message_id)
            if stamped is None:
                return False
            if time.time() - stamped > _CACHE_TTL_SEC:
                del self._entries[message_id]
                return False
            return True

    def _prune_locked(self, *, force: bool = False) -> None:
        now = time.time()
        expired = [key for key, stamped in self._entries.items() if now - stamped > _CACHE_TTL_SEC]
        for key in expired:
            del self._entries[key]
        if force and len(self._entries) > _CACHE_MAX:
            # 按写入时间淘汰最早的一批
            ordered = sorted(self._entries.items(), key=lambda item: item[1])
            for key, _ in ordered[: len(self._entries) - _CACHE_MAX + 1]:
                self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_CACHE = _UnavailableCache()


class MessageGuard:
    """单个 turn 的消息可用性守卫.

    一旦判定不可用即进入终止态，之后所有 ``should_skip`` 返回 True，
    调用方据此跳过全部飞书 API 调用。
    """

    __slots__ = ("_anchor_id", "_card_msg_id_getter", "_on_terminate", "_terminated")

    def __init__(
        self,
        *,
        anchor_id: str | None,
        card_msg_id_getter: Callable[[], str | None],
        on_terminate: Callable[[], None],
    ) -> None:
        self._anchor_id = anchor_id
        self._card_msg_id_getter = card_msg_id_getter
        self._on_terminate = on_terminate
        self._terminated = False

    @property
    def terminated(self) -> bool:
        return self._terminated

    def should_skip(self, scope: str) -> bool:
        """本次操作是否应跳过."""
        if self._terminated:
            return True
        if _CACHE.contains(self._anchor_id) or _CACHE.contains(self._card_msg_id_getter()):
            self._terminate(scope)
            return True
        return False

    def inspect_error(self, scope: str, error: BaseException) -> bool:
        """检查错误是否为消息终态错误，是则终止流水线.

        返回是否已终止。
        """
        if not is_terminal_message_error(error):
            return self._terminated
        self._terminate(scope, error=error)
        return True

    def _terminate(self, scope: str, error: BaseException | None = None) -> None:
        if self._terminated:
            return
        self._terminated = True
        _CACHE.mark(self._anchor_id)
        _CACHE.mark(self._card_msg_id_getter())
        METRICS.incr("guard.terminated")
        logger.warning(
            "消息已删除或撤回，停止卡片更新: scope=%s error=%s",
            scope,
            error if error is not None else "cached",
        )
        try:
            self._on_terminate()
        except Exception:
            logger.debug("guard 终止回调失败", exc_info=True)


def clear_unavailable_cache() -> None:
    """仅供测试使用."""
    _CACHE.clear()

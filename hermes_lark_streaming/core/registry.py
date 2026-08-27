"""Turn 注册表 — 多会话隔离与生命周期回收.

**为什么需要独立注册表**：平台适配器是进程内单例，被所有会话共享。织入
适配器方法后，包装函数拿到的只有 ``chat_id`` / ``session_key``，必须反查
出「这条消息属于哪个正在跑的 turn」。闭包绑定单个 turn 在这里是错的——
那样第二个会话的消息会被写进第一个会话的卡片。

回收策略：LRU（容量上限）+ TTL（时间上限）双重保险。任何一条触发即回收，
避免长时间运行的 gateway 内存无界增长。

**回收活跃 turn 必须先收卡**：被回收的 turn 若尚未终态，它的卡片正停在
「处理中」并带着 loading 动画。只把内存对象丢掉，卡片就会永远转圈——这正是
参考实现 HLS 的线上故障模式（按 ``created_at`` 剪枝，600 秒后必然发生）。
因此本注册表把「被回收的活跃 turn」放进 :meth:`take_pending_finalize`
队列交还编排层，由编排层异步收卡；注册表本身不认识飞书，也不做 IO。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from ..config import Config
from ..observability import METRICS, log_turn, logger
from .turn import REASON_EVICTED, REASON_EXPIRED, Turn, TurnState

#: 待收卡队列上限。正常情况下这个队列长度是 0~1
PENDING_FINALIZE_LIMIT = 64


class TurnRegistry:
    """线程安全的 turn 注册表.

    索引三份：主表按 ``turn_key``；``_aliases`` 收录 anchor_id 等别名；
    ``_by_chat`` / ``_by_session`` 供反查。别名表与反查表只在主表存在时有效，
    回收时同步清理，不允许出现悬空引用。
    """

    __slots__ = ("_aliases", "_by_chat", "_by_session", "_cfg", "_lock", "_pending_finalize", "_turns")

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._lock = threading.RLock()
        self._turns: OrderedDict[str, Turn] = OrderedDict()
        self._aliases: dict[str, str] = {}
        self._by_chat: dict[str, str] = {}
        self._by_session: dict[str, str] = {}
        # 已从主表摘除、但卡片仍需收尾的 turn。有界：超过上限丢最旧的，
        # 宁可漏收一张卡也不让这个队列自己变成新的内存泄漏点
        self._pending_finalize: list[tuple[Turn, str]] = []

    # ── 创建与注册 ────────────────────────────────────────────────

    def create(
        self,
        *,
        turn_key: str,
        message_id: str,
        chat_id: str,
        anchor_id: str | None = None,
        session_key: str | None = None,
    ) -> Turn | None:
        """创建 turn。已存在同键且未终态时返回 None，表示重复创建应忽略."""
        with self._lock:
            existing = self._turns.get(turn_key)
            if existing is not None and not existing.state.is_terminal:
                return None

            self._prune_locked()

            turn = Turn(
                turn_key=turn_key,
                message_id=message_id,
                chat_id=chat_id,
                anchor_id=anchor_id,
                session_key=session_key,
            )
            self._turns[turn_key] = turn
            self._turns.move_to_end(turn_key)

            if anchor_id and anchor_id != turn_key:
                self._aliases[anchor_id] = turn_key
            if message_id and message_id != turn_key:
                self._aliases[message_id] = turn_key
            if chat_id:
                self._by_chat[chat_id] = turn_key
            if session_key:
                self._by_session[session_key] = turn_key

            self._enforce_capacity_locked()

        METRICS.incr("turn.created")
        log_turn(10, turn_key, "turn 已创建 chat=%s anchor=%s", chat_id[:12], (anchor_id or "-")[:12])
        return turn

    # ── 查询 ──────────────────────────────────────────────────────

    def get(self, key: str | None) -> Turn | None:
        """按 turn_key 或任意别名查询."""
        if not key:
            return None
        with self._lock:
            turn = self._turns.get(key)
            if turn is None:
                aliased = self._aliases.get(key)
                if aliased is not None:
                    turn = self._turns.get(aliased)
            if turn is not None:
                self._turns.move_to_end(turn.turn_key)
            return turn

    def get_active(self, key: str | None) -> Turn | None:
        """按键查询且要求非终态."""
        turn = self.get(key)
        if turn is None or turn.state.is_terminal:
            return None
        return turn

    def get_active_by_chat(self, chat_id: str | None) -> Turn | None:
        """按会话反查活跃 turn — 适配器织入的主要入口.

        飞书同一会话在 Hermes 侧是串行的（新消息会打断旧消息），因此
        「该会话最近的非终态 turn」就是唯一正确答案。
        """
        if not chat_id:
            return None
        with self._lock:
            turn_key = self._by_chat.get(chat_id)
            if turn_key is None:
                return None
            turn = self._turns.get(turn_key)
        if turn is None or turn.state.is_terminal:
            return None
        return turn

    def get_active_by_session(self, session_key: str | None) -> Turn | None:
        if not session_key:
            return None
        with self._lock:
            turn_key = self._by_session.get(session_key)
            if turn_key is None:
                return None
            turn = self._turns.get(turn_key)
        if turn is None or turn.state.is_terminal:
            return None
        return turn

    def resolve_active(
        self,
        *,
        chat_id: str | None = None,
        session_key: str | None = None,
        message_id: str | None = None,
    ) -> Turn | None:
        """多线索解析活跃 turn，按可靠性从高到低尝试.

        session_key 最可靠（Hermes 内部主键），message_id 次之，
        chat_id 最宽松（同会话可能残留旧 turn，但已用终态过滤）。
        """
        for candidate in (
            self.get_active_by_session(session_key),
            self.get_active(message_id),
            self.get_active_by_chat(chat_id),
        ):
            if candidate is not None:
                return candidate
        return None

    # ── 别名与回收 ────────────────────────────────────────────────

    def add_alias(self, alias: str | None, turn_key: str) -> None:
        if not alias or alias == turn_key:
            return
        with self._lock:
            if turn_key in self._turns:
                self._aliases[alias] = turn_key

    def remove(self, turn_key: str) -> None:
        """回收 turn 及其全部索引."""
        with self._lock:
            turn = self._turns.pop(turn_key, None)
            if turn is None:
                return
            for alias, target in list(self._aliases.items()):
                if target == turn_key:
                    del self._aliases[alias]
            if turn.chat_id and self._by_chat.get(turn.chat_id) == turn_key:
                del self._by_chat[turn.chat_id]
            if turn.session_key and self._by_session.get(turn.session_key) == turn_key:
                del self._by_session[turn.session_key]
        METRICS.incr("turn.removed")

    def prune(self) -> int:
        with self._lock:
            return self._prune_locked()

    def _prune_locked(self) -> int:
        """回收超时 turn。调用方须持锁."""
        ttl = self._cfg.turn_ttl_sec
        now = time.time()
        stale = [key for key, turn in self._turns.items() if now - turn.updated_at > ttl]
        for key in stale:
            turn = self._turns.get(key)
            if turn is not None and not turn.state.is_terminal:
                logger.warning("回收超时未完成的 turn: %s state=%s", key[:12], turn.state)
                METRICS.incr("turn.pruned_active")
                self._defer_finalize_locked(turn, REASON_EXPIRED)
            self._remove_locked(key)
        return len(stale)

    def _enforce_capacity_locked(self) -> None:
        """LRU 淘汰。调用方须持锁."""
        limit = self._cfg.max_turns
        while len(self._turns) > limit:
            oldest_key, oldest = next(iter(self._turns.items()))
            if not oldest.state.is_terminal:
                logger.warning("turn 数超上限，淘汰未完成的 turn: %s", oldest_key[:12])
                METRICS.incr("turn.evicted_active")
                self._defer_finalize_locked(oldest, REASON_EVICTED)
            self._remove_locked(oldest_key)

    def _defer_finalize_locked(self, turn: Turn, reason: str) -> None:
        """把待收卡的活跃 turn 入队。调用方须持锁.

        队列本身不能无界：极端情况下（飞书持续不可达 + 高并发）收卡协程可能
        排不过来，此时丢最旧的一条并计数，保证注册表不会因为「防泄漏」的机制
        反而泄漏。
        """
        self._pending_finalize.append((turn, reason))
        if len(self._pending_finalize) > PENDING_FINALIZE_LIMIT:
            del self._pending_finalize[0]
            METRICS.incr("turn.pending_finalize_dropped")

    def take_pending_finalize(self) -> list[tuple[Turn, str]]:
        """取走待收卡队列（取走即清空），返回 ``(turn, reason)`` 列表.

        由编排层在 :meth:`create` 与 :meth:`prune` 之后调用。注册表处于 L2
        领域层，不能自己发起飞书调用，因此只负责「记下谁需要收尾」。
        """
        with self._lock:
            if not self._pending_finalize:
                return []
            items = self._pending_finalize
            self._pending_finalize = []
            return items

    def _remove_locked(self, turn_key: str) -> None:
        turn = self._turns.pop(turn_key, None)
        if turn is None:
            return
        for alias, target in list(self._aliases.items()):
            if target == turn_key:
                del self._aliases[alias]
        if turn.chat_id and self._by_chat.get(turn.chat_id) == turn_key:
            del self._by_chat[turn.chat_id]
        if turn.session_key and self._by_session.get(turn.session_key) == turn_key:
            del self._by_session[turn.session_key]

    # ── 观测 ──────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_state: dict[str, int] = {}
            for turn in self._turns.values():
                by_state[turn.state.value] = by_state.get(turn.state.value, 0) + 1
            return {
                "total": len(self._turns),
                "aliases": len(self._aliases),
                "by_state": by_state,
            }

    def active_turns(self) -> list[Turn]:
        with self._lock:
            return [turn for turn in self._turns.values() if not turn.state.is_terminal]

    def all_turns(self) -> list[Turn]:
        with self._lock:
            return list(self._turns.values())

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
            self._aliases.clear()
            self._by_chat.clear()
            self._by_session.clear()
            self._pending_finalize.clear()


__all__ = [
    "PENDING_FINALIZE_LIMIT",
    "TurnRegistry",
    "TurnState",
]

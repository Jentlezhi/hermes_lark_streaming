"""事件模型 — 所有消息源归一化后的统一表示.

这是 L1 与 L2 之间的契约。L2 及以上只认这里定义的 ``EventKind``，
不认 Hermes 的任何符号，因此 Hermes 升级的影响半径被限制在 L0/L1。

**扩展契约**：新增一种消息类型 = 新增一个 EventKind + 一个 dispatcher handler
+ 一个渲染分支。L0 与 L4 不需要改动。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    """事件类型全集.

    命名规则：``<主体>.<动作>``，主体用单数。
    """

    # ── Turn 生命周期 ──
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TURN_ABORTED = "turn.aborted"
    TURN_INTERRUPTED = "turn.interrupted"

    # ── 流式内容 ──
    ANSWER_DELTA = "answer.delta"
    REASONING_DELTA = "reasoning.delta"

    # ── 工具调用 ──
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"

    # ── 游离消息收纳（本插件的核心增量能力）──
    NOTICE = "notice"  # 状态提示：压缩、重试、限流、工作轮、额度
    REVIEW = "review"  # 自我改进 / 记忆更新通知

    # ── 交互 ──
    CLARIFY_OPENED = "clarify.opened"
    CLARIFY_CLOSED = "clarify.closed"
    APPROVAL_OPENED = "approval.opened"
    APPROVAL_CLOSED = "approval.closed"

    # ── 独立卡片（不属于任何 turn）──
    CRON_DELIVERED = "cron.delivered"
    BACKGROUND_DONE = "background.done"


#: 会让 turn 进入终态的事件
TERMINAL_KINDS = frozenset(
    {
        EventKind.TURN_COMPLETED,
        EventKind.TURN_FAILED,
        EventKind.TURN_ABORTED,
    }
)

#: 会让 turn 进入等待态（暂停 flush 但不封卡）的事件
WAITING_KINDS = frozenset({EventKind.CLARIFY_OPENED, EventKind.APPROVAL_OPENED})

#: 会让 turn 从等待态恢复的事件
RESUME_KINDS = frozenset({EventKind.CLARIFY_CLOSED, EventKind.APPROVAL_CLOSED})

#: 不归属任何 turn、独立成卡的事件
STANDALONE_KINDS = frozenset({EventKind.CRON_DELIVERED, EventKind.BACKGROUND_DONE})


class NoticeLevel(StrEnum):
    """提示级别，决定渲染时的颜色与图标."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """统一事件.

    ``payload`` 用普通 dict 而非强类型子类，是刻意的取舍：事件跨越 Hermes
    与插件的边界，字段随 Hermes 版本浮动，强类型会让新增字段变成破坏性改动。
    取值方一律用 ``get`` 并做类型校验。
    """

    kind: EventKind
    turn_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    # ── 便捷取值器（统一做类型校验，避免各调用点重复防御）──

    def text(self, key: str = "text", default: str = "") -> str:
        value = self.payload.get(key)
        return value if isinstance(value, str) else default

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.payload.get(key)
        return value if isinstance(value, bool) else default

    def number(self, key: str, default: float = 0.0) -> float:
        value = self.payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(value)

    def mapping(self, key: str) -> dict[str, Any]:
        value = self.payload.get(key)
        return value if isinstance(value, dict) else {}

    @property
    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS

    @property
    def is_standalone(self) -> bool:
        return self.kind in STANDALONE_KINDS


def make_event(kind: EventKind, turn_key: str, **payload: Any) -> StreamEvent:
    """构造事件的便捷入口，避免调用点手写 dict."""
    return StreamEvent(kind=kind, turn_key=turn_key, payload=dict(payload))

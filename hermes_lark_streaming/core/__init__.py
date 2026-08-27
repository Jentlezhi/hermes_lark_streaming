"""L2 领域层 — 与飞书无关的纯逻辑."""

from __future__ import annotations

from .registry import TurnRegistry
from .segments import (
    InteractionKind,
    InteractionState,
    InteractionStatus,
    NoticeItem,
    Segment,
    SegmentState,
    SegmentType,
)
from .tooltrack import ToolDisplayStep, ToolTracker
from .turn import (
    REASON_EVICTED,
    REASON_EXPIRED,
    REASON_INTERRUPTED,
    REASON_STOPPED,
    REASON_TIMEOUT,
    TIMEOUT_REASONS,
    Delivery,
    Turn,
    TurnState,
)

__all__ = [
    "REASON_EVICTED",
    "REASON_EXPIRED",
    "REASON_INTERRUPTED",
    "REASON_STOPPED",
    "REASON_TIMEOUT",
    "TIMEOUT_REASONS",
    "Delivery",
    "InteractionKind",
    "InteractionState",
    "InteractionStatus",
    "NoticeItem",
    "Segment",
    "SegmentState",
    "SegmentType",
    "ToolDisplayStep",
    "ToolTracker",
    "Turn",
    "TurnRegistry",
    "TurnState",
]

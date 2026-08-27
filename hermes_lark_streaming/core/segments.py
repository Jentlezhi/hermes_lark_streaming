"""Segment 模型 — 单张卡片内的扁平内容段.

**为什么用扁平列表而不是轮次树**：Agent 的真实输出是「思考 → 工具 → 回答 →
再思考 → 再工具」的任意交错，且交错次数不可预知。用轮次树需要推断边界，
边界一旦推断错内容顺序就乱。扁平列表按事件到达顺���追加，永远与真实时序一致。

**六种 Segment**：在参考实现的 reasoning/answer/tool 之外，新增 notice/review/
interaction 三种，这是「游离消息收进卡片」的数据落点。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from ..events import NoticeLevel


class SegmentType(StrEnum):
    REASONING = "reasoning"
    ANSWER = "answer"
    TOOL = "tool"
    NOTICE = "notice"
    REVIEW = "review"
    INTERACTION = "interaction"


class InteractionKind(StrEnum):
    CLARIFY = "clarify"
    APPROVAL = "approval"


class InteractionStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    TIMEOUT = "timeout"
    FAILED = "failed"


#: 单个 NOTICE/REVIEW segment 最多聚合的条目数，超出后折叠计数
MAX_NOTICE_ITEMS = 64
#: 单个 Segment 文本上限，超出后由 SegmentState 切分为新 Segment
MAX_SEGMENT_CHARS = 32 * 1024


@dataclass(slots=True)
class NoticeItem:
    """一条提示记录."""

    text: str
    level: NoticeLevel = NoticeLevel.INFO
    at: float = field(default_factory=time.time)


@dataclass(slots=True)
class InteractionState:
    """一次交互（澄清 / 审批）的状态快照.

    首版只做状态展示，不承载按钮；``result`` 由 Hermes 原生交互完成后回填，
    卡片据此把 PENDING 更新为 RESOLVED。
    """

    kind: InteractionKind
    title: str = ""
    detail: str = ""
    status: InteractionStatus = InteractionStatus.PENDING
    result: str = ""
    opened_at: float = field(default_factory=time.time)
    elapsed_ms: float = 0.0

    def resolve(self, status: InteractionStatus, result: str = "") -> None:
        self.status = status
        self.result = result
        self.elapsed_ms = (time.time() - self.opened_at) * 1000


class Segment:
    """单个内容段.

    ``created`` 表示元素是否已在飞书卡片上创建；``dirty`` 表示内容有待推送。
    两者共同驱动 flush：未创建走 batch_update 新增，已创建且脏走增量更新。
    """

    __slots__ = (
        "created",
        "dirty",
        "el_id",
        "elapsed_ms",
        "element_estimate",
        "interaction",
        "notices",
        "overflow_count",
        "reasoning_finalized",
        "start_time",
        "text",
        "text_el_id",
        "tool_end_offset",
        "tool_offset",
        "type",
    )

    def __init__(self, seg_type: SegmentType, el_id: str) -> None:
        self.type = seg_type
        self.el_id = el_id
        self.created = False
        self.dirty = True
        self.element_estimate = 0

        # 文本类（reasoning / answer）
        self.text = ""
        self.text_el_id = ""

        # 工具类：在全局 step 列表中的 [tool_offset, tool_end_offset) 区间
        # tool_end_offset == 0 表示尚未终结（仍在追加）
        self.tool_offset = 0
        self.tool_end_offset = 0

        # 计时
        self.start_time = 0.0
        self.elapsed_ms = 0.0
        self.reasoning_finalized = False

        # 提示类（notice / review）
        self.notices: list[NoticeItem] = []
        self.overflow_count = 0

        # 交互类
        self.interaction: InteractionState | None = None

    @property
    def is_text_kind(self) -> bool:
        return self.type in (SegmentType.REASONING, SegmentType.ANSWER)

    @property
    def is_notice_kind(self) -> bool:
        return self.type in (SegmentType.NOTICE, SegmentType.REVIEW)

    def add_notice(self, item: NoticeItem) -> None:
        """追加提示条目；超出上限后只累加折叠计数，防止元素无界增长."""
        if len(self.notices) >= MAX_NOTICE_ITEMS:
            self.overflow_count += 1
        else:
            self.notices.append(item)
        self.dirty = True


class SegmentState:
    """一张卡片的 Segment 列表管理器.

    纯数据类，不含任何 IO 与飞书概念，可独立单测。
    """

    __slots__ = ("_counter", "segments")

    def __init__(self) -> None:
        self._counter = 0
        self.segments: list[Segment] = []

    # ── 内部构造 ──────────────────────────────────────────────────

    def _next_id(self) -> int:
        value = self._counter
        self._counter += 1
        return value

    def _last(self) -> Segment | None:
        return self.segments[-1] if self.segments else None

    def _finalize_prev_reasoning(self, now: float) -> None:
        """终结最近一个未计时的 reasoning，使其显示「思考了 N 秒」."""
        for seg in reversed(self.segments):
            if seg.type == SegmentType.REASONING and seg.start_time and not seg.elapsed_ms:
                seg.elapsed_ms = (now - seg.start_time) * 1000
                return

    def _new(self, seg_type: SegmentType) -> Segment:
        index = self._next_id()
        if seg_type == SegmentType.REASONING:
            seg = Segment(seg_type, f"reasoning_{index}_panel")
            seg.text_el_id = f"reasoning_{index}_text"
        else:
            seg = Segment(seg_type, f"{seg_type.value}_{index}")
        seg.start_time = time.time()
        if seg_type != SegmentType.REASONING:
            self._finalize_prev_reasoning(seg.start_time)
        self.segments.append(seg)
        return seg

    # ── 内容追加 ──────────────────────────────────────────────────

    def append_reasoning(self, text: str) -> Segment:
        last = self._last()
        if last is not None and last.type == SegmentType.REASONING and len(last.text) < MAX_SEGMENT_CHARS:
            last.text += text
            last.dirty = True
            return last
        seg = self._new(SegmentType.REASONING)
        seg.text = text
        return seg

    def append_answer(self, text: str) -> Segment:
        last = self._last()
        if last is not None and last.type == SegmentType.ANSWER and len(last.text) < MAX_SEGMENT_CHARS:
            last.text += text
            last.dirty = True
            return last
        seg = self._new(SegmentType.ANSWER)
        seg.text = text
        return seg

    def append_notice(self, item: NoticeItem, seg_type: SegmentType) -> Segment:
        """追加提示；与上一段同类则合并，避免每条提示各占一个卡片元素."""
        last = self._last()
        if last is not None and last.type == seg_type:
            last.add_notice(item)
            return last
        seg = self._new(seg_type)
        seg.add_notice(item)
        return seg

    def open_interaction(self, state: InteractionState) -> Segment:
        seg = self._new(SegmentType.INTERACTION)
        seg.interaction = state
        return seg

    def resolve_interaction(
        self,
        kind: InteractionKind,
        status: InteractionStatus,
        result: str = "",
    ) -> Segment | None:
        """回填最近一个同类未决交互的结果."""
        for seg in reversed(self.segments):
            interaction = seg.interaction
            if (
                seg.type == SegmentType.INTERACTION
                and interaction is not None
                and interaction.kind == kind
                and interaction.status == InteractionStatus.PENDING
            ):
                interaction.resolve(status, result)
                seg.dirty = True
                return seg
        return None

    def on_tool_event(self, total_steps: int) -> Segment | None:
        """工具步数变化.

        同类相邻则只标脏；否则终结前一个工具段并新建，保证工具面板与
        其他内容的先后顺序真实反映执行过程。
        """
        if total_steps <= 0:
            return None
        last = self._last()
        if last is not None and last.type == SegmentType.TOOL:
            last.dirty = True
            return last

        # 终结此前仍开放的工具段，把区间右边界钉在当前位置
        for seg in reversed(self.segments):
            if seg.type == SegmentType.TOOL and seg.tool_end_offset == 0:
                seg.tool_end_offset = total_steps - 1
                seg.dirty = True
                break

        seg = self._new(SegmentType.TOOL)
        seg.tool_offset = total_steps - 1
        return seg

    # ── 拆分与终结 ────────────────────────────────────────────────

    def split_tool_segment(self, index: int, split_offset: int) -> Segment:
        """在 step 边界拆分工具段，返回承接后续 step 的新段.

        用于卡片元素接近上限时，把一个过大的工具面板切成两半。
        """
        seg = self.segments[index]
        new_seg = Segment(SegmentType.TOOL, f"tool_{self._next_id()}")
        new_seg.tool_offset = split_offset
        new_seg.tool_end_offset = seg.tool_end_offset
        new_seg.start_time = seg.start_time
        seg.tool_end_offset = split_offset
        seg.dirty = True
        self.segments.insert(index + 1, new_seg)
        return new_seg

    def finalize(self, total_tool_steps: int) -> None:
        """终态收尾：钉住最后一个工具段的右边界，补算最后一段思考耗时."""
        now = time.time()
        for seg in reversed(self.segments):
            if seg.type == SegmentType.TOOL and seg.tool_end_offset == 0:
                seg.tool_end_offset = total_tool_steps
                break
        self._finalize_prev_reasoning(now)

        # 未决交互在 turn 结束时一律标为超时，避免卡片永远停在「等待中」
        for seg in self.segments:
            interaction = seg.interaction
            if interaction is not None and interaction.status == InteractionStatus.PENDING:
                interaction.resolve(InteractionStatus.TIMEOUT)
                seg.dirty = True

    # ── 查询 ──────────────────────────────────────────────────────

    @property
    def has_dirty(self) -> bool:
        return any(seg.dirty for seg in self.segments)

    @property
    def has_answer(self) -> bool:
        return any(seg.type == SegmentType.ANSWER and seg.text.strip() for seg in self.segments)

    def last_text(self) -> str:
        """最近一段有内容的正文，用于生成会话列表摘要."""
        for seg in reversed(self.segments):
            if seg.type == SegmentType.ANSWER and seg.text.strip():
                return seg.text
        for seg in reversed(self.segments):
            if seg.type == SegmentType.REASONING and seg.text.strip():
                return seg.text
        return ""

    def pending_interaction(self) -> InteractionState | None:
        for seg in reversed(self.segments):
            interaction = seg.interaction
            if interaction is not None and interaction.status == InteractionStatus.PENDING:
                return interaction
        return None

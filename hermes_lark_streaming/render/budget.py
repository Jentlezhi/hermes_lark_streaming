"""元素预算与拆卡决策.

飞书单张卡片元素数硬上限 200。超限时 CardKit 返回 230099/子码 11310，
且**整次更新失败**——不是截断而是全丢。因此必须在发送前预估，接近阈值
时主动拆卡。

估算原则：**宁可高估不可低估**。高估只是提前拆卡（多一张卡片），
低估会导致更新整体失败（内容丢失）。
"""

from __future__ import annotations

from typing import Any

from .. import icons
from ..core.segments import Segment, SegmentType
from ..core.tooltrack import ToolDisplayStep
from . import elements

#: footer 预留（分隔线 + 文本）
FOOTER_RESERVE = 2

# 各类 segment 的元素占用估算
_REASONING_ELEMENTS = 4  # panel + title + icon + markdown
_ANSWER_ELEMENTS = 1
_NOTICE_ELEMENTS = 1
_INTERACTION_ELEMENTS = 1
_TOOL_PANEL_BASE = 3  # panel + title + icon
_TOOL_STEP_TITLE = 3  # div + icon + text
_TOOL_STEP_DETAIL = 2
_TOOL_STEP_OUTPUT = 2


def tool_segment_end(seg: Segment, all_steps: list[ToolDisplayStep]) -> int:
    """工具段的右边界。0 表示仍在追加，此时取当前全部步数."""
    return seg.tool_end_offset if seg.tool_end_offset else len(all_steps)


def estimate_tool_elements(start: int, end: int, all_steps: list[ToolDisplayStep]) -> int:
    count = _TOOL_PANEL_BASE
    for step in all_steps[start:end]:
        count += _TOOL_STEP_TITLE
        if step.get("detail"):
            count += _TOOL_STEP_DETAIL
        if step.get("result_block") or step.get("error_block"):
            count += _TOOL_STEP_OUTPUT
    return count


def estimate_segment(seg: Segment, all_steps: list[ToolDisplayStep]) -> int:
    if seg.type == SegmentType.REASONING:
        return _REASONING_ELEMENTS
    if seg.type == SegmentType.ANSWER:
        return _ANSWER_ELEMENTS
    if seg.type in (SegmentType.NOTICE, SegmentType.REVIEW):
        return _NOTICE_ELEMENTS
    if seg.type == SegmentType.INTERACTION:
        return _INTERACTION_ELEMENTS
    if seg.type == SegmentType.TOOL:
        return estimate_tool_elements(seg.tool_offset, tool_segment_end(seg, all_steps), all_steps)
    return 1


def find_tool_split_offset(
    *,
    base_count: int,
    seg: Segment,
    all_steps: list[ToolDisplayStep],
    threshold: int,
) -> int | None:
    """在工具步边界找拆分点，让当前卡尽可能多装几步.

    从后往前试，返回第一个能装下的边界；一步都装不下则返回 None，
    由调用方改为整段拆到新卡。
    """
    start = seg.tool_offset
    end = tool_segment_end(seg, all_steps)
    if end - start <= 1:
        return None
    for offset in range(end - 1, start, -1):
        if base_count + estimate_tool_elements(start, offset, all_steps) + FOOTER_RESERVE <= threshold:
            return offset
    return None


def exceeds(current: int, incoming: int, threshold: int) -> bool:
    """加上新元素后是否会超预算."""
    return current + incoming + FOOTER_RESERVE > threshold


# ── CardKit batch action 构造 ─────────────────────────────────────


def add_segment_action(
    seg: Segment,
    all_steps: list[ToolDisplayStep],
    *,
    text_size: str,
    expanded: bool,
    marks: icons.IconSet = None,
) -> dict[str, Any]:
    """构造「新增元素」action.

    统一插在 loading 元素之前，保证加载指示始终在最后。
    """
    return {
        "action": "add_elements",
        "params": {
            "type": "insert_before",
            "target_element_id": elements.LOADING_ELEMENT_ID,
            "elements": [
                render_segment(seg, all_steps, text_size=text_size, expanded=expanded, marks=marks)
            ],
        },
    }


def render_segment(
    seg: Segment,
    all_steps: list[ToolDisplayStep],
    *,
    text_size: str,
    expanded: bool,
    for_streaming: bool = True,
    marks: icons.IconSet = None,
) -> dict[str, Any]:
    """把 Segment 渲染为卡片元素.

    ``for_streaming`` 为 True 时保留 element_id（后续要增量更新），
    终态全量重建时不需要 element_id，可减小卡片体积。
    """
    element_id = seg.el_id if for_streaming else None

    if seg.type == SegmentType.REASONING:
        return elements.reasoning_panel(
            seg.text if not for_streaming else " ",
            seg.elapsed_ms,
            expanded=expanded,
            element_id=element_id,
            text_element_id=seg.text_el_id if for_streaming else None,
            marks=marks,
        )

    if seg.type == SegmentType.ANSWER:
        if for_streaming:
            return elements.streaming_text("", element_id=seg.el_id, text_size=text_size)
        return elements.static_text(seg.text, text_size=text_size)

    if seg.type == SegmentType.TOOL:
        start = seg.tool_offset
        end = tool_segment_end(seg, all_steps)
        return elements.tool_panel(
            all_steps[start:end], expanded=expanded, element_id=element_id, marks=marks
        )

    if seg.type in (SegmentType.NOTICE, SegmentType.REVIEW):
        return elements.notice_block(
            seg.notices,
            is_review=seg.type == SegmentType.REVIEW,
            element_id=element_id,
            overflow=seg.overflow_count,
            marks=marks,
        )

    if seg.type == SegmentType.INTERACTION and seg.interaction is not None:
        return elements.interaction_block(seg.interaction, element_id=element_id, marks=marks)

    # 未知类型不应出现；返回占位而非抛错，避免单个坏段毁掉整张卡
    return elements.static_text(" ", text_size=text_size)


def update_element_action(element_id: str, partial: dict[str, Any]) -> dict[str, Any]:
    """构造「局部更新元素」action."""
    return {
        "action": "partial_update_element",
        "params": {"element_id": element_id, "partial_element": partial},
    }


def tool_update_action(
    *,
    element_id: str,
    steps: list[ToolDisplayStep],
    expanded: bool,
    marks: icons.IconSet = None,
) -> dict[str, Any]:
    panel = elements.tool_panel(steps, expanded=expanded, marks=marks)
    return update_element_action(
        element_id,
        {"elements": panel["elements"], "header": panel["header"]},
    )


def notice_update_action(seg: Segment, *, marks: icons.IconSet = None) -> dict[str, Any]:
    block = elements.notice_block(
        seg.notices,
        is_review=seg.type == SegmentType.REVIEW,
        overflow=seg.overflow_count,
        marks=marks,
    )
    return update_element_action(seg.el_id, {"content": block["content"]})


def interaction_update_action(seg: Segment, *, marks: icons.IconSet = None) -> dict[str, Any] | None:
    if seg.interaction is None:
        return None
    block = elements.interaction_block(seg.interaction, marks=marks)
    return update_element_action(seg.el_id, {"content": block["content"]})


def reasoning_finalize_action(seg: Segment, *, marks: icons.IconSet = None) -> dict[str, Any]:
    return update_element_action(seg.el_id, elements.reasoning_title_patch(seg.elapsed_ms, marks=marks))

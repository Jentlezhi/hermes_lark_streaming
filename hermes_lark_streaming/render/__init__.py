"""L3 渲染层 — Segment 转 CardKit JSON.

本层是唯一产出飞书卡片结构的地方，上层不拼 JSON。
"""

from __future__ import annotations

from .budget import (
    FOOTER_RESERVE,
    add_segment_action,
    estimate_segment,
    estimate_tool_elements,
    exceeds,
    find_tool_split_offset,
    interaction_update_action,
    notice_update_action,
    reasoning_finalize_action,
    render_segment,
    tool_segment_end,
    tool_update_action,
)
from .card import (
    build_archived_card,
    build_background_card,
    build_complete_card,
    build_cron_card,
    build_streaming_card,
)
from .elements import LOADING_ELEMENT_ID
from .markdown import (
    downgrade_wide_tables,
    find_image_refs,
    normalize_markdown,
    replace_image_refs,
    split_long_text,
)

__all__ = [
    "FOOTER_RESERVE",
    "LOADING_ELEMENT_ID",
    "add_segment_action",
    "build_archived_card",
    "build_background_card",
    "build_complete_card",
    "build_cron_card",
    "build_streaming_card",
    "downgrade_wide_tables",
    "estimate_segment",
    "estimate_tool_elements",
    "exceeds",
    "find_image_refs",
    "find_tool_split_offset",
    "interaction_update_action",
    "normalize_markdown",
    "notice_update_action",
    "reasoning_finalize_action",
    "render_segment",
    "replace_image_refs",
    "split_long_text",
    "tool_segment_end",
    "tool_update_action",
]

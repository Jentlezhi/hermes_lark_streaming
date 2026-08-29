"""卡片组装 — Segment 列表转完整 CardKit 卡片.

**打字机效果的三个必要条件**（缺一则退化为整块跳变）：

1. 卡片经 ``cardkit_create`` 实体化，消息只引用 card_id
2. ``config.streaming_mode = true`` 且带 ``streaming_config``
3. 文本更新走 ``cardkit_stream_element`` 单元素增量，而非整卡替换

本模块负责第 2 条。``streaming_config.print_frequency_ms`` 决定**客户端**
以多快的节奏逐字播放——打字机是飞书客户端本地插值渲染的，与服务端推送
频率解耦。这也是为什么服务端 100ms 推一次仍能得到平滑打字。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import icons
from ..core.segments import Segment, SegmentType
from ..core.tooltrack import ToolDisplayStep
from ..core.turn import REASON_INTERRUPTED, TIMEOUT_REASONS
from . import elements, i18n
from .budget import render_segment
from .markdown import downgrade_wide_tables, normalize_markdown, split_long_text

#: 客户端逐字播放节奏（毫秒/次）。15ms 接近人眼舒适的打字速度
PRINT_FREQUENCY_MS = 15
#: 每次播放的字符数
PRINT_STEP = 1


def _streaming_config() -> dict[str, Any]:
    return {
        "print_frequency_ms": {"default": PRINT_FREQUENCY_MS},
        "print_step": {"default": PRINT_STEP},
        "print_strategy": "fast",
    }


def _base_config(*, width_mode: str, summary: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "width_mode": width_mode,
        "locales": i18n.LOCALES,
    }
    if summary:
        # 会话列表预览文本：跨会话状态可见性的唯一原生通道
        config["summary"] = {"content": summary}
    return config


def build_streaming_card(
    *,
    header_enabled: bool,
    width_mode: str,
    summary: str = "",
) -> dict[str, Any]:
    """流式占位卡.

    只含一个 loading 元素：后续内容全部通过 ``add_elements`` 插在它之前。
    这样卡片一创建就能立刻发出，用户马上看到响应，不必等首个 delta。
    """
    config = _base_config(width_mode=width_mode, summary=summary or i18n.zh("processing"))
    config["streaming_mode"] = True
    config["streaming_config"] = _streaming_config()

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": config,
        "body": {"elements": [elements.loading_element()]},
    }
    if header_enabled:
        card["header"] = elements.header("streaming")
    return card


def build_complete_card(
    *,
    segments: list[Segment],
    all_tool_steps: list[ToolDisplayStep],
    footer_data: dict[str, Any] | None = None,
    footer_fields: list[list[str]],
    footer_show_label: bool,
    footer_enabled: bool,
    footer_text_size: str,
    body_text_size: str,
    panel_expanded: bool,
    show_tool_use: bool,
    header_enabled: bool,
    width_mode: str,
    summary: str = "",
    is_error: bool = False,
    is_aborted: bool = False,
    abort_reason: str = "",
    tool_dropped: int = 0,
    marks: icons.IconSet = None,
) -> dict[str, Any]:
    """终态卡片 — 按 segment 顺序全量重建.

    终态不再需要 element_id（不会再增量更新），去掉可减小卡片体积；
    长回答在这里做切分与表格降级，流式过程中不做以免频繁重排。
    """
    body: list[dict[str, Any]] = []
    has_content = False

    for seg in segments:
        if seg.type == SegmentType.TOOL and not show_tool_use:
            continue

        if seg.type == SegmentType.ANSWER:
            text = normalize_markdown(seg.text)
            if not text:
                continue
            has_content = True
            for chunk in split_long_text(downgrade_wide_tables(text)):
                if chunk.strip():
                    body.append(elements.static_text(chunk, text_size=body_text_size))
            continue

        if seg.type == SegmentType.REASONING and not seg.text.strip():
            continue
        if seg.type == SegmentType.TOOL:
            start = seg.tool_offset
            end = seg.tool_end_offset if seg.tool_end_offset else len(all_tool_steps)
            if not all_tool_steps[start:end]:
                continue
        if seg.type in (SegmentType.NOTICE, SegmentType.REVIEW) and not seg.notices:
            continue

        has_content = True
        body.append(
            render_segment(
                seg,
                all_tool_steps,
                text_size=body_text_size,
                expanded=panel_expanded,
                for_streaming=False,
                marks=marks,
            )
        )

    if not has_content:
        body.append(elements.static_text(i18n.zh("done"), text_size=body_text_size))

    if tool_dropped > 0:
        body.append(
            elements.static_text(
                f"<font color='grey'>{i18n.zh('tool_dropped').format(tool_dropped)}</font>",
                text_size="notation",
            )
        )

    if footer_enabled:
        body.extend(
            elements.footer_elements(
                footer_data,
                fields=footer_fields,
                show_label=footer_show_label,
                text_size=footer_text_size,
                is_error=is_error,
                is_aborted=is_aborted,
                abort_reason=abort_reason,
                marks=marks,
            )
        )

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": _base_config(width_mode=width_mode, summary=summary),
        "body": {"elements": body},
    }
    if header_enabled:
        if is_error:
            status = "error"
        elif is_aborted:
            # 三档语义各有配色：用户主动停止（红）、被新消息接续（橙）、
            # 插件超时收尾（橙）。混成一档会让用户分不清「谁停的」
            if abort_reason in TIMEOUT_REASONS:
                status = "timeout"
            elif abort_reason == REASON_INTERRUPTED:
                status = "interrupted"
            else:
                status = "stopped"
        else:
            status = "completed"
        card["header"] = elements.header(status)
    return card


def build_archived_card(
    *,
    segments: list[Segment],
    all_tool_steps: list[ToolDisplayStep],
    body_text_size: str,
    panel_expanded: bool,
    show_tool_use: bool,
    width_mode: str,
    marks: icons.IconSet = None,
) -> dict[str, Any]:
    """拆卡时封存旧卡.

    不带 footer（footer 只出现在最后一张卡），summary 明确标注已归档，
    避免会话列表显示旧卡的过期状态而误导用户。
    """
    return build_complete_card(
        segments=segments,
        all_tool_steps=all_tool_steps,
        footer_data=None,
        footer_fields=[],
        footer_show_label=False,
        footer_enabled=False,
        footer_text_size="notation",
        body_text_size=body_text_size,
        panel_expanded=panel_expanded,
        show_tool_use=show_tool_use,
        header_enabled=False,
        width_mode=width_mode,
        summary=i18n.zh("card_archived"),
        marks=marks,
    )


# ── 独立卡片（不属于任何 turn）─────────────────────────────────────


def build_cron_card(
    content: str,
    *,
    task_name: str = "",
    run_time: str = "",
    width_mode: str = "default",
    marks: icons.IconSet = None,
) -> dict[str, Any]:
    """定时任务结果卡.

    cron 不属于任何用户 turn，没有宿主卡片可收敛，因此独立成卡。
    但仍用完整卡片而非灰色文本，保证 Markdown 渲染与来源标识。
    """
    title_parts = [part for part in (task_name, _format_time(run_time)) if part]
    header_title = " · ".join(title_parts) if title_parts else i18n.zh("cron_title")
    return _build_standalone_card(
        content=content,
        header_title=f"{icons.with_space(marks, 'cron')}{header_title}",
        template="blue",
        width_mode=width_mode,
    )


def build_background_card(
    preview: str,
    content: str,
    *,
    width_mode: str = "default",
    marks: icons.IconSet = None,
) -> dict[str, Any]:
    """后台任务完成卡."""
    title = f"{icons.with_space(marks, 'completed')}{i18n.zh('background_title')}"
    if preview.strip():
        title = f"{title}：{preview.strip()}"
    return _build_standalone_card(
        content=content,
        header_title=title,
        template="green",
        width_mode=width_mode,
    )


def _build_standalone_card(
    *,
    content: str,
    header_title: str,
    template: str,
    width_mode: str,
) -> dict[str, Any]:
    body_text = normalize_markdown(content) or i18n.zh("no_response")
    summary = " ".join(body_text.replace("```", " ").split())[:120]

    body: list[dict[str, Any]] = []
    for chunk in split_long_text(downgrade_wide_tables(body_text)):
        if chunk.strip():
            body.append(elements.static_text(chunk))

    return {
        "schema": "2.0",
        "config": _base_config(width_mode=width_mode, summary=summary),
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": template,
        },
        "body": {"elements": body},
    }


def _format_time(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return value

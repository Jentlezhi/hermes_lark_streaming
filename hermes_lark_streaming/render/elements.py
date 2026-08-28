"""卡片元素构建原语.

本模块是**唯一**产出飞书 schema 2.0 元素结构的地方。上层只描述「要渲染什么」，
不拼 JSON；换平台时只需替换本模块与 card.py。

新增一种展示样式 = 在这里加一个原语函数，不影响其他层。
"""

from __future__ import annotations

from typing import Any, Final

from ..core.segments import InteractionKind, InteractionStatus, InteractionState, NoticeItem
from ..core.tooltrack import ToolDisplayStep
from ..core.turn import REASON_INTERRUPTED, TIMEOUT_REASONS
from ..events import NoticeLevel
from . import i18n
from .markdown import code_block, escape_inline, escape_tags

# ── 固定元素 id ───────────────────────────────────────────────────
# loading 元素是 add_elements 的插入锚点：新元素永远插在它之前，
# 保证「加载指示」始终位于卡片末尾。
LOADING_ELEMENT_ID: Final[str] = "hls_loading"

#: 飞书内置的加载动画图片 key
_LOADING_IMG_KEY: Final[str] = "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg"

#: 语义图标 -> 飞书 standard_icon token
_ICON_TOKENS: Final[dict[str, str]] = {
    "skill": "app-default_outlined",
    "read": "file-link-text_outlined",
    "edit": "edit_outlined",
    "search": "search_outlined",
    "web": "language_outlined",
    "grep": "doc-search_outlined",
    "folder": "folder_outlined",
    "terminal": "setting_outlined",
    "browser": "browser-mac_outlined",
    "agent": "robot_outlined",
    "check": "list-check_outlined",
    "report": "report_outlined",
    "chat": "chat_outlined",
    "list": "list-check_outlined",
    "tool": "setting-inter_outlined",
}

#: 工具状态 -> (文案 key, 颜色)
_TOOL_STATUS_STYLE: Final[dict[str, tuple[str, str]]] = {
    "running": ("tool_running", "turquoise"),
    "success": ("tool_success", "green"),
    "error": ("tool_error", "red"),
}

#: 提示级别 -> (图标 token, 颜色)
_NOTICE_STYLE: Final[dict[NoticeLevel, tuple[str, str]]] = {
    NoticeLevel.INFO: ("info_outlined", "grey"),
    NoticeLevel.WARNING: ("warning_outlined", "orange"),
    NoticeLevel.ERROR: ("error_outlined", "red"),
}

#: 卡片状态 -> (header 模板色, 文案 key)
_HEADER_STYLE: Final[dict[str, tuple[str, str]]] = {
    "streaming": ("blue", "processing"),
    "waiting": ("orange", "status_waiting"),
    "completed": ("green", "status_completed"),
    "error": ("red", "status_error"),
    "stopped": ("red", "status_stopped"),
    # 被新消息接续不是错误，用橙色区别于用户主动 /stop 的红色
    "interrupted": ("orange", "status_interrupted"),
    # 超时收尾同样不是错误：任务可能仍在跑，只是卡片不再跟踪
    "timeout": ("orange", "status_timeout"),
}


def icon_token(icon_key: str) -> str:
    return _ICON_TOKENS.get(icon_key, _ICON_TOKENS["tool"])


# ── 基础元素 ──────────────────────────────────────────────────────


def loading_element() -> dict[str, Any]:
    """末尾加载指示，同时充当新元素的插入锚点."""
    return {
        "tag": "markdown",
        "content": " ",
        "icon": {"tag": "custom_icon", "img_key": _LOADING_IMG_KEY, "size": "16px 16px"},
        "element_id": LOADING_ELEMENT_ID,
    }


def streaming_text(
    content: str = "",
    *,
    element_id: str,
    text_size: str = "normal_v2",
) -> dict[str, Any]:
    """流式文本元素.

    必须带 ``element_id``：CardKit 的增量更新接口按 element_id 定位，
    这是打字机效果的前提。
    """
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": text_size,
        "margin": "0px 0px 0px 0px",
        "element_id": element_id,
    }


def static_text(content: str, *, text_size: str = "normal_v2") -> dict[str, Any]:
    return {"tag": "markdown", "content": content, "text_size": text_size}


def divider() -> dict[str, Any]:
    return {"tag": "hr"}


def collapsible_panel(
    *,
    title_element: dict[str, Any],
    elements: list[dict[str, Any]],
    expanded: bool,
    element_id: str | None = None,
    vertical_spacing: str = "4px",
) -> dict[str, Any]:
    panel: dict[str, Any] = {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": title_element,
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
                "color": "grey",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": vertical_spacing,
        "padding": "8px 8px 8px 8px",
        "elements": elements,
    }
    if element_id:
        panel["element_id"] = element_id
    return panel


def panel_title(zh_text: str, en_text: str) -> dict[str, Any]:
    return {
        "tag": "plain_text",
        "content": zh_text,
        "i18n_content": i18n.i18n(zh_text, en_text),
        "text_color": "grey",
        "text_size": "notation",
    }


# ── 卡片 header ───────────────────────────────────────────────────


def header(status: str) -> dict[str, Any]:
    template, text_key = _HEADER_STYLE.get(status, _HEADER_STYLE["completed"])
    zh_text, en_text = i18n.t(text_key)
    return {
        "title": {
            "tag": "plain_text",
            "content": zh_text,
            "i18n_content": i18n.i18n(zh_text, en_text),
        },
        "template": template,
    }


# ── 思考面板 ──────────────────────────────────────────────────────


def reasoning_panel(
    text: str,
    elapsed_ms: float = 0.0,
    *,
    expanded: bool = False,
    element_id: str | None = None,
    text_element_id: str | None = None,
) -> dict[str, Any]:
    if elapsed_ms > 0:
        duration = format_elapsed(elapsed_ms)
        zh_label = i18n.zh("thought_for").format(duration)
        en_label = i18n.en("thought_for").format(duration)
    elif not text.strip():
        zh_label, en_label = i18n.t("thinking_panel")
    else:
        zh_label, en_label = i18n.t("thought")

    inner: dict[str, Any] = {"tag": "markdown", "content": text or " ", "text_size": "notation"}
    if text_element_id:
        inner["element_id"] = text_element_id

    return collapsible_panel(
        title_element=panel_title(f"💭 {zh_label}", f"💭 {en_label}"),
        elements=[inner],
        expanded=expanded,
        element_id=element_id,
        vertical_spacing="8px",
    )


def reasoning_title_patch(elapsed_ms: float) -> dict[str, Any]:
    """思考面板终结时只更新标题（把「思考中」改成「思考了 N 秒」）."""
    duration = format_elapsed(elapsed_ms)
    zh_label = i18n.zh("thought_for").format(duration)
    en_label = i18n.en("thought_for").format(duration)
    return {"header": {"title": panel_title(f"💭 {zh_label}", f"💭 {en_label}")}}


# ── 工具面板 ──────────────────────────────────────────────────────


def tool_panel(
    steps: list[ToolDisplayStep],
    *,
    expanded: bool = True,
    element_id: str | None = None,
    dropped: int = 0,
) -> dict[str, Any]:
    zh_parts = [i18n.zh("tool_use")]
    en_parts = [i18n.en("tool_use")]
    if steps:
        zh_parts.append(i18n.zh("steps").format(len(steps)))
        en_parts.append(i18n.en("steps").format(len(steps), "s" if len(steps) > 1 else ""))

    children: list[dict[str, Any]] = []
    for step in steps:
        children.extend(_tool_step_elements(step))
    if dropped > 0:
        children.append(
            {
                "tag": "div",
                "margin": "0px 0px 0px 22px",
                "text": {
                    "tag": "plain_text",
                    "content": i18n.zh("tool_dropped").format(dropped),
                    "i18n_content": i18n.i18n(
                        i18n.zh("tool_dropped").format(dropped),
                        i18n.en("tool_dropped").format(dropped),
                    ),
                    "text_color": "grey",
                    "text_size": "notation",
                },
            }
        )

    return collapsible_panel(
        title_element=panel_title(f"🛠️ {' · '.join(zh_parts)}", f"🛠️ {' · '.join(en_parts)}"),
        elements=children,
        expanded=expanded,
        element_id=element_id,
    )


def _tool_step_elements(step: ToolDisplayStep) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = [_tool_step_title(step)]
    detail = step.get("detail", "").strip()
    if detail:
        elements.append(
            {
                "tag": "div",
                "margin": "0px 0px 0px 22px",
                "text": {
                    "tag": "plain_text",
                    "content": detail,
                    "text_color": "grey",
                    "text_size": "notation",
                },
            }
        )
    output = _tool_step_output(step)
    if output:
        elements.append(output)
    return elements


def _tool_step_title(step: ToolDisplayStep) -> dict[str, Any]:
    status = step.get("status", "running")
    text_key, color = _TOOL_STATUS_STYLE.get(status, ("tool_running", "grey"))
    label = i18n.zh(text_key)
    title = escape_inline(step.get("title") or step.get("name") or "工具")
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": icon_token(step.get("icon_key", "tool")),
            "color": "grey",
        },
        "text": {
            "tag": "lark_md",
            "content": f"**{title}** · <font color='{color}'>{label}</font>",
            "text_size": "notation",
        },
    }


def _tool_step_output(step: ToolDisplayStep) -> dict[str, Any] | None:
    error_block = step.get("error_block")
    result_block = step.get("result_block")

    if error_block:
        label = i18n.zh("tool_error_label")
        body = code_block(error_block.get("content", ""), error_block.get("language", "text"))
    elif result_block:
        label = i18n.zh("tool_result")
        body = code_block(result_block.get("content", ""), result_block.get("language", "json"))
    else:
        return None

    return {
        "tag": "div",
        "margin": "0px 0px 0px 22px",
        "text": {"tag": "lark_md", "content": f"**{label}**\n{body}", "text_size": "notation"},
    }


# ── 提示块（游离消息收纳的渲染出口）────────────────────────────────


def notice_block(
    items: list[NoticeItem],
    *,
    is_review: bool,
    element_id: str | None = None,
    overflow: int = 0,
) -> dict[str, Any]:
    """把收纳来的提示渲染为一个紧凑块.

    多条提示合并进同一个元素，避免每条提示各占一个卡片元素而撑爆预算。
    """
    lines: list[str] = []
    for item in items:
        token, color = _NOTICE_STYLE.get(item.level, _NOTICE_STYLE[NoticeLevel.INFO])
        marker = {"info_outlined": "·", "warning_outlined": "⚠", "error_outlined": "✕"}.get(token, "·")
        # 提示文本可能来自模型（子任务摘要）或 Hermes 状态消息，都是不可信输入。
        # 不转义标签起始符，一个 </font> 会提前闭合配色，而 `a < b` 这种更常见的
        # 写法会让飞书把后面的内容当未知标签吞掉——那是丢内容，不只是变丑
        text = escape_tags(item.text.replace("\n", " ").strip())
        lines.append(f"<font color='{color}'>{marker} {text}</font>")

    if overflow > 0:
        lines.append(f"<font color='grey'>{i18n.zh('notice_overflow').format(overflow)}</font>")

    prefix = "🧠" if is_review else "ℹ️"
    content = f"{prefix} " + "\n".join(lines) if lines else prefix

    element: dict[str, Any] = {
        "tag": "markdown",
        "content": content,
        "text_size": "notation",
    }
    if element_id:
        element["element_id"] = element_id
    return element


# ── 交互块 ────────────────────────────────────────────────────────


def interaction_block(state: InteractionState, *, element_id: str | None = None) -> dict[str, Any]:
    """交互状态块.

    首版只展示状态，不承载按钮——按钮仍由 Hermes 原生卡片提供，
    这里保证「卡片内能看到审批发生过、内容是什么、结果如何」。
    """
    if state.kind == InteractionKind.APPROVAL:
        icon = "🔐"
        pending_key, resolved_key = "approval_pending", "approval_resolved"
    else:
        icon = "❓"
        pending_key, resolved_key = "clarify_pending", "clarify_resolved"

    if state.status == InteractionStatus.PENDING:
        status_text = i18n.zh(pending_key)
        color = "orange"
    elif state.status == InteractionStatus.RESOLVED:
        status_text = i18n.zh(resolved_key)
        color = "green"
    elif state.status == InteractionStatus.TIMEOUT:
        status_text = i18n.zh("interaction_timeout")
        color = "grey"
    else:
        status_text = i18n.zh("status_error")
        color = "red"

    lines = [f"{icon} **{escape_inline(state.title)}** · <font color='{color}'>{status_text}</font>"]
    if state.detail.strip():
        lines.append(f"<font color='grey'>{escape_inline(state.detail.strip())}</font>")
    if state.result.strip():
        lines.append(f"<font color='grey'>→ {escape_inline(state.result.strip())}</font>")

    element: dict[str, Any] = {
        "tag": "markdown",
        "content": "\n".join(lines),
        "text_size": "notation",
    }
    if element_id:
        element["element_id"] = element_id
    return element


# ── Footer ────────────────────────────────────────────────────────


def footer_elements(
    data: dict[str, Any] | None,
    *,
    fields: list[list[str]],
    show_label: bool,
    text_size: str,
    is_error: bool = False,
    is_aborted: bool = False,
    abort_reason: str = "",
) -> list[dict[str, Any]]:
    payload = data or {}
    zh_lines: list[str] = []
    en_lines: list[str] = []

    for row in fields:
        zh_parts: list[str] = []
        en_parts: list[str] = []
        for name in row:
            zh_value, en_value = _footer_field(name, payload, is_error, is_aborted, show_label, abort_reason)
            if zh_value:
                zh_parts.append(zh_value)
                en_parts.append(en_value or zh_value)
        if zh_parts:
            zh_lines.append(" · ".join(zh_parts))
            en_lines.append(" · ".join(en_parts))

    if not zh_lines:
        return []

    zh_content = "\n".join(zh_lines)
    en_content = "\n".join(en_lines)
    if is_error:
        zh_content = f"<font color='red'>{zh_content}</font>"
        en_content = f"<font color='red'>{en_content}</font>"

    return [
        divider(),
        {
            "tag": "markdown",
            "content": zh_content,
            "i18n_content": i18n.i18n(zh_content, en_content),
            "text_size": text_size,
        },
    ]


def _footer_field(
    name: str,
    data: dict[str, Any],
    is_error: bool,
    is_aborted: bool,
    show_label: bool,
    abort_reason: str = "",
) -> tuple[str | None, str | None]:
    if name == "status":
        if is_error:
            return f"❌ {i18n.zh('status_error')}", f"❌ {i18n.en('status_error')}"
        if is_aborted:
            if abort_reason in TIMEOUT_REASONS:
                return f"⏱️ {i18n.zh('status_timeout')}", f"⏱️ {i18n.en('status_timeout')}"
            if abort_reason == REASON_INTERRUPTED:
                return f"⏭️ {i18n.zh('status_interrupted')}", f"⏭️ {i18n.en('status_interrupted')}"
            return f"⏹️ {i18n.zh('status_stopped')}", f"⏹️ {i18n.en('status_stopped')}"
        return f"✅ {i18n.zh('status_completed')}", f"✅ {i18n.en('status_completed')}"

    if name == "elapsed":
        duration = data.get("duration", 0)
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            value = format_elapsed(float(duration) * 1000)
            if show_label:
                return i18n.zh("elapsed").format(value), i18n.en("elapsed").format(value)
            return value, value
        return None, None

    if name == "model":
        value = data.get("model")
        text = str(value) if value else None
        return text, text

    if name == "usage":
        # 订阅额度。查不到时返回 None 让该字段整体消失，而不是显示占位符——
        # 大多数服务商没有额度接口，占位符只会变成永久的噪音
        value = str(data.get("usage") or "").strip()
        if not value:
            return None, None
        if show_label:
            return i18n.zh("usage").format(value), i18n.en("usage").format(value)
        return value, value

    if name == "tokens":
        input_tokens = _safe_int(data.get("input_tokens"))
        output_tokens = _safe_int(data.get("output_tokens"))
        if input_tokens or output_tokens:
            value = f"↑ {compact_number(input_tokens)} ↓ {compact_number(output_tokens)}"
            return value, value
        return None, None

    if name == "context":
        used = _safe_int(data.get("context_used"))
        total = _safe_int(data.get("context_max"))
        if total > 0:
            percent = int(used / total * 100)
            value = f"{compact_number(used)}/{compact_number(total)} ({percent}%)"
            if show_label:
                return i18n.zh("context").format(value), i18n.en("context").format(value)
            return value, value
        return None, None

    return None, None


def _safe_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


# ── 格式化工具 ────────────────────────────────────────────────────


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        millions = value / 1_000_000
        return f"{int(millions)}M" if millions >= 100 else f"{millions:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_elapsed(ms: float) -> str:
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60)}s"

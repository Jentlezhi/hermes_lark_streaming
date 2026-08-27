"""事件归一化 — 把 Hermes 的原始文本与回调语义收敛为插件事件.

本模块是 Hermes 语义的**唯一解释点**。所有「这段文本是什么意思」的判断
都集中在这里，其他层不做文本模式匹配。

设计取舍：优先复用 Hermes 自己导出的常量（它随 Hermes 一起演进，比自写
正则稳定），只在 import 失败时回退到内置模式表，并在日志中标注降级。
"""

from __future__ import annotations

import re
from typing import Final

from ..observability import logger
from .model import EventKind, NoticeLevel

# ── 复用 Hermes 常量（失败则降级为内置模式）─────────────────────────

#: Hermes 压缩相关状态模板。取到则说明可与 Hermes 版本同步演进
_HERMES_COMPACTION_TEMPLATES: tuple[str, ...] = ()
_HERMES_CONSTANTS_LOADED = False

try:  # pragma: no cover - 依赖运行时 Hermes 环境
    from agent.conversation_compression import (  # type: ignore[import-not-found]
        COMPACTION_STATUS,
        IDLE_COMPACTION_STATUS_TEMPLATE,
        PRE_API_COMPRESSION_STATUS_TEMPLATE,
        PREFLIGHT_COMPRESSION_STATUS_TEMPLATE,
    )

    _HERMES_COMPACTION_TEMPLATES = tuple(
        str(item)
        for item in (
            COMPACTION_STATUS,
            IDLE_COMPACTION_STATUS_TEMPLATE,
            PRE_API_COMPRESSION_STATUS_TEMPLATE,
            PREFLIGHT_COMPRESSION_STATUS_TEMPLATE,
        )
        if isinstance(item, str) and item.strip()
    )
    _HERMES_CONSTANTS_LOADED = bool(_HERMES_COMPACTION_TEMPLATES)
except Exception:
    logger.debug("Hermes 压缩常量不可用，状态分类降级为内置模式表", exc_info=True)


#: Gateway 生命周期通知的特征短语。这类消息必须**同时**进卡片与聊天，
#: 见 :func:`is_lifecycle_notice` 的说明。
#: 优先从 Hermes 借常量（与上面复用压缩模板同源），拿不到时用实测字面量兜底。
_LIFECYCLE_PHRASES: tuple[str, ...] = ("Gateway shutting down", "Gateway restarting")

try:  # pragma: no cover - 依赖运行时 Hermes 环境
    from gateway.run import (  # type: ignore[import-not-found]
        _INTERRUPT_REASON_GATEWAY_RESTART,
        _INTERRUPT_REASON_GATEWAY_SHUTDOWN,
    )

    _borrowed = tuple(
        str(item)
        for item in (_INTERRUPT_REASON_GATEWAY_SHUTDOWN, _INTERRUPT_REASON_GATEWAY_RESTART)
        if isinstance(item, str) and item.strip()
    )
    if _borrowed:
        _LIFECYCLE_PHRASES = _borrowed
except Exception:
    logger.debug("Hermes 生命周期常量不可用，使用内置短语表", exc_info=True)


def is_lifecycle_notice(text: str) -> bool:
    """是否为 gateway 生命周期通知（即将关闭 / 重启）.

    **这类消息必须同时进卡片和聊天，不能被卡片「接管」。** 原因是时序：
    通知在 ``stop()`` 开头发出，此时事件循环即将关闭，而卡片更新要经过
    100ms 节流再走一次飞书 API 往返——协程很可能来不及执行。若此时抑制了
    原生输出，用户就既看不到卡片更新、也看不到通知，消息彻底丢失。

    卡片里留一条记录（切回来能看到发生了什么）+ 聊天里保留原生消息，
    是「宁可重复也不丢」在这个场景下的落点。
    """
    if not text:
        return False
    lowered = text.lower()
    return any(phrase.lower() in lowered for phrase in _LIFECYCLE_PHRASES)



def hermes_constants_available() -> bool:
    """供 status 命令展示是否处于降级模式."""
    return _HERMES_CONSTANTS_LOADED


def _template_prefix(template: str) -> str:
    """取模板中第一个占位符之前的字面前缀，用作匹配特征.

    Hermes 的状态模板形如 ``"Compacting context — summarizing {n} messages"``，
    直接整串匹配会因参数不同而失配，取字面前缀更鲁棒。
    """
    cut = len(template)
    for marker in ("{", "%s", "%d"):
        index = template.find(marker)
        if index != -1:
            cut = min(cut, index)
    return template[:cut].strip()


_HERMES_COMPACTION_PREFIXES: Final[tuple[str, ...]] = tuple(
    prefix for prefix in (_template_prefix(t) for t in _HERMES_COMPACTION_TEMPLATES) if len(prefix) >= 6
)


# ── 内置模式表 ────────────────────────────────────────────────────

#: 记忆更新 / 自我改进类，渲染为 REVIEW
_REVIEW_RE: Final[re.Pattern[str]] = re.compile(
    r"(💾|🧠|📝)\s*|"
    r"\b(memory\s+(updated|saved|stored)|self[-\s]?improvement|improvement\s+review"
    r"|记忆(已)?更新|自我改进|反思完成)\b",
    re.IGNORECASE,
)

#: 上下文压缩类，渲染为 NOTICE
_COMPACTION_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(compacting\s+context|compress(ing|ed)|preflight\s+compression|pre[-\s]?api\s+compression"
    r"|context\s+too\s+large|上下文(压缩|过长)|正在压缩)\b",
    re.IGNORECASE,
)

#: 重试 / 限流类，渲染为 WARNING 级 NOTICE
_RETRY_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(rate\s+limited|retrying|retry\s+\d|max\s+retries|falling\s+back|trying\s+fallback"
    r"|限流|重试中|正在重试)\b",
    re.IGNORECASE,
)

#: 失败类，渲染为 ERROR 级 NOTICE
_ERROR_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(failed|failure|error|unavailable|exhausted|错误|失败|不可用)\b",
    re.IGNORECASE,
)

#: 纯噪音：这些内容 Hermes 自己都认为该留在日志而非聊天，收纳会污染卡片
_NOISE_RE: Final[re.Pattern[str]] = re.compile(
    r"("
    r"auxiliary\s+.+\s+failed"
    r"|compression\s+summary\s+failed"
    r"|fallback\s+context\s+marker"
    r"|no\s+auxiliary\s+llm\s+provider\s+configured"
    r"|configured\s+compression\s+model\s+.+\s+failed"
    r"|configured\s+auxiliary\s+compression\s+provider\s+.+\s+unavailable"
    r"|skipping\s+concurrent\s+compression"
    r"|stale\s+connections\s+from\s+a\s+previous\s+provider\s+issue"
    r"|auto-lowered\s+(?:this\s+)?session'?s?\s+threshold"
    r")",
    re.IGNORECASE | re.DOTALL,
)

#: 工作轮 / 迭代进度，渲染为 INFO 级 NOTICE
_PROGRESS_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(iteration\s+\d+|step\s+\d+\s+of\s+\d+|round\s+\d+|working|thinking"
    r"|第\s*\d+\s*轮|工作轮|正在处理)\b",
    re.IGNORECASE,
)


def is_noise_status(text: str) -> bool:
    """是否为不应进入卡片的噪音状态.

    这些文案 Hermes 在 Telegram 上就已经过滤掉了（``_TELEGRAM_NOISY_STATUS_RE``），
    收进卡片只会让用户困惑。
    """
    if not text or not text.strip():
        return True
    return bool(_NOISE_RE.search(text))


def classify_status_text(text: str) -> tuple[EventKind, NoticeLevel]:
    """判断一段状态文本的归类与级别.

    返回 ``(EventKind.NOTICE | EventKind.REVIEW, NoticeLevel)``。
    调用方需先用 :func:`is_noise_status` 过滤噪音。

    判定顺序有意如此：REVIEW 特征最明确故最先判；压缩与进度是中性信息；
    失败判定放在重试之后，因为「重试」文案里常含 failed 字样但本质是警告而非错误。
    """
    stripped = text.strip()
    if not stripped:
        return EventKind.NOTICE, NoticeLevel.INFO

    if _REVIEW_RE.search(stripped):
        return EventKind.REVIEW, NoticeLevel.INFO

    # 优先用 Hermes 自带常量前缀判压缩，命中即为中性信息
    for prefix in _HERMES_COMPACTION_PREFIXES:
        if prefix and prefix.lower() in stripped.lower():
            return EventKind.NOTICE, NoticeLevel.INFO

    if _COMPACTION_RE.search(stripped) or _PROGRESS_RE.search(stripped):
        return EventKind.NOTICE, NoticeLevel.INFO

    if _RETRY_RE.search(stripped):
        return EventKind.NOTICE, NoticeLevel.WARNING

    if _ERROR_RE.search(stripped):
        return EventKind.NOTICE, NoticeLevel.ERROR

    return EventKind.NOTICE, NoticeLevel.INFO


# ── 思考标签剥离 ──────────────────────────────────────────────────

_THINK_OPEN_RE: Final[re.Pattern[str]] = re.compile(r"<\s*think(?:ing)?\s*>", re.IGNORECASE)
_THINK_CLOSE_RE: Final[re.Pattern[str]] = re.compile(r"<\s*/\s*think(?:ing)?\s*>", re.IGNORECASE)


def strip_reasoning_tags(text: str) -> str:
    """剥离模型内部思考标签，防止 ``</think>`` 之类控制标记泄露到卡片.

    只做标记清理，不改变正文内容；清理后仅剩空白则返回空串，
    由调用方决定是否跳过该次更新。
    """
    if not text:
        return ""
    if "<" not in text:
        return text
    cleaned = _THINK_OPEN_RE.sub("", text)
    cleaned = _THINK_CLOSE_RE.sub("", cleaned)
    return cleaned if cleaned.strip() else ""


def split_reasoning_text(text: str) -> tuple[str, str]:
    """把混合文本拆成 ``(思考部分, 回答部分)``.

    Hermes 的 ``interim_assistant_callback`` 可能送来带 ``<think>`` 包裹的
    混合内容。未出现标签时整段视为回答。
    """
    if not text:
        return "", ""
    if not _THINK_OPEN_RE.search(text):
        return "", strip_reasoning_tags(text)

    reasoning_parts: list[str] = []
    answer_parts: list[str] = []
    cursor = 0
    while cursor < len(text):
        open_match = _THINK_OPEN_RE.search(text, cursor)
        if open_match is None:
            answer_parts.append(text[cursor:])
            break
        answer_parts.append(text[cursor : open_match.start()])
        close_match = _THINK_CLOSE_RE.search(text, open_match.end())
        if close_match is None:
            # 标签未闭合：剩余全部视为思考内容
            reasoning_parts.append(text[open_match.end() :])
            break
        reasoning_parts.append(text[open_match.end() : close_match.start()])
        cursor = close_match.end()

    return "".join(reasoning_parts), "".join(answer_parts)

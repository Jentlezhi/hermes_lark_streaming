"""工具调用追踪.

本模块属 L2 领域层，**不含任何飞书概念**：只输出语义化的 ``icon_key``
（如 ``read`` / ``search``），由 L3 渲染层映射为具体平台的图标标识。
这样换平台时 L2 不需要改动。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, TypedDict

from ..observability import redact

#: 单个 turn 最多追踪的工具步数，超出丢弃（防御恶性循环调用）
MAX_TOOL_STEPS = 128
#: 单条结果/错误块的最大字符数，超出截断
MAX_BLOCK_CHARS = 2000


class ToolStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class ToolBlock(TypedDict):
    """结果或错误的展示块."""

    language: str
    content: str


class ToolDisplayStep(TypedDict):
    """供渲染层消费的工具步骤视图."""

    name: str
    title: str
    status: str
    detail: str
    icon_key: str
    elapsed_ms: float
    result_block: ToolBlock | None
    error_block: ToolBlock | None


@dataclass(slots=True)
class ToolStep:
    name: str
    status: ToolStatus
    detail: str = ""
    output: str = ""
    error: str = ""
    result_block: ToolBlock | None = None
    error_block: ToolBlock | None = None
    started_at: float = 0.0
    elapsed_ms: float = 0.0


@dataclass(slots=True)
class ToolSession:
    steps: list[ToolStep] = field(default_factory=list)
    started_at: float = 0.0


# ── 工具描述符 ────────────────────────────────────────────────────
# sanitizer 决定 detail 的清洗方式：
#   command — 命令行，脱敏 + 路径只留文件名
#   path    — 路径，只留文件名
#   search  — 搜索词，去引号
#   url     — 链接，去引号与前缀
#   None    — 原样
# no_result=True 表示该工具的输出通常冗长且无展示价值，不渲染结果块

_TOOL_DESCRIPTORS: Final[tuple[dict[str, Any], ...]] = (
    {"aliases": ("skill",), "icon_key": "skill", "title": "加载技能", "sanitizer": None},
    {"aliases": ("read", "open"), "icon_key": "read", "title": "读取文件", "sanitizer": "path", "no_result": True},
    {"aliases": ("write", "edit"), "icon_key": "edit", "title": "编辑文件", "sanitizer": "path", "no_result": True},
    {"aliases": ("web_search", "search"), "icon_key": "search", "title": "搜索", "sanitizer": "search"},
    {"aliases": ("web_fetch", "fetch"), "icon_key": "web", "title": "抓取网页", "sanitizer": "url", "no_result": True},
    {"aliases": ("grep",), "icon_key": "grep", "title": "检索文本", "sanitizer": "search"},
    {"aliases": ("glob",), "icon_key": "folder", "title": "检索文件", "sanitizer": "path"},
    {"aliases": ("exec", "bash", "command", "run"), "icon_key": "terminal", "title": "执行命令", "sanitizer": "command"},
    {"aliases": ("browser", "playwright", "navigate"), "icon_key": "browser", "title": "浏览器", "no_result": True},
    {"aliases": ("agent", "task", "spawn"), "icon_key": "agent", "title": "子任务"},
    {"aliases": ("check", "verify", "determine"), "icon_key": "check", "title": "校验"},
    {"aliases": ("summarize", "analyze", "prepare"), "icon_key": "report", "title": "分析"},
    {"aliases": ("clarify",), "icon_key": "chat", "title": "澄清提问", "no_result": True},
    {"aliases": ("todo", "plan"), "icon_key": "list", "title": "规划"},
)


def _resolve_descriptor(name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    normalized = name.strip().lower().replace("-", "_")
    for desc in _TOOL_DESCRIPTORS:
        for alias in desc["aliases"]:
            if normalized == alias or normalized.startswith(f"{alias}_"):
                return desc
    return None


def _humanize(name: str) -> str:
    cleaned = name.replace("-", " ").replace("_", " ").strip()
    if not cleaned:
        return "工具"
    return cleaned[0].upper() + cleaned[1:]


def _basename_only(text: str) -> str:
    if not text:
        return text
    return os.path.basename(text.replace("\\", "/").rstrip("/"))


def _redact_paths(text: str) -> str:
    """命令中的路径只保留文件名，避免泄露目录结构."""
    return re.sub(
        r'(^|[\s=\'"()])([~./][^\s\'"()]+)',
        lambda m: f"{m.group(1)}{os.path.basename(m.group(2))}",
        text,
    )


def _sanitize_detail(text: str, sanitizer: str | None) -> str:
    if not text or not sanitizer:
        return text
    cleaned = re.sub(r"<[^>]+>", "", text).strip()
    if not cleaned:
        return text
    if sanitizer == "command":
        return _redact_paths(redact(cleaned))
    if sanitizer == "path":
        return _basename_only(re.sub(r"^(?:from|file|path)\s+", "", cleaned, flags=re.IGNORECASE).strip())
    if sanitizer in ("search", "url"):
        cleaned = cleaned.strip("'\"")
        if sanitizer == "url" and cleaned.lower().startswith("from "):
            return cleaned.replace("from ", "", 1)
        return cleaned
    return cleaned


def _build_block(value: Any, fallback_lang: str, *, sanitizer: str | None = None) -> ToolBlock | None:
    """构造结果/错误展示块，JSON 自动美化并统一截断."""
    if value is None:
        return None

    if isinstance(value, (dict, list)):
        try:
            content = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            content = str(value)
        return _clip_block("json", content)

    text = str(value).replace("\r\n", "\n").strip()
    if not text:
        return None
    if sanitizer == "command":
        text = redact(text)

    if text.startswith(("{", "[")):
        try:
            content = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            return _clip_block("json", content)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return _clip_block("text" if fallback_lang == "json" else fallback_lang, text)


def _clip_block(language: str, content: str) -> ToolBlock:
    if len(content) > MAX_BLOCK_CHARS:
        content = content[:MAX_BLOCK_CHARS] + "\n… （已截断）"
    return {"language": language, "content": content}


class ToolTracker:
    """追踪单个 turn 内的工具调用.

    线程安全说明：调用方（Turn）持锁后再调用本类，本类自身不加锁。
    """

    __slots__ = ("_session", "_dropped")

    def __init__(self) -> None:
        self._session: ToolSession | None = None
        self._dropped = 0

    @property
    def dropped(self) -> int:
        """因超出上限被丢弃的步数，用于在卡片上如实标注."""
        return self._dropped

    @property
    def elapsed_ms(self) -> float:
        if self._session is None:
            return 0.0
        return (time.time() - self._session.started_at) * 1000

    @property
    def has_running(self) -> bool:
        """是否还有工具正在执行.

        空闲守护用它区分「任务卡死」与「任务在跑但暂时没输出」：工具执行期间
        Hermes 不产生任何回调，turn 的 ``updated_at`` 一动不动，两种情况在时间
        维度上长得一模一样。不做这个区分，一次跑几分钟的编译或测试就会被判成
        超时，卡片提前定格成「已超时收尾」而任务其实还在跑。

        刻意不复用 :meth:`current_action`：那个方法要做脱敏与字符串拼接，而这里
        只需要一个布尔值——守护每轮扫描都要对每个活跃 turn 问一次。
        """
        if self._session is None:
            return False
        return any(step.status == ToolStatus.RUNNING for step in self._session.steps)

    def record_start(self, name: str, detail: str = "") -> None:
        if self._session is None:
            self._session = ToolSession(started_at=time.time())
        if len(self._session.steps) >= MAX_TOOL_STEPS:
            self._dropped += 1
            return
        self._session.steps.append(
            ToolStep(name=name, status=ToolStatus.RUNNING, detail=detail, started_at=time.time())
        )

    def record_end(self, name: str, *, error: str = "", output: str = "") -> None:
        """结束最近一个同名的运行中步骤.

        按名字倒序匹配而非顺序匹配，是为了正确处理并行同名工具：
        后开始的先结束时，配对到最近那个才符合直觉。
        """
        if self._session is None:
            return
        desc = _resolve_descriptor(name)
        sanitizer = desc.get("sanitizer") if desc else None

        for step in reversed(self._session.steps):
            if step.name == name and step.status == ToolStatus.RUNNING:
                step.status = ToolStatus.ERROR if error else ToolStatus.SUCCESS
                step.error = error
                step.output = output
                step.elapsed_ms = (time.time() - step.started_at) * 1000
                if error:
                    step.error_block = _build_block(error, "text", sanitizer=sanitizer)
                elif output:
                    step.result_block = _build_block(output, "json", sanitizer=sanitizer)
                return

        # 没匹配到开始事件（丢事件或跨 turn），补一条终态步骤而不是丢弃
        if len(self._session.steps) >= MAX_TOOL_STEPS:
            self._dropped += 1
            return
        self._session.steps.append(
            ToolStep(
                name=name,
                status=ToolStatus.ERROR if error else ToolStatus.SUCCESS,
                detail=error or output,
                output=output,
                error=error,
                started_at=time.time(),
                error_block=_build_block(error, "text", sanitizer=sanitizer) if error else None,
                result_block=_build_block(output, "json", sanitizer=sanitizer) if output else None,
            )
        )

    def build_display_steps(self) -> list[ToolDisplayStep]:
        """构造渲染视图，同时完成脱敏与标题人性化."""
        if self._session is None:
            return []
        steps: list[ToolDisplayStep] = []
        for step in self._session.steps:
            desc = _resolve_descriptor(step.name)
            title = desc["title"] if desc else _humanize(step.name)
            if step.elapsed_ms > 0:
                title = f"{title}（{_format_duration(step.elapsed_ms)}）"
            sanitizer = desc.get("sanitizer") if desc else None
            steps.append(
                {
                    "name": step.name,
                    "title": title,
                    "status": step.status.value,
                    "detail": _sanitize_detail(step.detail, sanitizer),
                    "icon_key": desc["icon_key"] if desc else "tool",
                    "elapsed_ms": step.elapsed_ms,
                    "result_block": None if (desc and desc.get("no_result")) else step.result_block,
                    "error_block": step.error_block,
                }
            )
        return steps

    def current_action(self) -> str:
        """当前正在执行的动作描述，用于会话列表状态摘要."""
        if self._session is None:
            return ""
        for step in reversed(self._session.steps):
            if step.status == ToolStatus.RUNNING:
                desc = _resolve_descriptor(step.name)
                title = desc["title"] if desc else _humanize(step.name)
                detail = _sanitize_detail(step.detail, desc.get("sanitizer") if desc else None)
                return f"{title}：{detail}" if detail else title
        return ""

    @property
    def step_count(self) -> int:
        return len(self._session.steps) if self._session else 0


def _format_duration(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60)}s"

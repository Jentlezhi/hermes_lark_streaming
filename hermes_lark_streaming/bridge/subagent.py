"""子 Agent 生命周期 — 把委派出去的子任务收进卡片.

HFC 通过官方 hook ``subagent_start`` / ``subagent_stop`` 拿到这两个时机。
本模块改用运行时织入 :class:`SubagentLifecycleService`（``agent/subagent_lifecycle.py``），
原因是官方 hook 走的是 HFC 的跨进程事件通道，而我们在进程内可以直接拿到
更完整的信息：``SubagentLaunchRequest.goal``（子任务目标）、``SubagentState``
（9 档状态，能区分 SUCCEEDED / FAILED / INTERRUPTED / CANCELLED）、以及父
agent 实例本身——后者是定位 turn 的关键。

**呈现方式刻意选 NOTICE 而不是新建段类型**：子任务由 ``tools/delegate_tool``
触发，Hermes 的 ``tool_progress_callback`` 很可能已经把 delegate 这次调用报进
了工具面板。再建一个平行的段会让同一件事在卡片上出现两次。走 notice 通道
则是「补充说明」的语义，与工具时间线并存而不重复。

**两个织入点各自独立降级**：``launch`` 是公开方法（稳定）负责「已启动」，
``_run`` 是私有方法（易随版本改名）负责终态。只有 ``launch`` 织上时仍能显示
启动，只是看不到完成——比全都没有要好。
"""

from __future__ import annotations

from typing import Any

from ..events import NoticeLevel
from ..observability import logger
from ..orchestrator import PUSH_REJECTED, Orchestrator

#: 类级幂等标记
_SUBAGENT_WOVEN_FLAG = "_hls_subagent_woven"

#: 织入前的原方法，供 :func:`uninstall_subagent_hook` 还原
_originals: tuple[type, dict[str, Any]] | None = None

#: 终态 -> (图标, 中文说明, 提示级别)
_TERMINAL_STYLE: dict[str, tuple[str, str, NoticeLevel]] = {
    "SUCCEEDED": ("✅", "子任务已完成", NoticeLevel.INFO),
    "FAILED": ("❌", "子任务失败", NoticeLevel.ERROR),
    "INTERRUPTED": ("⏹️", "子任务被中断", NoticeLevel.WARNING),
    "CANCELLED": ("⏹️", "子任务已取消", NoticeLevel.WARNING),
}

#: 子任务目标在卡片上的展示上限。goal 可能是一整段需求描述
_GOAL_LIMIT = 80
#: 结果摘要展示上限
_SUMMARY_LIMIT = 200


def locate_service_class() -> type:
    """定位 ``SubagentLifecycleService``。找不到即抛异常，由调用方降级."""
    from agent.subagent_lifecycle import (  # type: ignore[import-not-found]
        SubagentLifecycleService,
    )

    return SubagentLifecycleService  # type: ignore[no-any-return]



def install_subagent_hook(orch: Orchestrator) -> list[str]:
    """织入子 Agent 生命周期，返回成功织入的方法名.

    找不到该服务或两个方法都织不上时返回空列表——子任务不在卡片上呈现，
    其余功能完全不受影响。
    """
    try:
        service_class = locate_service_class()
    except Exception:
        logger.debug("定位 SubagentLifecycleService 失败，子任务织入跳过", exc_info=True)
        return []

    if getattr(service_class, _SUBAGENT_WOVEN_FLAG, False):
        return []

    applied: list[str] = []
    saved: dict[str, Any] = {}
    for name, wrapper in (("launch", _wrap_launch), ("_run", _wrap_run)):
        original = getattr(service_class, name, None)
        if not callable(original):
            logger.debug("SubagentLifecycleService 无 %s 方法，跳过该点", name)
            continue
        try:
            setattr(service_class, name, wrapper(orch, original))
            applied.append(name)
            saved[name] = original
        except Exception:
            logger.debug("子任务方法织入失败: %s", name, exc_info=True)

    if applied:
        try:
            setattr(service_class, _SUBAGENT_WOVEN_FLAG, True)
        except Exception:
            logger.debug("写入子任务织入标记失败", exc_info=True)
        global _originals
        _originals = (service_class, saved)
    return applied


def uninstall_subagent_hook() -> bool:
    """还原子任务生命周期方法，返回是否确实卸载了."""
    global _originals
    if _originals is None:
        return False
    service_class, saved = _originals
    for name, original in saved.items():
        try:
            setattr(service_class, name, original)
        except Exception:
            logger.debug("子任务方法还原失败: %s", name, exc_info=True)
    try:
        if _SUBAGENT_WOVEN_FLAG in vars(service_class):
            delattr(service_class, _SUBAGENT_WOVEN_FLAG)
    except Exception:
        logger.debug("子任务织入标记清理失败", exc_info=True)
    _originals = None
    return True


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)] + "…"


def _push(orch: Orchestrator, parent: Any, text: str, level: NoticeLevel) -> None:
    """把一条子任务消息收进父 turn 的卡片."""
    if not orch.enabled or not orch.capture_active("subagent"):
        return
    from .callbacks import turn_key_for

    turn_key = turn_key_for(orch, parent, create=False)
    if not turn_key:
        return
    if orch.push_notice(turn_key, text, level):
        orch.record_capture_success("subagent")
    else:
        orch.record_capture_failure("subagent", PUSH_REJECTED)


def _wrap_launch(orch: Orchestrator, original: Any) -> Any:
    """包装 ``launch``：子任务启动时报一条「已启动」.

    父 agent 从 service 的 resolver 取。用 ``getattr`` 容错访问私有属性：
    它随时可能改名，改名后只是拿不到父 agent（本条不展示），不影响 launch。
    """

    def launch(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        handle = original(self, request, *args, **kwargs)
        try:
            resolver = getattr(self, "_parent_agent_resolver", None)
            parent = resolver() if callable(resolver) else None
            if parent is None:
                return handle
            goal = _clip(getattr(request, "goal", "") or "", _GOAL_LIMIT)
            model = str(getattr(handle, "model", "") or "")
            role = str(getattr(handle, "role", "") or "")
            detail = " · ".join(part for part in (role, model) if part)
            text = f"🧩 子任务已启动：{goal}" if goal else "🧩 子任务已启动"
            if detail:
                text = f"{text}（{detail}）"
            _push(orch, parent, text, NoticeLevel.INFO)
        except Exception:
            logger.debug("子任务启动收纳失败", exc_info=True)
        return handle

    return launch


def _wrap_run(orch: Orchestrator, original: Any) -> Any:
    """包装 ``_run``：执行结束后按终态报一条结果.

    ``_run(self, record, goal, parent)`` 的第三个参数就是父 agent，终态写在
    ``record.state`` 上（见 ``agent/subagent_lifecycle.py`` 的 ``_run``），
    因此这一个点既拿得到归属又拿得到结果，不必再去查 registry。
    """

    def run(self: Any, record: Any, goal: Any = "", parent: Any = None, *args: Any, **kwargs: Any) -> Any:
        try:
            return original(self, record, goal, parent, *args, **kwargs)
        finally:
            try:
                _report_terminal(orch, record, goal, parent)
            except Exception:
                logger.debug("子任务终态收纳失败", exc_info=True)

    return run


def _report_terminal(orch: Orchestrator, record: Any, goal: Any, parent: Any) -> None:
    """按 record 的终态拼一条结果消息."""
    if parent is None:
        return

    state = getattr(record, "state", None)
    # SubagentState 是 str 枚举，取 .value；万一换了类型就退回 str()
    name = str(getattr(state, "value", state) or "").upper()
    icon, label, level = _TERMINAL_STYLE.get(name, ("🧩", f"子任务结束（{name or '未知'}）", NoticeLevel.INFO))

    parts = [f"{icon} {label}"]
    title = _clip(goal or "", _GOAL_LIMIT)
    if title:
        parts.append(f"：{title}")
    text = "".join(parts)

    # 结果摘要与错误都在 record 上，取到就附一行——子任务失败时这是唯一线索
    result = getattr(record, "result", None)
    summary = getattr(result, "summary", None) if result is not None else None
    error = getattr(result, "error", None) if result is not None else None
    tail = summary if isinstance(summary, str) and summary.strip() else error
    if isinstance(tail, str) and tail.strip():
        text = f"{text}\n{_clip(tail, _SUMMARY_LIMIT)}"

    _push(orch, parent, text, level)


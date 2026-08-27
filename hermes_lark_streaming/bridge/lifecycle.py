"""Turn 终态 — 精确收卡路径.

卡片必须**保证**能走到终态，否则用户会看到一张永远停在「处理中」的卡片，
这正是需求里要治理的「切走后看不出任务是否完成」。为此设计两条独立路径：

1. **精确终态（本模块）**：织入 ``AIAgent`` 的对话主方法，返回时立即收卡，
   并带上模型、耗时、token 等 footer 数据。快、信息全，但依赖方法名。
2. **兜底守护**（见 ``orchestrator._IdleWatcher``）：后台定时扫描，长时间
   无更新的 turn 强制收卡。慢一些、footer 信息不全，但**不依赖 Hermes 的
   任何内部结构**，永远可用。

两条路径互相独立：路径 1 失效（Hermes 改了方法名）时路径 2 仍然兜底，
卡片最坏只是晚几十秒定格，绝不会永久悬挂。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from ..observability import logger
from ..orchestrator import Orchestrator

#: AIAgent 上可能的对话主方法名。只织入实际存在的
_CONVERSATION_METHODS = ("run_conversation", "arun_conversation", "run_conversation_async")

#: 类级幂等标记
_CONV_WOVEN_FLAG = "_hls_conversation_woven"

#: 织入前的原方法，供 :func:`uninstall_conversation_hook` 还原
_originals: tuple[type, dict[str, Any]] | None = None

#: 调用深度，只在最外层收卡（对话方法可能内部递归调用自身）
_depth = threading.local()


def _enter() -> int:
    value = getattr(_depth, "value", 0) + 1
    _depth.value = value
    return value


def _leave() -> int:
    value = max(0, getattr(_depth, "value", 1) - 1)
    _depth.value = value
    return value


def install_conversation_hook(orch: Orchestrator) -> list[str]:
    """织入对话主方法，获得精确终态.

    与回调织入不同：对话方法是**类上的函数**，不是被赋值的实例属性，
    因此直接替换类属性，而不是安装描述符。

    找不到任何候选方法时返回空列表——此时终态完全由兜底守护负责，
    功能仍然可用，只是收卡会晚几十秒且 footer 信息不全。
    """
    try:
        from .weave import AgentWeaver

        agent_class = AgentWeaver.locate_agent_class()
    except Exception:
        logger.debug("定位 AIAgent 失败，对话织入跳过", exc_info=True)
        return []

    if getattr(agent_class, _CONV_WOVEN_FLAG, False):
        return []

    applied: list[str] = []
    saved: dict[str, Any] = {}
    for name in _CONVERSATION_METHODS:
        original = getattr(agent_class, name, None)
        if not callable(original):
            continue
        try:
            setattr(agent_class, name, _wrap_conversation_method(orch, original))
            applied.append(name)
            saved[name] = original
        except Exception:
            logger.debug("对话方法织入失败: %s", name, exc_info=True)

    if applied:
        try:
            setattr(agent_class, _CONV_WOVEN_FLAG, True)
        except Exception:
            logger.debug("写入对话织入标记失败", exc_info=True)
        global _originals
        _originals = (agent_class, saved)
    return applied


def uninstall_conversation_hook() -> bool:
    """还原对话主方法，返回是否确实卸载了."""
    global _originals
    if _originals is None:
        return False
    agent_class, saved = _originals
    for name, original in saved.items():
        try:
            setattr(agent_class, name, original)
        except Exception:
            logger.debug("对话方法还原失败: %s", name, exc_info=True)
    try:
        if _CONV_WOVEN_FLAG in vars(agent_class):
            delattr(agent_class, _CONV_WOVEN_FLAG)
    except Exception:
        logger.debug("对话织入标记清理失败", exc_info=True)
    _originals = None
    return True


def _wrap_conversation_method(orch: Orchestrator, original: Any) -> Any:
    """包装对话方法，在最外层调用结束时收卡."""
    if asyncio.iscoroutinefunction(original):

        async def async_method(self: Any, *args: Any, **kwargs: Any) -> Any:
            started = time.time()
            _enter()
            result: Any = None
            try:
                result = await original(self, *args, **kwargs)
                return result
            finally:
                if _leave() == 0:
                    _finalize(orch, self, result, started)

        return async_method

    def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.time()
        _enter()
        result: Any = None
        try:
            result = original(self, *args, **kwargs)
            return result
        finally:
            if _leave() == 0:
                _finalize(orch, self, result, started)

    return method


def _extract_result(result: Any) -> dict[str, Any]:
    """从对话返回值中提取终态信息.

    Hermes 的结果是一个字典（字段名见 gateway/run.py 的 completion 处理）。
    拿不到就返回空——footer 少几个字段不影响卡片可用。
    """
    if not isinstance(result, dict):
        return {}
    return {
        "model": str(result.get("model") or ""),
        "is_error": bool(result.get("failed")),
        "interrupted": bool(result.get("interrupted")),
        "answer": str(result.get("final_response") or result.get("response") or ""),
        "tokens": {
            "input_tokens": result.get("input_tokens", 0),
            "output_tokens": result.get("output_tokens", 0),
        },
        "context": {
            "used_tokens": result.get("last_prompt_tokens", 0),
            "max_tokens": result.get("context_length", 0),
        },
    }


def _finalize(orch: Orchestrator, agent: Any, result: Any, started: float) -> None:
    """对话结束，收束对应 turn.

    注意本函数运行在 Agent 的 worker 线程上，因此只做调度，不做 IO。
    """
    try:
        from .callbacks import turn_key_for

        turn_key = turn_key_for(orch, agent, create=False)
        if not turn_key:
            return
        turn = orch.registry.get(turn_key)
        if turn is None or turn.state.is_terminal:
            return

        info = _extract_result(result)
        duration = time.time() - started

        if info.get("interrupted"):
            orch.spawn(orch.abort_turn(turn_key, aborted=True))
            return

        orch.spawn(
            orch.complete_turn(
                turn_key,
                answer=str(info.get("answer") or ""),
                is_error=bool(info.get("is_error")),
                duration=duration,
                model=str(info.get("model") or ""),
                tokens=info.get("tokens"),
                context=info.get("context"),
            )
        )
    except Exception:
        logger.debug("对话结束收卡失败", exc_info=True)

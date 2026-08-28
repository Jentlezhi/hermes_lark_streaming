"""中断捕获 — 精确区分「用户 /stop」与「被新消息接续」.

HLS 用 3 处 AST 注入（``ABORT`` / ``STOP`` / ``INTERRUPT``）达到这个精度。
本模块改用运行时织入 ``AIAgent.interrupt()``（``run_agent.py:3268``）——那是
Hermes 表达中断的唯一入口方法，语义比 gateway 层的注入点更根本：

* **不依赖 gateway 的代码结构**：``gateway/run.py`` 怎么重构都不影响，而
  HLS 注入的是 ``was_interrupted`` 分支所在的具体位置
* **区分度更好**：Hermes 自己的 ``hard_cancel`` 参数就是用来分辨「显式停止」
  与「新消息打断」的（见该方法 docstring），一个织入点覆盖 HLS 三个注入点

**为什么中断必须立刻收卡**：中断后 Agent 不再产出任何内容，卡片会一直停在
「处理中」直到空闲守护兜底（默认 90 秒，见 ``streaming.limits.idle_finalize_sec``）。
而用户此时往往已经发了新消息，看着旧卡片
继续转圈只会以为系统卡死了——这正是需求里要治理的「看不出任务状态」。

**顺序铁律**：先执行 Hermes 原方法，再收卡。中断是功能，卡片是展示；
收卡失败绝不能让中断本身不生效。
"""

from __future__ import annotations

import logging
from typing import Any

from ..observability import METRICS, log_turn, logger
from ..orchestrator import Orchestrator

#: Hermes 的中断入口方法名
_INTERRUPT_METHOD = "interrupt"
#: 类级幂等标记
_INTERRUPT_WOVEN_FLAG = "_hls_interrupt_woven"
#: 织入前的原方法与所属类，供 :func:`uninstall_interrupt_hook` 还原。
#: 不留这个引用就没有卸载路径——``selftest`` 声称的「演练后回滚」会变成空话
_original: tuple[type, Any] | None = None


def install_interrupt_hook(orch: Orchestrator) -> bool:
    """织入 ``AIAgent.interrupt()``，中断时立即定格卡片.

    与回调织入不同：``interrupt`` 是**类上的函数**而非被赋值的实例属性，
    因此直接替换类属性，与 :mod:`.lifecycle` 织入对话主方法同源。

    失败返回 False 且不影响其他功能——空闲守护仍会兜底收卡，只是会晚
    几十秒，且分不清中断原因。
    """
    try:
        from .weave import AgentWeaver

        agent_class = AgentWeaver.locate_agent_class()
    except Exception:
        logger.debug("定位 AIAgent 失败，中断织入跳过", exc_info=True)
        return False

    if getattr(agent_class, _INTERRUPT_WOVEN_FLAG, False):
        return True

    original = getattr(agent_class, _INTERRUPT_METHOD, None)
    if not callable(original):
        logger.debug("AIAgent 无 %s 方法，中断织入跳过（空闲守护兜底）", _INTERRUPT_METHOD)
        return False

    try:
        setattr(agent_class, _INTERRUPT_METHOD, _wrap_interrupt(orch, original))
        setattr(agent_class, _INTERRUPT_WOVEN_FLAG, True)
    except Exception:
        logger.debug("中断方法织入失败", exc_info=True)
        return False

    global _original
    _original = (agent_class, original)
    return True


def uninstall_interrupt_hook() -> bool:
    """还原 ``AIAgent.interrupt``，返回是否确实卸载了.

    没有这条路径，``selftest`` 声称的「演练后回滚」就是空话——类方法替换
    会一直留在本进程里。
    """
    global _original
    if _original is None:
        return False
    agent_class, original = _original
    try:
        setattr(agent_class, _INTERRUPT_METHOD, original)
        if _INTERRUPT_WOVEN_FLAG in vars(agent_class):
            delattr(agent_class, _INTERRUPT_WOVEN_FLAG)
    except Exception:
        logger.debug("中断织入卸载失败", exc_info=True)
        return False
    _original = None
    return True


def _wrap_interrupt(orch: Orchestrator, original: Any) -> Any:
    """包装中断方法：原方法先跑完，再据其语义定格卡片.

    用 ``*args, **kwargs`` 透传：Hermes 的形参可能随版本增减，硬编码签名
    会在升级后直接破坏中断功能本身。
    """

    def interrupt(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return original(self, *args, **kwargs)
        finally:
            try:
                _capture(orch, self, args, kwargs)
            except Exception:
                logger.debug("中断收卡失败", exc_info=True)

    return interrupt


def _classify(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """区分中断类型，依据 Hermes 自己的语义.

    ``run_agent.py`` 的 ``interrupt`` docstring 写明：``hard_cancel`` 标记
    「显式停止」而非 redirect 或新消息打断，连压缩流程都要为它破例；
    ``message`` 则是「触发本次中断的新消息」，Agent 会把它接进下一轮。

    无 message 且非 hard_cancel 的 redirect 归为 ``stopped``：此时没有后继
    消息，用户不会看到新卡片，说「已中断·新消息已接续」是错的。
    """
    if kwargs.get("hard_cancel") is True:
        return "stopped"
    message = args[0] if args else kwargs.get("message")
    if isinstance(message, str) and message.strip():
        return "interrupted"
    return "stopped"


def _capture(orch: Orchestrator, agent: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """把中断映射为卡片终态.

    运行在**调用 interrupt 的线程**上（按 Hermes 的用法通常是消息接收线程，
    不是 agent worker），因此只做调度不做 IO——``orch.spawn`` 内部已处理
    跨线程投递到事件循环。
    """
    if not orch.enabled:
        return

    from .callbacks import turn_key_for

    turn_key = turn_key_for(orch, agent, create=False)
    if not turn_key:
        return
    turn = orch.registry.get(turn_key)
    if turn is None or turn.state.is_terminal:
        return

    reason = _classify(args, kwargs)
    METRICS.incr(f"interrupt.{reason}")
    log_turn(logging.INFO, turn_key, "捕获中断（%s），立即定格卡片", reason)
    orch.spawn(orch.abort_turn(turn_key, aborted=True, reason=reason))

"""回调包装 — 把 Hermes 的 Agent 回调接到卡片上.

**签名策略**：Hermes 各回调的形参名与个数随版本浮动，因此所有包装统一用
``(*args, **kwargs)`` 接收再尽力解析。解析不出来就原样透传，绝不因为签名
不匹配而让回调失败——那会直接破坏 Hermes 的运行。

**同步/异步**：不假设回调一定是同步函数。猜错会导致返回协程却从未 await，
消息静默丢失。这里按 ``iscoroutinefunction`` 分别包装。

**透传规则**：

* 内容类（answer / reasoning）——卡片接管后**不再透传**，否则卡片与原生消息重复
* 工具类——**始终透传**，Hermes 内部依赖它维护 typing 指示与日志
* 游离消息类（status / notice / review）——卡片接管后**不再透传**，这正是单卡收敛的目标
* 交互类（clarify）——**始终透传**，卡片只记录状态，交互本身不接管
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from ..core import InteractionKind, InteractionStatus
from ..events import NoticeLevel, classify_status_text, is_noise_status, split_reasoning_text
from ..events.model import EventKind
from ..observability import logger
from ..orchestrator import PUSH_REJECTED, Orchestrator
from .adapter import INBOUND, try_late_weave

#: 从 agent 实例上读取会话线索的属性名（构造参数，见 gateway/run.py:5884-5893）
_SESSION_ATTR = "gateway_session_key"
_CHAT_ATTR = "chat_id"
_PLATFORM_ATTR = "platform"

_FEISHU_PLATFORMS = frozenset({"feishu", "lark"})

#: 拦截判断：返回 True 表示已接管，不再调用原回调
Interceptor = Callable[[Any, tuple[Any, ...], dict[str, Any]], bool]


# ── Turn 定位 ─────────────────────────────────────────────────────


def _agent_platform(agent: Any) -> str:
    value = getattr(agent, _PLATFORM_ATTR, None)
    value = getattr(value, "value", value)
    return str(value or "").lower()


def turn_key_for(orch: Orchestrator, agent: Any, *, create: bool = True) -> str | None:
    """由 agent 实例定位它归属的 turn，必要时懒创建.

    agent 上带有 Hermes 构造时注入的 ``gateway_session_key`` 与 ``chat_id``
    （见 gateway/run.py:5884-5893），两者足以在注册表中定位活跃 turn。

    **懒创建**（``create=True``）：首个回调到达时若尚无 turn 就新建一个。优先
    用适配器层记录的入站消息作为 reply 锚点，让卡片挂在用户那条消息下面。

    **拿不到锚点时降级为无锚点建卡，而不是放弃建卡。** 这一条是从一次真机
    故障里改出来的：适配器织入若因任何原因没生效（Hermes 换了入站方法名、
    实例创建早于插件加载、判定条件失配……），``INBOUND`` 就会一直是空的。
    原先的写法在这里直接 ``return None``，结果是**卡片功能整体静默失效**——
    不建卡、不报错、一行日志都没有，只能靠读源码倒推。

    「少一个回复关系」和「卡片全废」是两件事，不该用同一个分支处理。降级后
    卡片直发会话，其余能力（打字机、工具面板、终态、summary）全部照常。

    收卡路径应传 ``create=False``：turn 已结束时不该再凭空造一张新卡。
    """
    session_key = getattr(agent, _SESSION_ATTR, None)
    chat_id = getattr(agent, _CHAT_ATTR, None)
    session_key = session_key if isinstance(session_key, str) else None
    chat_id = chat_id if isinstance(chat_id, str) else None

    turn = orch.registry.resolve_active(session_key=session_key, chat_id=chat_id)
    if turn is not None:
        return turn.turn_key
    if not create:
        return None

    # 仅对飞书会话懒建 turn，其他平台完全不干预
    if not chat_id or _agent_platform(agent) not in _FEISHU_PLATFORMS:
        return None

    # 显式配置为不接管的会话：一张卡都不建，全部走 Hermes 原生输出。
    # 拦在建 turn 之前是最省的落点——turn 不存在，后续所有收纳自然放行
    if orch.is_native_chat(chat_id):
        return None

    inbound = INBOUND.take(chat_id)
    if inbound is None:
        # 没有锚点通常意味着适配器入站织入还没生效——最常见的原因是插件加载
        # 早于飞书适配器创建。趁这条真实消息补织一次，成功后下一条就有锚点了
        if try_late_weave(orch):
            inbound = INBOUND.take(chat_id)

    if inbound is not None:
        message_id, _thread_id = inbound
        anchor_id: str | None = message_id
    else:
        # 降级路径：没有入站锚点，用「会话 + 毫秒时间戳」造一个进程内唯一的 key。
        # anchor_id 为 None 时编排层直发会话，不做 reply
        message_id = f"noanchor-{chat_id}-{int(time.time() * 1000)}"
        anchor_id = None
        _warn_missing_anchor(chat_id)

    # 同一条消息可能已建过 turn（Hermes 重试）——已存在则直接复用
    existing = orch.registry.get_active(message_id)
    if existing is not None:
        return existing.turn_key

    if orch.start_turn(
        turn_key=message_id,
        message_id=message_id,
        chat_id=chat_id,
        anchor_id=anchor_id,
        session_key=session_key,
    ):
        return message_id
    return None


#: 已就「缺少入站锚点」告警过的会话。每个会话只喊一次，避免刷日志
_anchorless_chats: set[str] = set()


def _warn_missing_anchor(chat_id: str) -> None:
    """首次遇到某会话缺锚点时明确告警.

    用 WARNING 而非 debug：这说明适配器入站织入没生效，是需要人处理的状态。
    卡片仍然工作（直发会话），所以不阻断，但必须留下可查的痕迹——
    静默降级正是上一次故障排查困难的根源。
    """
    if chat_id in _anchorless_chats:
        return
    _anchorless_chats.add(chat_id)
    logger.warning(
        "会话 %s 无入站锚点，卡片将直发会话而非回复原消息；"
        "这通常意味着适配器入站织入未生效（用 `status` 查看织入实况）",
        chat_id[:16],
    )


# ── 通用包装 ──────────────────────────────────────────────────────


def _compose(agent: Any, original: Any, intercept: Interceptor) -> Any:
    """把拦截逻辑与原回调组合，按实际类型适配同步 / 异步."""
    if asyncio.iscoroutinefunction(original):

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if _safe_intercept(intercept, agent, args, kwargs):
                return None
            return await original(*args, **kwargs)

        return async_wrapper

    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        if _safe_intercept(intercept, agent, args, kwargs):
            return None
        return original(*args, **kwargs)

    return sync_wrapper


def _safe_intercept(
    intercept: Interceptor,
    agent: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> bool:
    """执行拦截判断。任何异常都视为「未接管」，让原回调照常执行.

    这是不丢消息的最后一道保险。
    """
    try:
        return bool(intercept(agent, args, kwargs))
    except Exception:
        logger.debug("回调拦截异常，透传原回调", exc_info=True)
        return False


def _first_str(args: tuple[Any, ...], kwargs: dict[str, Any], *names: str) -> str:
    """从关键字或位置参数中取第一个字符串值."""
    for name in names:
        value = kwargs.get(name)
        if isinstance(value, str):
            return value
    for value in args:
        if isinstance(value, str):
            return value
    return ""


# ── 内容类 ────────────────────────────────────────────────────────


def _make_answer(orch: Orchestrator) -> Interceptor:
    """答案增量 → 卡片流式文本（打字机）."""

    def intercept(agent: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        text = _first_str(args, kwargs, "text", "delta", "content")
        if not text:
            return False
        turn_key = turn_key_for(orch, agent)
        return bool(turn_key and orch.push_answer(turn_key, text))

    return intercept


def _make_interim(orch: Orchestrator) -> Interceptor:
    """中间态助手文本 → 思考面板.

    这条通道可能混有 ``<think>`` 包裹的推理与正式回答，需要拆开分别落到
    reasoning 段与 answer 段，否则思考内容会污染正文。
    """

    def intercept(agent: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        if kwargs.get("already_streamed"):
            return False
        text = _first_str(args, kwargs, "text", "content")
        if not text:
            return False
        turn_key = turn_key_for(orch, agent)
        if not turn_key:
            return False

        reasoning, answer = split_reasoning_text(text)
        taken = False
        if reasoning:
            taken = orch.push_reasoning(turn_key, reasoning) or taken
        if answer:
            taken = orch.push_answer(turn_key, answer) or taken
        return taken

    return intercept


def _make_reasoning(orch: Orchestrator) -> Interceptor:
    """模型原生推理增量 → 思考面板."""

    def intercept(agent: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        text = _first_str(args, kwargs, "text", "content", "delta")
        if not text:
            return False
        turn_key = turn_key_for(orch, agent)
        return bool(turn_key and orch.push_reasoning(turn_key, text))

    return intercept


def _make_tool(orch: Orchestrator) -> Interceptor:
    """工具调用事件 → 卡片时间线.

    始终返回 False：工具事件必须继续透传，Hermes 内部依赖它维护
    typing 指示、日志与实时状态文本。
    """

    def intercept(agent: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        event_type = str(kwargs.get("event_type") or (args[0] if args and isinstance(args[0], str) else ""))
        if not event_type:
            return False

        tool_name = kwargs.get("tool_name")
        if not isinstance(tool_name, str):
            tool_name = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
        preview = kwargs.get("preview")
        if not isinstance(preview, str):
            preview = args[2] if len(args) > 2 and isinstance(args[2], str) else ""

        if not event_type.endswith(("started", "completed")):
            return False
        turn_key = turn_key_for(orch, agent)
        if not turn_key:
            return False

        if event_type.endswith("started"):
            orch.push_tool_start(turn_key, tool_name, preview)
        else:
            orch.push_tool_end(turn_key, tool_name, output=preview)
        return False  # 永远透传

    return intercept


# ── 游离消息类（单卡收敛的核心）────────────────────────────────────


def _make_status(orch: Orchestrator) -> Interceptor:
    """状态提示（压缩、重试、限流、工作轮）→ 收进卡片.

    这是「工作轮数等信息跑到卡片外」的主要治理点。
    """

    def intercept(agent: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        if not orch.capture_active("notice"):
            return False
        text = _first_str(args, kwargs, "status", "text", "message", "content")
        if not text.strip():
            return False

        if is_noise_status(text):
            # Hermes 自己都认为该留在日志的噪音，卡片和聊天都不展示
            return True

        turn_key = turn_key_for(orch, agent)
        if not turn_key:
            return False

        kind, level = classify_status_text(text)
        if orch.push_notice(turn_key, text, level, as_review=kind == EventKind.REVIEW):
            orch.record_capture_success("notice")
            return True

        orch.record_capture_failure("notice", PUSH_REJECTED)
        return False

    return intercept


def _make_notice(orch: Orchestrator) -> Interceptor:
    """额度 / 系统通知 → 收进卡片.

    Hermes 传入的是 notice 对象而非字符串（见 gateway/run.py:5968），
    优先用 Hermes 自己的渲染函数取文本。
    """

    def intercept(agent: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        if not orch.capture_active("notice"):
            return False
        text = _render_notice(args[0] if args else kwargs.get("notice"))
        if not text.strip() or is_noise_status(text):
            return False
        turn_key = turn_key_for(orch, agent)
        if not turn_key:
            # 无活跃 turn 不是失败：非飞书平台或 turn 已收尾都会走到这里，
            # 计入失败会让自愈层误判并错误降级
            return False
        if orch.push_notice(turn_key, text, NoticeLevel.INFO):
            orch.record_capture_success("notice")
            return True
        orch.record_capture_failure("notice", PUSH_REJECTED)
        return False

    return intercept


def _render_notice(notice: Any) -> str:
    """把 Hermes 的 notice 对象渲染成一行文本."""
    if notice is None:
        return ""
    if isinstance(notice, str):
        return notice
    try:
        from agent.notice_render import render_notice_line  # type: ignore[import-not-found]

        rendered = render_notice_line(notice)
        if isinstance(rendered, str):
            return rendered
    except Exception:
        logger.debug("Hermes notice 渲染不可用，回退 str()", exc_info=True)
    text = str(notice)
    # 形如 <SomeObject at 0x...> 的默认 repr 没有展示价值
    return "" if text.startswith("<") else text


def _make_review(orch: Orchestrator) -> Interceptor:
    """自我改进 / 记忆更新 → 收进卡片.

    参考实现的做法是「延迟到卡片收尾后再单独发一条」，那仍然是游离消息。
    本插件直接收进卡片的 review 段。
    """

    def intercept(agent: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        if not orch.capture_active("review"):
            return False
        text = _first_str(args, kwargs, "message", "text", "content")
        if not text.strip():
            return False
        turn_key = turn_key_for(orch, agent)
        if not turn_key:
            return False
        if orch.push_notice(turn_key, text, NoticeLevel.INFO, as_review=True):
            orch.record_capture_success("review")
            return True
        orch.record_capture_failure("review", PUSH_REJECTED)
        return False

    return intercept


# ── 交互类（只记录状态，不接管交互）────────────────────────────────


def _make_clarify_wrapper(orch: Orchestrator) -> Any:
    """澄清提问 → 卡片状态块 + 原生交互照常进行.

    与其他回调不同，这里需要在原回调**前后**都插入逻辑（开启等待态、
    回填结果），所以不能用 ``_compose`` 的「拦截或透传」模式。

    **刻意不接管交互本身**：澄清是阻塞 Agent 线程的同步调用，接管意味着
    要自己实现等待、超时、结果回填，任何一处做错都会让 Agent 永久挂起。
    """

    def factory(agent: Any, original: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            turn_key: str | None = None
            try:
                if orch.capture_active("clarify"):
                    question = _first_str(args, kwargs, "question")
                    turn_key = turn_key_for(orch, agent)
                    if turn_key and question:
                        choices = kwargs.get("choices")
                        if choices is None and len(args) > 1:
                            choices = args[1]
                        if orch.open_interaction(
                            turn_key,
                            InteractionKind.CLARIFY,
                            question,
                            _choices_preview(choices),
                        ):
                            orch.record_capture_success("clarify")
                        else:
                            orch.record_capture_failure("clarify", PUSH_REJECTED)
            except Exception as error:
                orch.record_capture_failure("clarify", error)
                logger.debug("clarify 进入包装异常", exc_info=True)

            try:
                result = original(*args, **kwargs)
            finally:
                try:
                    if turn_key:
                        orch.close_interaction(
                            turn_key,
                            InteractionKind.CLARIFY,
                            status=InteractionStatus.TIMEOUT,
                        )
                except Exception:
                    logger.debug("clarify 退出包装异常", exc_info=True)

            # 拿到有效回复后把状态改写为「已回复」
            try:
                if turn_key and isinstance(result, str) and result.strip():
                    orch.close_interaction(
                        turn_key,
                        InteractionKind.CLARIFY,
                        status=InteractionStatus.RESOLVED,
                        result=result.strip(),
                    )
            except Exception:
                logger.debug("clarify 结果回填异常", exc_info=True)
            return result

        return wrapper

    return factory


def _choices_preview(choices: Any, limit: int = 5) -> str:
    """把候选项压成一行摘要."""
    if not isinstance(choices, (list, tuple)) or not choices:
        return ""
    items = [str(item).strip() for item in choices[:limit] if str(item).strip()]
    if not items:
        return ""
    suffix = f" 等 {len(choices)} 项" if len(choices) > limit else ""
    return " / ".join(items) + suffix


# ── 织入清单 ──────────────────────────────────────────────────────


def build_factories(orch: Orchestrator) -> dict[str, Any]:
    """构造「属性名 → 包装工厂」映射，交给 AgentWeaver 安装.

    只织入这里列出的回调，其余一律不碰。
    """
    interceptors: dict[str, Interceptor] = {
        "stream_delta_callback": _make_answer(orch),
        "interim_assistant_callback": _make_interim(orch),
        "reasoning_callback": _make_reasoning(orch),
        "tool_progress_callback": _make_tool(orch),
        "status_callback": _make_status(orch),
        "notice_callback": _make_notice(orch),
        "background_review_callback": _make_review(orch),
    }

    factories: dict[str, Any] = {
        name: _make_factory(intercept) for name, intercept in interceptors.items()
    }
    # clarify 需要前后包裹，单独构造
    factories["clarify_callback"] = _make_clarify_wrapper(orch)
    return factories


def _make_factory(intercept: Interceptor) -> Any:
    def factory(agent: Any, original: Any) -> Any:
        return _compose(agent, original, intercept)

    return factory

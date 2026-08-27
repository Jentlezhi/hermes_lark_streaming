"""适配器织入 — 捕获入站消息锚点 + 拦截游离消息.

**必须避开的陷阱**：Hermes 用 ``getattr(type(adapter), "send_exec_approval",
None)`` 检查**类**是否支持按钮审批（gateway/run.py:6323）。如果用代理对象
替换适配器，``type(proxy)`` 上没有这个方法，检查失败 → 飞书审批被静默降级
为纯文本 ``/approve``，且不会有任何报错。

因此这里采用**实例方法绑定**：类保持原样（能力检查照常通过），只把包装函数
写到实例 ``__dict__``（实例属性优先级高于类属性，实际调用命中包装）。两个
需求同时满足。

织入时机由 ``BasePlatformAdapter.__init__`` 提供——适配器一被创建就织入，
不依赖任何 gateway 内部时序。
"""

from __future__ import annotations

import asyncio
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

from ..core import InteractionKind
from ..events import classify_status_text, is_lifecycle_notice, is_noise_status
from ..events.model import EventKind
from ..observability import METRICS, logger
from ..orchestrator import PUSH_REJECTED, Orchestrator

#: 实例上的幂等标记
_WOVEN_FLAG = "_hls_adapter_woven"
#: 织入的适配器方法名。卸载要按这份清单清理实例属性，因此它必须与
#: :func:`_interceptors` 的键完全一致——两处漂移由测试守住
_METHOD_NAMES: tuple[str, ...] = ("send", "send_clarify", "send_exec_approval", "handle_message")
#: 已织入的适配器实例（弱引用）。用 WeakSet 而非强引用：适配器的生命周期
#: 由 Hermes 掌握，插件不该延长它
_WOVEN_INSTANCES: weakref.WeakSet[Any] = weakref.WeakSet()
#: 织入前的 ``BasePlatformAdapter.__init__``，供卸载还原
_original_init: tuple[type, Any] | None = None
#: 入站消息上下文的保留时长
_INBOUND_TTL_SEC = 900.0
_INBOUND_MAX = 512

_FEISHU_PLATFORMS = frozenset({"feishu", "lark"})

#: 拦截判断函数：返回 True 表示已接管，不再调用原方法
Interceptor = Callable[[tuple[Any, ...], dict[str, Any]], bool]


class InboundIndex:
    """最近入站消息索引.

    卡片需要 reply 到用户原消息上（才能形成引用关系、才能进话题），
    但 Agent 回调只带 session/chat 线索，拿不到 message_id。因此在适配器
    入站处先把 ``chat_id → message_id`` 记下来，建卡时取用。
    """

    __slots__ = ("_entries", "_lock")

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, str | None, float]] = {}
        self._lock = threading.Lock()

    def remember(self, chat_id: str, message_id: str, thread_id: str | None) -> None:
        if not chat_id or not message_id:
            return
        with self._lock:
            self._entries[chat_id] = (message_id, thread_id, time.time())
            if len(self._entries) > _INBOUND_MAX:
                self._prune_locked(force=True)

    def take(self, chat_id: str | None) -> tuple[str, str | None] | None:
        if not chat_id:
            return None
        with self._lock:
            entry = self._entries.get(chat_id)
            if entry is None:
                return None
            message_id, thread_id, stamped = entry
            if time.time() - stamped > _INBOUND_TTL_SEC:
                del self._entries[chat_id]
                return None
            return message_id, thread_id

    def _prune_locked(self, *, force: bool = False) -> None:
        now = time.time()
        for key in [k for k, v in self._entries.items() if now - v[2] > _INBOUND_TTL_SEC]:
            del self._entries[key]
        if force and len(self._entries) > _INBOUND_MAX:
            ordered = sorted(self._entries.items(), key=lambda item: item[1][2])
            for key, _ in ordered[: len(self._entries) - _INBOUND_MAX + 1]:
                self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


INBOUND = InboundIndex()


# ── 通用包装 ──────────────────────────────────────────────────────


def _compose(original: Any, intercept: Interceptor) -> Any:
    """把拦截逻辑与原方法组合，自动适配同步 / 异步.

    不能假设适配器方法一定是协程：不同平台实现不一，猜错会导致返回
    协程对象却从未 await，消息静默丢失。这里按实际类型分别包装。
    """
    if asyncio.iscoroutinefunction(original):

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if _safe_intercept(intercept, args, kwargs):
                return None
            return await original(*args, **kwargs)

        return async_wrapper

    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        if _safe_intercept(intercept, args, kwargs):
            return None
        return original(*args, **kwargs)

    return sync_wrapper


def _safe_intercept(intercept: Interceptor, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    """执行拦截判断。任何异常都视为「未接管」，让原方法照常执行.

    这是不丢消息的最后一道保险。
    """
    try:
        return bool(intercept(args, kwargs))
    except Exception:
        logger.debug("拦截判断异常，透传原方法", exc_info=True)
        return False


def _is_feishu(adapter: Any) -> bool:
    platform = getattr(adapter, "platform", None)
    value = getattr(platform, "value", platform)
    return str(value or "").lower() in _FEISHU_PLATFORMS


def _chat_id_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    value = kwargs.get("chat_id")
    if isinstance(value, str) and value:
        return value
    for item in args:
        if isinstance(item, str) and item:
            return item
    return ""


def _text_of(args: tuple[Any, ...], kwargs: dict[str, Any], *names: str) -> str:
    for name in names:
        value = kwargs.get(name)
        if isinstance(value, str):
            return value
    # 位置参数形态通常是 (chat_id, message, ...)，正文在第二个字符串
    strings = [item for item in args if isinstance(item, str)]
    return strings[1] if len(strings) > 1 else ""


# ── 拦截逻辑 ──────────────────────────────────────────────────────


def _make_send_interceptor(orch: Orchestrator) -> Interceptor:
    """通用消息发送：把状态提示与 review 收进卡片.

    降级契约：无活跃卡片、卡片已终态、收纳失败——任一命中都返回 False
    让原方法执行，保证消息永不丢失。
    """

    def intercept(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        if not orch.enabled or not orch.capture_active("notice"):
            return False
        text = _text_of(args, kwargs, "message", "text", "content")
        if not text.strip():
            return False

        if is_noise_status(text):
            # Hermes 自己都认为该留在日志的噪音，卡片和聊天都不展示
            METRICS.incr("adapter.noise_dropped")
            return True

        turn = orch.registry.get_active_by_chat(_chat_id_of(args, kwargs))
        if turn is None:
            return False

        kind, level = classify_status_text(text)
        captured = orch.push_notice(turn.turn_key, text, level, as_review=kind == EventKind.REVIEW)
        if not captured:
            orch.record_capture_failure("notice", PUSH_REJECTED)
            return False

        orch.record_capture_success("notice")
        METRICS.incr("adapter.captured")

        if is_lifecycle_notice(text):
            # gateway 即将关闭：卡片已记下这一条，但**必须放行原生消息**。
            # 此刻事件循环随时会停，卡片更新（100ms 节流 + 一次 API 往返）
            # 很可能来不及；抑制原生输出就会让这条关键通知彻底消失。
            METRICS.incr("adapter.lifecycle_passthrough")
            return False
        return True

    return intercept


def _make_clarify_interceptor(orch: Orchestrator) -> Interceptor:
    """澄清卡片：在流式卡内记录状态，但**始终放行**原生交互卡.

    交互本身不接管——澄清会阻塞 Agent 线程，接管意味着要自己实现等待、
    超时与结果回填，任何一处做错都会让 Agent 永久挂起。
    """

    def intercept(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        if orch.enabled and orch.capture_active("clarify"):
            question = kwargs.get("question")
            turn = orch.registry.get_active_by_chat(_chat_id_of(args, kwargs))
            if turn is not None and isinstance(question, str) and question.strip():
                if orch.open_interaction(turn.turn_key, InteractionKind.CLARIFY, question.strip()):
                    orch.record_capture_success("clarify")
                else:
                    orch.record_capture_failure("clarify", PUSH_REJECTED)
        return False  # 永远放行

    return intercept


def _make_approval_interceptor(orch: Orchestrator) -> Interceptor:
    """审批卡片：在流式卡内记录「等待授权」，**始终放行**原生按钮卡.

    审批是安全边界，接管交互的风险远大于收益。
    """

    def intercept(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        if orch.enabled and orch.capture_active("approval"):
            command = kwargs.get("command")
            description = kwargs.get("description")
            turn = orch.registry.get_active_by_chat(_chat_id_of(args, kwargs))
            if turn is not None and isinstance(command, str) and command.strip():
                title = description if isinstance(description, str) and description.strip() else "命令执行授权"
                if orch.open_interaction(turn.turn_key, InteractionKind.APPROVAL, title, command.strip()):
                    orch.record_capture_success("approval")
                else:
                    orch.record_capture_failure("approval", PUSH_REJECTED)
        return False  # 永远放行

    return intercept


def _make_inbound_interceptor(orch: Orchestrator) -> Interceptor:
    """记录入站消息，为建卡提供 reply 锚点。永不拦截."""

    def intercept(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        event = kwargs.get("event")
        if event is None:
            event = next((item for item in args if hasattr(item, "message_id")), None)
        if event is None:
            return False
        message_id = getattr(event, "message_id", None)
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", None)
        thread_id = getattr(source, "thread_id", None)
        if isinstance(message_id, str) and isinstance(chat_id, str):
            INBOUND.remember(chat_id, message_id, thread_id if isinstance(thread_id, str) else None)
        return False

    return intercept


# ── 织入入口 ──────────────────────────────────────────────────────


def _interceptors(orch: Orchestrator) -> dict[str, Interceptor]:
    """方法名 → 拦截逻辑。只织入实例上确实存在的方法."""
    mapping = {
        "send": _make_send_interceptor(orch),
        "send_clarify": _make_clarify_interceptor(orch),
        "send_exec_approval": _make_approval_interceptor(orch),
        "handle_message": _make_inbound_interceptor(orch),
    }
    # 与卸载用的清单保持同步，防止将来加了拦截却漏了清理
    assert set(mapping) == set(_METHOD_NAMES), "拦截器与 _METHOD_NAMES 不一致"
    return mapping


def weave_adapter(orch: Orchestrator, adapter: Any) -> list[str]:
    """对单个飞书适配器实例织入方法.

    只做实例级绑定，不改类——这样 Hermes 的 ``getattr(type(adapter), ...)``
    能力检查仍然看到原始类方法，不会误判平台能力。
    """
    if not _is_feishu(adapter) or getattr(adapter, _WOVEN_FLAG, False):
        return []

    applied: list[str] = []
    try:
        for name, intercept in _interceptors(orch).items():
            original = getattr(adapter, name, None)
            if original is None or not callable(original):
                continue
            try:
                setattr(adapter, name, _compose(original, intercept))
                applied.append(name)
            except Exception:
                logger.debug("适配器方法织入失败: %s", name, exc_info=True)
        if applied:
            setattr(adapter, _WOVEN_FLAG, True)
            # 弱引用登记，供卸载时清理实例属性。用 WeakSet 而不是强引用：
            # 适配器的生命周期由 Hermes 掌握，插件不该延长它
            _WOVEN_INSTANCES.add(adapter)
            logger.info("飞书适配器已织入: %s", ", ".join(applied))
    except Exception:
        logger.warning("适配器织入异常，保持原生行为", exc_info=True)
        return []
    return applied


def install_adapter_hook(orch: Orchestrator) -> bool:
    """织入 ``BasePlatformAdapter.__init__``，使新建的飞书适配器自动被织入.

    这是唯一需要改动类的地方，且只是包一层构造函数，不改变签名与语义。
    同时对已存在的适配器实例做一次补织入（插件晚于适配器创建时的兜底）。
    """
    try:
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("无法导入 BasePlatformAdapter，适配器织入跳过（卡片将直发会话）")
        return False

    if getattr(BasePlatformAdapter, _WOVEN_FLAG, False):
        return True

    original_init = BasePlatformAdapter.__init__

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        try:
            weave_adapter(orch, self)
        except Exception:
            logger.debug("构造后织入失败", exc_info=True)

    try:
        BasePlatformAdapter.__init__ = patched_init  # type: ignore[method-assign]
        setattr(BasePlatformAdapter, _WOVEN_FLAG, True)
    except Exception:
        logger.warning("BasePlatformAdapter 织入失败", exc_info=True)
        return False

    global _original_init
    _original_init = (BasePlatformAdapter, original_init)

    _weave_existing_adapters(orch, BasePlatformAdapter)
    return True


def uninstall_adapter_hook() -> bool:
    """还原 ``__init__`` 并清掉已织入实例上的包装方法.

    两件事都要做：只还原类而不清实例，已存在的适配器仍会走包装；只清实例
    而不还原类，之后新建的适配器又会被重新织入。
    """
    global _original_init
    if _original_init is None:
        return False
    base_class, original_init = _original_init

    try:
        base_class.__init__ = original_init  # type: ignore[misc]
        if _WOVEN_FLAG in vars(base_class):
            delattr(base_class, _WOVEN_FLAG)
    except Exception:
        logger.debug("适配器构造函数还原失败", exc_info=True)
        return False

    # 清实例：删掉实例 __dict__ 里的包装，属性查找自然回落到类方法
    for adapter in list(_WOVEN_INSTANCES):
        for name in _METHOD_NAMES:
            try:
                if name in vars(adapter):
                    delattr(adapter, name)
            except Exception:
                logger.debug("适配器实例方法还原失败: %s", name, exc_info=True)
        try:
            if _WOVEN_FLAG in vars(adapter):
                delattr(adapter, _WOVEN_FLAG)
        except Exception:
            logger.debug("适配器实例标记清理失败", exc_info=True)
    _WOVEN_INSTANCES.clear()
    INBOUND.clear()
    _original_init = None
    return True


def _weave_existing_adapters(orch: Orchestrator, base_class: type) -> None:
    """补织入已经创建出来的适配器实例.

    插件加载时机若晚于适配器构造，仅靠 ``__init__`` 织入会漏掉它们。
    用 gc 扫描一次实例，代价只在启动时付一次。
    """
    try:
        import gc

        for obj in gc.get_objects():
            try:
                if isinstance(obj, base_class) and _is_feishu(obj):
                    weave_adapter(orch, obj)
            except Exception:
                continue
    except Exception:
        logger.debug("补织入已有适配器失败", exc_info=True)

"""运行时织入 — 不修改 Hermes 源码，在类上安装描述符拦截回调装配.

**原理**：Hermes 用实例属性赋值装配 Agent 回调（``agent.stream_delta_callback
= cb``，见 gateway/run.py:5953）。Python 规定**数据描述符（定义了 __set__ 的
类属性）优先于实例 __dict__**，因此在 ``AIAgent`` 类上装一个描述符，就能在
每次赋值时把原回调包装掉。

**为什么这样做**：Hermes 升级路径是 git reset（见 gateway/run.py:20-25 注释），
会抹掉写进源码的任何 marker。而本方案不写 Hermes 的任何文件，升级不受影响，
卸载零残留。完整论证见 docs/03-升级韧性设计.md。

**安全底线**：安装采用「全有或全无」——先做完整自检，任一环节不通过就完全
不装，让 Hermes 保持原生行为。绝不留下半织入状态。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..observability import logger

#: 描述符在实例 __dict__ 中的存储前缀。
#: 必须与属性名不同：数据描述符会遮蔽同名实例属性，同名存储将永远读不回来。
_STORAGE_PREFIX = "_hls_cb_"
#: 类级标记，用于幂等判定与卸载
_INSTALLED_FLAG = "_hls_weave_installed"

WrapperFactory = Callable[[Any, Any], Any]

#: 装配观测器。由 :mod:`.plugin` 在启动时注入为自愈层的记录函数。
#: 之所以用注入而不是直接 import selfheal：本模块属 L0 桥接层，
#: 让它反向依赖自愈层会破坏单向依赖，装配关系交给 plugin 统一编排。
_observer: Callable[[str], None] | None = None


def set_observer(observer: Callable[[str], None] | None) -> None:
    """注册回调装配观测器.

    每当 Hermes 真实装配某个回调（描述符 ``__set__`` 被触发）就调用一次。
    这是判断「Hermes 是否还在用这个回调」的唯一可靠信号——回调是实例属性，
    静态检查看不到它是否仍在装配路径上。
    """
    global _observer
    _observer = observer


class WeaveError(RuntimeError):
    """织入失败。抛出后调用方必须完全放弃织入，不得部分启用."""


class _CallbackDescriptor:
    """拦截单个回调属性赋值的数据描述符.

    ``__set__`` 存在使其成为数据描述符，优先级高于实例 ``__dict__``，
    这是整个方案成立的语言学基础。
    """

    __slots__ = ("_factory", "_name", "_silent", "_storage")

    def __init__(self, name: str, factory: WrapperFactory, *, silent: bool = False) -> None:
        self._name = name
        self._storage = _STORAGE_PREFIX + name
        self._factory = factory
        # 自测探针必须静默：探针赋值不是 Hermes 的真实装配，上报会污染
        # 自愈层的观测集合，让「历史装配过哪些回调」这个基线失去意义
        self._silent = silent

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return obj.__dict__.get(self._storage)

    def __set__(self, obj: Any, value: Any) -> None:
        observer = None if self._silent else _observer
        if observer is not None:
            try:
                observer(self._name)
            except Exception:
                # 观测失败绝不影响织入：自愈层是旁路的
                logger.debug("装配观测上报失败: %s", self._name, exc_info=True)
        if value is None:
            obj.__dict__[self._storage] = None
            return
        try:
            wrapped = self._factory(obj, value)
        except Exception:
            # 包装失败必须原样放行：宁可不接管，也不能让 Hermes 的回调消失
            logger.debug("回调包装失败，原样透传: %s", self._name, exc_info=True)
            wrapped = value
        obj.__dict__[self._storage] = wrapped

    def __delete__(self, obj: Any) -> None:
        obj.__dict__.pop(self._storage, None)


class WeaveReport:
    """织入结果，供 status 命令与日志展示."""

    __slots__ = ("attached", "detail", "missing", "ok")

    def __init__(self) -> None:
        self.ok = False
        self.attached: list[str] = []
        self.missing: list[str] = []
        self.detail = ""

    def summary(self) -> str:
        if self.ok:
            return f"ok（{len(self.attached)} 个回调已织入）"
        return f"FAILED — {self.detail}"


class AgentWeaver:
    """管理 ``AIAgent`` 类上的描述符安装与卸载."""

    __slots__ = ("_installed", "_lock", "_target")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._installed: dict[str, Any] = {}
        self._target: type | None = None

    @property
    def installed(self) -> bool:
        return bool(self._installed)

    # ── 目标定位 ──────────────────────────────────────────────────

    @staticmethod
    def locate_agent_class() -> type:
        """定位 Hermes 的 AIAgent 类.

        依据 run_agent.py:17 的官方用法 ``from run_agent import AIAgent``。
        """
        try:
            import run_agent  # type: ignore[import-not-found]
        except ImportError as error:
            raise WeaveError(f"无法导入 Hermes 的 run_agent 模块：{error}") from error

        agent_class = getattr(run_agent, "AIAgent", None)
        if agent_class is None or not isinstance(agent_class, type):
            raise WeaveError("run_agent 模块中未找到 AIAgent 类")
        return agent_class

    # ── 自检 ──────────────────────────────────────────────────────

    @staticmethod
    def preflight(agent_class: type, names: list[str]) -> None:
        """安装前自检。任一项不通过即抛 WeaveError，调用方完全放弃织入."""
        # 1. 必须允许动态实例属性。有 __slots__ 且未声明 __dict__ 时，
        #    描述符仍可安装但存储会失败，且可能破坏原有 slot 语义。
        if _has_blocking_slots(agent_class):
            raise WeaveError(
                "AIAgent 使用了 __slots__ 且未暴露 __dict__，无法安装描述符"
            )

        # 2. 目标属性不得已被其他数据描述符（property / slot）占用，
        #    覆盖它们会破坏 Hermes 自身的语义。
        for name in names:
            existing = _lookup_class_attr(agent_class, name)
            if existing is None:
                continue
            if isinstance(existing, _CallbackDescriptor):
                continue
            if hasattr(type(existing), "__set__"):
                raise WeaveError(f"属性 {name} 已被 {type(existing).__name__} 占用，不能织入")

        # 3. 验证描述符机制在该类的元类下确实生效
        _verify_descriptor_mechanics()

    # ── 安装与卸载 ────────────────────────────────────────────────

    def install(self, factories: dict[str, WrapperFactory]) -> WeaveReport:
        """安装描述符。全有或全无：任一步失败即回滚已安装项."""
        report = WeaveReport()
        names = list(factories)

        with self._lock:
            if self._installed:
                report.ok = True
                report.attached = list(self._installed)
                report.detail = "已安装（幂等跳过）"
                return report

            try:
                agent_class = self.locate_agent_class()
                self.preflight(agent_class, names)
            except WeaveError as error:
                report.detail = str(error)
                return report
            except Exception as error:
                report.detail = f"自检异常：{error}"
                return report

            applied: list[str] = []
            try:
                for name, factory in factories.items():
                    previous = agent_class.__dict__.get(name, _MISSING)
                    setattr(agent_class, name, _CallbackDescriptor(name, factory))
                    self._installed[name] = previous
                    applied.append(name)
            except Exception as error:
                # 回滚：绝不留半织入状态
                self._rollback_locked(agent_class, applied)
                report.detail = f"安装失败已回滚：{error}"
                return report

            self._target = agent_class
            try:
                setattr(agent_class, _INSTALLED_FLAG, True)
            except Exception:
                logger.debug("写入织入标记失败（不影响功能）", exc_info=True)

            report.ok = True
            report.attached = applied
            return report

    def uninstall(self) -> None:
        """还原类属性到织入前的状态."""
        with self._lock:
            agent_class = self._target
            if agent_class is None or not self._installed:
                return
            self._rollback_locked(agent_class, list(self._installed))
            self._target = None
            try:
                delattr(agent_class, _INSTALLED_FLAG)
            except Exception:
                pass

    def _rollback_locked(self, agent_class: type, names: list[str]) -> None:
        for name in names:
            previous = self._installed.pop(name, _MISSING)
            try:
                if previous is _MISSING:
                    delattr(agent_class, name)
                else:
                    setattr(agent_class, name, previous)
            except Exception:
                logger.debug("回滚属性失败: %s", name, exc_info=True)


class _Missing:
    """哨兵：区分「原本没有该属性」与「原本是 None」."""

    __slots__ = ()


_MISSING = _Missing()


# ── 内部工具 ──────────────────────────────────────────────────────


def _has_blocking_slots(cls: type) -> bool:
    """类及其基类是否使用了会阻断动态属性的 __slots__.

    只要继承链上任一类未定义 __slots__（如继承自 object 的普通类），
    实例就有 __dict__，动态属性可用。
    """
    for base in cls.__mro__:
        if base is object:
            continue
        if "__slots__" not in base.__dict__:
            # 该层未声明 __slots__ → 实例带 __dict__
            return False
        slots = base.__dict__.get("__slots__")
        if isinstance(slots, str):
            slots = (slots,)
        if slots and "__dict__" in tuple(slots):
            return False
    # 继承链上每层都声明了 __slots__ 且都不含 __dict__
    return True


def _lookup_class_attr(cls: type, name: str) -> Any:
    for base in cls.__mro__:
        if name in base.__dict__:
            return base.__dict__[name]
    return None


def _verify_descriptor_mechanics() -> None:
    """用探针类验证数据描述符确实优先于实例 __dict__.

    这是对语言行为的运行时确认。虽然 CPython 保证该语义，但在异常的
    元类或解释器实现下可能不成立——与其在生产上静默错乱，不如在此拦下。
    """
    marker: list[Any] = []

    class _Probe:
        pass

    def _factory(_obj: Any, value: Any) -> Any:
        marker.append(value)
        return f"wrapped::{value}"

    # silent=True：这是自测探针，其赋值不代表 Hermes 的真实装配行为
    setattr(_Probe, "probe_cb", _CallbackDescriptor("probe_cb", _factory, silent=True))
    probe = _Probe()
    probe.probe_cb = "original"  # type: ignore[attr-defined]

    if not marker or probe.probe_cb != "wrapped::original":  # type: ignore[attr-defined]
        raise WeaveError("数据描述符未按预期拦截属性赋值，环境不支持运行时织入")


# 全局织入器：AIAgent 类是进程级单例，织入也必须是进程级
WEAVER = AgentWeaver()

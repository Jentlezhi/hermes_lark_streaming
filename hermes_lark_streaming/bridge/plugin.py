"""Hermes 官方插件入口.

这是整个插件唯一的启动点，由 Hermes 通过 entry point 调用：

.. code-block:: toml

    [project.entry-points."hermes_agent.plugins"]
    hermes-lark-streaming = "hermes_lark_streaming.bridge.plugin"

**为什么用 entry point 而不是改源码**：entry point 由 pip 写入 site-packages，
不在 Hermes 的 git 工作区内，因此 ``hermes update`` 的 git reset 不会影响它。
这是「升级免疫」的基础，详见 docs/03-升级韧性设计.md。

**失败即完全退出**：任何一步出问题都不得让 Hermes 受影响。启动失败时插件
彻底不干预，Hermes 保持原生行为——用户只是看不到卡片，消息不会丢。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from .. import __version__
from ..observability import logger
from ..orchestrator import get_orchestrator


class BootstrapReport:
    """启动结果，供 ``status`` 命令与日志展示."""

    __slots__ = (
        "adapter_hooked",
        "callbacks",
        "conversation",
        "detail",
        "diagnosis",
        "interrupt_hooked",
        "ok",
        "subagent",
        "watcher",
    )

    def __init__(self) -> None:
        self.ok = False
        self.callbacks: list[str] = []
        self.conversation: list[str] = []
        self.adapter_hooked = False
        self.interrupt_hooked = False
        self.subagent: list[str] = []
        self.watcher = False
        self.detail = ""
        # 失败时由自愈层用历史基线生成的精确对照，例如
        # 「上次成功织入 8 个回调（Hermes 0.20.5）· 本次缺失 reasoning_callback」
        self.diagnosis = ""

    def render(self) -> str:
        lines = [f"hermes-lark-streaming v{__version__}"]
        if self.ok:
            lines.append(f"  织入状态: ok（{len(self.callbacks)} 个回调）")
            lines.append(f"  回调织入: {', '.join(self.callbacks) if self.callbacks else '无'}")
            lines.append(f"  终态织入: {', '.join(self.conversation) if self.conversation else '仅兜底守护'}")
            lines.append(f"  适配器织入: {'ok' if self.adapter_hooked else '未生效（无 reply 锚点，卡片直发会话）'}")
            lines.append(
                f"  中断织入: {'ok（可区分 /stop 与新消息接续）' if self.interrupt_hooked else '未生效（由空闲守护兜底）'}"
            )
            lines.append(f"  子任务织入: {', '.join(self.subagent) if self.subagent else '未生效（子任务不进卡片）'}")
            lines.append(f"  空闲守护: {'运行中' if self.watcher else '未启动'}")
        else:
            lines.append(f"  织入状态: FAILED — {self.detail}")
            if self.diagnosis:
                lines.append(f"  历史对照: {self.diagnosis}")
            lines.append("  影响: 流式卡片不可用，Hermes 保持原生行为，消息不受影响")
        return "\n".join(lines)


def detect_hermes_version() -> str:
    """尽力探测 Hermes 版本，仅用于报告展示.

    **不作为环境指纹**：语义版本变化远慢于实际代码（同一个 0.20.5 可能对应
    几十个 commit），真正的指纹由织入的回调集合计算，见
    :func:`..selfheal.healer._digest_of`。
    """
    try:
        from hermes_cli import __version__ as version  # type: ignore[import-not-found]
    except Exception:
        return ""
    return version.strip() if isinstance(version, str) else ""


_report = BootstrapReport()
_bootstrap_lock = threading.Lock()
_bootstrapped = False


def last_report() -> BootstrapReport:
    return _report


def register(ctx: Any = None) -> None:
    """Hermes 插件注册入口.

    ``ctx`` 由 Hermes 传入，当前实现不依赖它的任何字段——这是刻意的：
    ctx 的结构随 Hermes 版本变化，不依赖它可以少一个失效来源。
    """
    try:
        bootstrap()
    except Exception:
        # 绝不把异常抛回 Hermes 的插件加载流程
        logger.warning("插件注册失败，已跳过", exc_info=True)


def bootstrap(*, force: bool = False) -> BootstrapReport:
    """执行织入。幂等：重复调用直接返回上次结果."""
    global _bootstrapped

    with _bootstrap_lock:
        if _bootstrapped and not force:
            return _report

        report = BootstrapReport()
        try:
            orch = get_orchestrator()

            if not orch.config.enabled:
                report.detail = "配置中 streaming.enabled 未开启"
                _finish(report, record=False)
                return report
            if not orch.config.has_credentials:
                report.detail = "飞书凭据未配置（FEISHU_APP_ID / FEISHU_APP_SECRET）"
                _finish(report, record=False)
                return report

            # ── 1. 回调织入（内容流式 + 游离消息收纳）──
            from .callbacks import build_factories
            from .weave import WEAVER, set_observer

            # 先接上装配观测：描述符装好后 Hermes 的第一次赋值就该被记下，
            # 这是自愈层判断「哪些回调仍在装配路径上」的唯一信号来源
            set_observer(orch.healer.record_observed)

            weave_report = WEAVER.install(build_factories(orch))
            if not weave_report.ok:
                report.detail = weave_report.detail
                _finish(report)
                return report
            report.callbacks = weave_report.attached

            # ── 2. 对话方法织入（精确终态）──
            from .lifecycle import install_conversation_hook

            report.conversation = install_conversation_hook(orch)

            # ── 3. 适配器织入（入站锚点 + 游离消息拦截）──
            from .adapter import install_adapter_hook

            report.adapter_hooked = install_adapter_hook(orch)

            # ── 4. 中断织入（精确区分 /stop 与被新消息接续）──
            from .interrupt import install_interrupt_hook

            report.interrupt_hooked = install_interrupt_hook(orch)

            # ── 5. 子 Agent 生命周期（委派任务收进卡片）──
            from .subagent import install_subagent_hook

            report.subagent = install_subagent_hook(orch)

            # ── 6. 订阅额度查询器（默认关闭，需显式开启）──
            from .usage import fetch_usage_line

            orch.set_usage_provider(lambda: fetch_usage_line(orch.config))

            # ── 7. 兜底守护 ──
            report.watcher = _start_watcher(orch)

            # 立刻落一次心跳。守护要等第一次事件循环调度才会写，若 gateway
            # 已启动但还没来过消息，``activity`` 命令就分不清「gateway 没跑」
            # 和「gateway 空闲」——两种情况都是「无心跳记录」。开机即写一条，
            # 把这个歧义消掉
            _publish_initial_activity(orch)

            report.ok = True
            _bootstrapped = True
            logger.info("插件织入完成: %s", ", ".join(report.callbacks))
        except Exception as error:
            report.detail = f"启动异常：{error}"
            logger.warning("插件启动失败，Hermes 保持原生行为", exc_info=True)
            _rollback()

        _finish(report)
        return report


def _finish(report: BootstrapReport, *, record: bool = True) -> None:
    global _report
    _report = report
    if record:
        # 配置未开启 / 凭据缺失属于「没启用」，不是织入失败，不该污染
        # 自愈层的织入历史——否则会把用户主动关闭记成环境退化
        _record_experience(report)
    if not report.ok:
        logger.info("hermes-lark-streaming 未启用: %s", report.detail)


def _record_experience(report: BootstrapReport, orch: Any = None) -> None:
    """把织入结果交给自愈层，失败时用历史基线生成精确诊断.

    失败诊断的价值在于把「织入失败」这个笼统结论，变成「相比上次成功的
    8 个回调，本次缺失 reasoning_callback」——这是升级后排查的起点。

    ``orch`` 仅供测试注入；生产路径走全局单例。
    """
    try:
        healer = (orch or get_orchestrator()).healer
    except Exception:
        logger.debug("自愈层不可用，跳过织入记录", exc_info=True)
        return

    try:
        if report.ok:
            healer.record_weave(
                ok=True,
                hermes_version=detect_hermes_version(),
                callbacks=report.callbacks,
                conversation=report.conversation,
            )
            return

        healer.record_weave(ok=False, detail=report.detail)
        last = healer.last_success()
        if not last:
            return

        version = last.get("hermes_version") or ""
        summary = f"上次成功织入 {len(last.get('callbacks') or [])} 个回调"
        if version:
            summary += f"（Hermes {version}）"
        parts = [summary]
        # 只在本次确实织入了部分回调时才做集合对照。织入是全有或全无的，
        # 完全失败时 callbacks 为空，此时对照会把全部基线误报成「缺失」，
        # 而真实原因可能只是 __slots__ 或描述符冲突
        if report.callbacks:
            missing, added = healer.regression(report.callbacks)
            if missing:
                parts.append(f"本次缺失: {', '.join(missing)}")
            if added:
                parts.append(f"本次新增: {', '.join(added)}")
        report.diagnosis = " · ".join(parts)
    except Exception:
        logger.debug("自愈层记录织入结果失败", exc_info=True)


def _start_watcher(orch: Any) -> bool:
    """启动空闲守护.

    守护需要事件循环，而插件加载可能早于循环启动。此处尝试一次，失败也无妨：
    编排器会在首次拿到事件循环时自动补启动。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("事件循环尚未就绪，空闲守护将在首次调度时自动补启动")
        return False

    orch.remember_loop(loop)
    return bool(orch.ensure_watcher(loop))


def _publish_initial_activity(orch: Any) -> None:
    """织入成功后立即落一条心跳（当前必然是空闲态）.

    完全旁路：写失败只记 debug。心跳只服务于 ``activity`` 命令的升级前检查，
    它的失败不该影响任何卡片行为。
    """
    try:
        orch.publish_activity()
    except Exception:
        logger.debug("初始心跳写入失败", exc_info=True)


def _rollback() -> None:
    """还原所有已安装的织入，绝不留半织入状态.

    必须覆盖**全部**织入点：回调描述符之外还有四处类方法替换。漏掉任何一处，
    ``selftest`` 声称的「演练后回滚」就名不副实，而半卸载状态比不卸载更难查。
    每一步独立容错——一处还原失败不应阻止其余还原。
    """
    steps: list[tuple[str, Any]] = []
    try:
        from .adapter import uninstall_adapter_hook
        from .interrupt import uninstall_interrupt_hook
        from .lifecycle import uninstall_conversation_hook
        from .subagent import uninstall_subagent_hook
        from .weave import WEAVER, set_observer

        steps = [
            ("装配观测", lambda: set_observer(None)),
            ("回调描述符", WEAVER.uninstall),
            ("对话主方法", uninstall_conversation_hook),
            ("适配器", uninstall_adapter_hook),
            ("中断", uninstall_interrupt_hook),
            ("子任务", uninstall_subagent_hook),
        ]
    except Exception:
        logger.debug("加载卸载入口失败", exc_info=True)
        return

    for label, action in steps:
        try:
            action()
        except Exception:
            logger.debug("回滚失败: %s", label, exc_info=True)


def teardown() -> None:
    """卸载织入（仅供测试与手动排障使用）."""
    global _bootstrapped
    with _bootstrap_lock:
        _rollback()
        _bootstrapped = False

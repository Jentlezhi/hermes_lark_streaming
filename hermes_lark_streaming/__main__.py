"""命令行入口.

.. code-block:: bash

    $HERMES_PYTHON -m hermes_lark_streaming status    # 环境与配置总览
    $HERMES_PYTHON -m hermes_lark_streaming verify    # 验证织入前提（不改任何东西）
    $HERMES_PYTHON -m hermes_lark_streaming doctor    # 详细诊断与修复建议
    $HERMES_PYTHON -m hermes_lark_streaming selftest  # 在本进程实际演练一次织入
    $HERMES_PYTHON -m hermes_lark_streaming heal      # 查看自愈经验
    $HERMES_PYTHON -m hermes_lark_streaming activity  # 升级前检查：有没有任务在跑

**注意：没有 install / uninstall 命令。** 本插件采用运行时织入，不修改 Hermes
任何文件，``pip install`` 即生效、``pip uninstall`` 即停止一切干预，无需额外步骤。
唯一会留下的是插件自己的状态目录 ``<hermes_home>/hermes-lark-streaming/``
（自愈经验 + 运行心跳），与 Hermes 文件零交集，删除即彻底干净。
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__

#: 旧版插件在 Hermes 源码里留下的注入标记。检测到即提示先卸载，避免重复卡片
_LEGACY_MARKERS = ("# HERMES_LARK_", "# HFC_", "# HERMES_FEISHU_CARD_")

#: 本插件在 Hermes 插件系统里注册的模块路径，须与 pyproject.toml 的
#: ``[project.entry-points."hermes_agent.plugins"]`` 保持一致。参考实现 HLS
#: 的分发名与顶层包名和本插件完全相同，只有这个值不同，因此它是区分
#: 「venv 里装的是哪一个」的唯一精确判据
_PLUGIN_ENTRY_TARGET = "hermes_lark_streaming.bridge.plugin"

_OK = "✔"
_WARN = "!"
_FAIL = "✘"


def main() -> int:
    args = sys.argv[1:]
    if not args:
        _usage()
        return 0

    commands: dict[str, Callable[[], int]] = {
        "status": cmd_status,
        "verify": cmd_verify,
        "doctor": cmd_doctor,
        "selftest": cmd_selftest,
        "heal": cmd_heal,
        "activity": cmd_activity,
        "version": cmd_version,
    }
    handler = commands.get(args[0])
    if handler is None:
        print(f"未知命令: {args[0]}")
        _usage()
        return 1
    return handler()


def _usage() -> None:
    print(f"hermes-lark-streaming v{__version__}")
    print()
    print("用法: python -m hermes_lark_streaming <命令>")
    print()
    print("命令:")
    print("  status    环境、配置与冲突检查")
    print("  verify    验证运行时织入的前提条件（只读）")
    print("  doctor    详细诊断并给出修复建议")
    print("  selftest  在当前进程实际演练一次织入")
    print("  heal      查看自愈经验；`heal reset` 清空重学")
    print("  activity  升级前检查：当前有没有任务在跑")
    print("  version   显示版本")
    print()
    print("本插件不修改 Hermes 源码，因此没有 install / uninstall 命令：")
    print("  pip install 即生效，pip uninstall 即停止一切干预。")
    print("  唯一留下的是自愈状态目录 <hermes_home>/hermes-lark-streaming/，删除即净。")


def cmd_version() -> int:
    print(__version__)
    return 0


#: 心跳新鲜度阈值。空闲守护每 15 秒写一次，取 4 倍留足余量——判早了会把
#: 正在跑的 gateway 误报成已停，那正是这个命令最不该出的错
_ACTIVITY_FRESH_SEC = 60

#: 活跃状态的中文名。只列活跃态，终态不会出现在心跳里
_STATE_LABELS = {
    "idle": "刚创建",
    "creating": "正在建卡",
    "streaming": "流式输出中",
    "waiting": "等待你确认",
    "finalizing": "正在收尾",
}


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rest}s" if rest else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m" if minutes else f"{hours}h"


def _process_alive(pid: int) -> bool:
    """进程是否还在。``kill(pid, 0)`` 只做存在性与权限检查，不发送信号.

    pid 会被系统复用，所以这个判定**只用来加强「可以升级」的结论**——
    发现写心跳的进程已退出时敢直接放行；反之不把「pid 存在」当作
    「真的有任务在跑」的独立证据，那仍然由心跳新鲜度决定。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在，只是不属于当前用户
        return True
    except Exception:
        # 判不出来就当它活着，宁可让用户多等也不误导他去升级
        return True
    return True


def _activity_verdict(data: Any, age: int) -> tuple[bool, str]:
    """给出升级建议，返回 ``(现在是否适合升级, 说明)``.

    四种情形分开判断，因为「文件不存在」「心跳过期」「写心跳的进程已退出」
    「有任务在跑」对用户意味着完全不同的动作。
    """
    if data is None:
        return True, "尚无活动记录（gateway 未运行本插件，或插件尚未启用）"
    if age > _ACTIVITY_FRESH_SEC:
        return True, f"心跳已过期 {_fmt_duration(age)}，gateway 应该已经停了，可以直接升级"

    pid = 0
    if isinstance(data, dict):
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
    if pid and not _process_alive(pid):
        # 典型来源：一次 selftest 演练，或 gateway 崩溃后残留的新鲜心跳。
        # 不识别这种情况会让用户白等一轮
        return True, f"心跳来自已退出的进程 {pid}（如一次 selftest 演练），可以直接升级"

    active = data.get("active") if isinstance(data, dict) else None
    active = [item for item in active if isinstance(item, dict)] if isinstance(active, list) else []
    if not active:
        return True, "当前无活跃卡片，可以安全执行 hermes update"

    oldest = max((int(item.get("age_sec") or 0) for item in active), default=0)
    return False, (
        f"当前有 {len(active)} 个任务在跑（最久已 {_fmt_duration(oldest)}），"
        "建议等它们结束后再执行 hermes update"
    )


def cmd_activity() -> int:
    """升级前检查：现在有没有任务在跑.

    数据来自 gateway 落盘的心跳——活跃 turn 只存在于 gateway 进程内存里，
    本命令是另一个进程，读不到它的内存，只能读这份心跳。
    """
    import time as _time

    from .config import hermes_home
    from .selfheal import activity_path, read_activity

    home = hermes_home()
    data = read_activity(home)
    age = max(0, int(_time.time() - int(data.get("at") or 0))) if isinstance(data, dict) else 0

    print(f"hermes-lark-streaming v{__version__} — 运行活动")
    print()
    print(f"  数据来源: {activity_path(home)}")

    if data is None:
        print("  Gateway: 无心跳记录")
    elif age > _ACTIVITY_FRESH_SEC:
        print(f"  Gateway: 未在运行（心跳停在 {_fmt_duration(age)} 前）")
    else:
        print(f"  Gateway: 运行中（pid {data.get('pid', '?')}，心跳 {_fmt_duration(age)} 前）")

    active = data.get("active") if isinstance(data, dict) else None
    active = [item for item in active if isinstance(item, dict)] if isinstance(active, list) else []
    fresh = data is not None and age <= _ACTIVITY_FRESH_SEC

    print()
    if fresh and active:
        print(f"  活跃卡片: {len(active)} 个")
        for item in active:
            state = _STATE_LABELS.get(str(item.get("state") or ""), str(item.get("state") or "未知"))
            parts = [
                f"会话 {item.get('chat') or '-'}",
                state,
                f"已运行 {_fmt_duration(int(item.get('age_sec') or 0))}",
            ]
            action = str(item.get("action") or "").strip()
            if action:
                parts.append(f"当前 {action}")
            idle = int(item.get("idle_sec") or 0)
            if idle >= 30:
                parts.append(f"已 {_fmt_duration(idle)} 无更新")
            print(f"    · {' · '.join(parts)}")
    elif fresh:
        print("  活跃卡片: 无")

    ok, detail = _activity_verdict(data, age)
    print()
    print(f"  升级建议: {_OK if ok else _WARN} {detail}")
    return 0


def _build_healer(home: Path) -> Any:
    """按当前配置构造 healer.

    刻意不经 ``get_orchestrator()``：那会拉起传输层并要求飞书 SDK，而查看
    经验这件事在 SDK 缺失的环境里同样应该可用。
    """
    from .config import Config
    from .selfheal import get_healer

    cfg = Config(home)
    return get_healer(
        home,
        __version__,
        enabled=cfg.selfheal_enabled,
        degrade_threshold=cfg.degrade_after_failures,
        probe_interval=cfg.selfheal_probe_interval,
    )


def cmd_heal() -> int:
    """查看自愈经验；``heal reset`` 清空重学."""
    from .config import hermes_home

    home = hermes_home()
    try:
        healer = _build_healer(home)
    except Exception as error:
        print(f"{_FAIL} 自愈层不可用: {error}")
        return 1

    if sys.argv[2:3] == ["reset"]:
        healer.reset()
        print("已清空自愈经验（降级记录与织入指纹）。下次 gateway 启动重新学习。")
        return 0

    print(f"hermes-lark-streaming v{__version__} — 自愈经验")
    print()
    print(healer.render())

    notes = _explicit_override_notes(home, healer)
    if notes:
        print()
        print("  配置优先说明:")
        for note in notes:
            print(f"    {_WARN} {note}")

    print()
    print("说明")
    print("  降级是插件的自我保护：某类消息持续收纳失败时只关闭该类，")
    print("  其余能力照常工作，结论落盘后下次启动直接生效，并会自动试探恢复。")
    print("  用 `heal reset` 可清空经验强制重学（例如你已修好根因）。")
    return 0


def _explicit_override_notes(home: Path, healer: Any) -> list[str]:
    """列出「用户显式开启、但持续失败」的能力.

    红线要求：插件不改用户配置，只说明。用户写死 ``capture.notice: true`` 时
    即使连续失败也继续尝试——这里把这个事实讲清楚，避免用户误以为插件
    已经自动处理掉了。
    """
    from .config import Config

    cfg = Config(home)
    notes: list[str] = []
    try:
        capabilities = healer.snapshot()["capabilities"]
    except Exception:
        return notes
    for name, entry in sorted(capabilities.items()):
        if not entry.get("total"):
            continue
        try:
            explicit = cfg.capture_explicit(name)
        except Exception:
            continue
        if explicit is True:
            notes.append(
                f"{name}: 配置里显式开启，已累计失败 {entry['total']} 次但仍在尝试"
                "（用户配置优先于学习经验，插件不会自动关闭它）"
            )
    return notes


# ── 环境探测 ──────────────────────────────────────────────────────


def _hermes_python() -> Path | None:
    """定位 Hermes 自带的 Python 解释器."""
    cli = shutil.which("hermes")
    if cli:
        try:
            text = Path(cli).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        import re

        match = re.search(r"""exec\s+["']([^"']+)["']""", text)
        if match:
            venv_bin = Path(match.group(1)).parent
            for name in ("python3", "python"):
                candidate = venv_bin / name
                if candidate.exists():
                    return candidate
        match = re.match(r"^#!\s*(\S+)", text)
        if match:
            candidate = Path(match.group(1))
            if candidate.exists() and "python" in candidate.name.lower():
                return candidate

    from .config import hermes_home

    for root in (hermes_home() / "hermes-agent", Path("/usr/local/lib/hermes-agent")):
        for parts in (("venv", "bin", "python3"), (".venv", "bin", "python3")):
            candidate = root.joinpath(*parts)
            if candidate.exists():
                return candidate
    return None


def _hermes_install_dir() -> Path | None:
    from .config import hermes_home

    for root in (hermes_home() / "hermes-agent", Path("/usr/local/lib/hermes-agent")):
        if (root / "gateway" / "run.py").exists():
            return root
    return None


#: 本插件在 Hermes 插件表里可能出现的名字形态（entry point 名 / 包名）
_PLUGIN_NAMES = ("hermes-lark-streaming", "hermes_lark_streaming")


def _plugin_registration_status(home: Path) -> tuple[bool, list[str]]:
    """检查 Hermes 的插件加载器是否会接受本插件.

    这是最容易踩、且症状最隐蔽的一个坑：Hermes 的 pip 插件是**白名单**机制
    （``hermes_cli/plugins.py`` 的 ``_get_enabled_plugins`` 明确写着
    "Plugins are opt-in by default"），且 ``plugins.disabled`` 命中即跳过。
    两者任一不满足，``register()`` 就永远不会被调用——插件完全静默不工作，
    日志里也不会有本插件的任何痕迹。

    返回 ``(是否会被加载, 需要执行的修复动作列表)``。
    """
    import yaml

    problems: list[str] = []
    path = home / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, yaml.YAMLError) as error:
        return False, [f"无法读取 {path}：{error}"]

    section = raw.get("plugins") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return False, [
            f"在 {path} 中新增 plugins.enabled 并加入 hermes-lark-streaming"
            "（Hermes 的 pip 插件是白名单机制，未列出则不加载）"
        ]

    enabled = section.get("enabled")
    enabled_set = {item for item in enabled if isinstance(item, str)} if isinstance(enabled, list) else set()
    disabled = section.get("disabled")
    disabled_set = {item for item in disabled if isinstance(item, str)} if isinstance(disabled, list) else set()

    hit_disabled = [name for name in _PLUGIN_NAMES if name in disabled_set]
    if hit_disabled:
        problems.append(f"从 {path} 的 plugins.disabled 中移除 {hit_disabled[0]}（显式禁用优先于白名单）")

    if not any(name in enabled_set for name in _PLUGIN_NAMES):
        problems.append(f"把 hermes-lark-streaming 加入 {path} 的 plugins.enabled 列表")

    return not problems, problems


def _detect_distribution_conflict() -> tuple[str, str]:
    """检查 venv 里已安装的同名分发究竟是哪一个实现.

    返回 ``(级别, 说明)``，级别取 ``ok`` / ``warn`` / ``absent``。

    **为什么必须单独查这一项**：本插件与参考实现 HLS 的分发名和顶层包名
    完全相同（都是 ``hermes-lark-streaming`` / ``hermes_lark_streaming``），
    pip 层面互相覆盖。而源码 marker 检查发现不了这种冲突——旧插件完全可以
    已经清掉注入痕迹（``uninstall`` 过，或被 ``hermes update`` 的 git reset
    抹掉），但包和 entry point 还留在 venv 里，Hermes 照样会去调它的
    ``register()``。判据取 entry point 指向的模块，比版本号更精确：本插件固定
    指向 ``bridge.plugin``，旧插件指向顶层包。
    """
    try:
        from importlib.metadata import PackageNotFoundError, distribution
    except Exception:  # pragma: no cover - 标准库缺失属于不可能分支
        return "absent", "无法读取包元数据"

    try:
        dist = distribution("hermes-lark-streaming")
    except PackageNotFoundError:
        return "absent", "本插件尚未 pip 安装（当前可能以 PYTHONPATH 方式运行）"
    except Exception as error:
        return "absent", f"读取包元数据失败：{error}"

    installed_version = dist.version or "?"
    target = ""
    try:
        for entry in dist.entry_points:
            if entry.group == "hermes_agent.plugins":
                target = entry.value
                break
    except Exception:
        target = ""

    if target == _PLUGIN_ENTRY_TARGET:
        if installed_version != __version__:
            return "warn", (
                f"venv 里装的是本插件但版本为 {installed_version}，当前代码是 {__version__}；"
                "重新执行 pip install -e . 使两者一致"
            )
        return "ok", f"venv 中已安装本插件 v{installed_version}"

    if target:
        return "warn", (
            f"venv 中同名分发 v{installed_version} 的插件入口指向 {target}，"
            f"不是本插件的 {_PLUGIN_ENTRY_TARGET}——这是另一个实现（很可能是旧版 HLS）。"
            "两者分发名相同，安装本插件会直接替换它；若要保留旧插件请先备份"
        )
    return "warn", (
        f"venv 中存在同名分发 v{installed_version}，但未注册 hermes_agent.plugins "
        "入口；无法确认它是哪个实现，建议先 pip uninstall hermes-lark-streaming"
    )


def _detect_legacy_injection(install_dir: Path | None) -> list[str]:
    """检测 Hermes 源码里是否残留其他插件的注入标记."""
    if install_dir is None:
        return []
    found: list[str] = []
    for relative in ("gateway/run.py", "cron/scheduler.py", "gateway/platforms/base.py"):
        path = install_dir / relative
        try:
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for marker in _LEGACY_MARKERS:
            if marker in content:
                found.append(f"{relative} 含 {marker.strip('# ')}")
                break
    return found


# ── 命令实现 ──────────────────────────────────────────────────────


def _render_runtime_weave(home: Path) -> list[str]:
    """渲染 gateway 进程内的适配器织入实况（数据来自心跳文件）.

    **为什么需要这一段**：`verify` 检查的是「前提成立吗」，`selftest` 检查的是
    「在本进程能织上吗」，两者都答不出「**正在跑的那个 gateway** 里究竟织上了
    没有」。一次真机故障就卡在这里：适配器零方法织入，而 status / selftest
    全绿，排查只能靠读源码倒推。织入结果必须是可查的事实。

    实况住在 gateway 内存里，本命令是另一个进程，只能读心跳落盘的那一份。
    """
    import time as _time

    from .selfheal import read_activity

    data = read_activity(home)
    if not isinstance(data, dict):
        return [f"  {_WARN} 无心跳记录，无法确认运行中的 gateway 是否已织入（gateway 未运行本插件？）"]

    age = max(0, int(_time.time() - int(data.get("at") or 0)))
    weave = data.get("weave")
    if not isinstance(weave, dict) or not weave:
        return [
            f"  {_WARN} 心跳里没有织入实况（{_fmt_duration(age)} 前写入，"
            "可能是旧版本插件写的心跳；重启 gateway 后再看）"
        ]

    pid = data.get("pid", "?")
    lines = [f"  数据来源: 心跳（pid {pid}，{_fmt_duration(age)} 前）"]

    init_hooked = bool(weave.get("init_hooked"))
    lines.append(
        f"  {_OK if init_hooked else _FAIL} 适配器构造函数"
        + ("已包装（新建的适配器会自动织入）" if init_hooked else "未包装")
    )

    methods = [str(item) for item in weave.get("methods") or []]
    instances = int(weave.get("instances") or 0)
    missing = [str(item) for item in weave.get("missing") or []]
    scanned = int(weave.get("scanned") or 0)

    if instances and methods:
        lines.append(f"  {_OK} 已织入 {instances} 个适配器实例: {', '.join(methods)}")
    elif scanned:
        lines.append(f"  {_FAIL} 扫到 {scanned} 个适配器实例但一个方法都没织上")
    else:
        lines.append(
            f"  {_WARN} 尚未织入任何适配器实例"
            "（若已发过消息仍是这样，说明入站织入没生效，卡片会直发会话）"
        )

    if missing:
        lines.append(f"  {_WARN} 目标方法在实例上不存在: {', '.join(missing)}")
    skipped = [str(item) for item in weave.get("skipped") or []]
    if skipped:
        lines.append(f"  · 判定为非飞书而跳过的平台: {', '.join(skipped)}")
    return lines


def cmd_status() -> int:
    from .config import Config, hermes_home

    print(f"hermes-lark-streaming v{__version__}")
    print()

    home = hermes_home()
    install_dir = _hermes_install_dir()
    hermes_py = _hermes_python()

    print("环境")
    print(f"  Hermes 主目录: {home}")
    print(f"  Hermes 安装目录: {install_dir or '未找到'}")
    print(f"  Hermes Python: {hermes_py or '未找到'}")
    print(f"  当前解释器: {Path(sys.executable).resolve()}")

    if hermes_py and Path(sys.executable).resolve() != hermes_py.resolve():
        print(f"  {_WARN} 当前不是 Hermes 的解释器。请改用：")
        print(f"     {hermes_py} -m hermes_lark_streaming status")

    print()
    print("配置")
    cfg = Config(home)
    print(f"  streaming.enabled: {cfg.enabled}")
    print(f"  飞书凭据: {'已配置' if cfg.has_credentials else '缺失'}")
    if cfg.has_credentials:
        print(f"  接入域名: {cfg.base_url}")
    print(f"  卡片 header: {'开启' if cfg.header_enabled else '关闭'}")
    print(f"  会话列表摘要: {'开启' if cfg.summary_enabled else '关闭'}")
    print(f"  工具面板: {'展示' if cfg.show_tool_use else '隐藏'}")

    print()
    print("冲突检查")
    legacy = _detect_legacy_injection(install_dir)
    if legacy:
        print(f"  {_FAIL} 检测到其他插件的源码注入残留：")
        for item in legacy:
            print(f"     - {item}")
        print("     两个插件同时接管会产生重复卡片，请先卸载旧插件。")
    else:
        print(f"  {_OK} 未发现其他插件的注入残留")

    # 源码 marker 之外还要查 pip 层：同名分发会互相覆盖，而注入痕迹可能已被
    # 清掉、包却还在，这种情况 marker 检查完全看不见
    dist_level, dist_detail = _detect_distribution_conflict()
    mark = {"ok": _OK, "warn": _WARN, "absent": _WARN}.get(dist_level, _WARN)
    print(f"  {mark} {dist_detail}")

    print()
    print("插件注册（Hermes 白名单机制）")
    registered, plugin_problems = _plugin_registration_status(home)
    if registered:
        print(f"  {_OK} 已在 plugins.enabled 中启用，Hermes 会调用本插件的 register()")
    else:
        print(f"  {_FAIL} Hermes 不会加载本插件，需要：")
        for item in plugin_problems:
            print(f"     - {item}")

    print()
    print("织入前提")
    ok, detail = _check_weave_preconditions()
    print(f"  {_OK if ok else _FAIL} {detail}")

    print()
    print("gateway 内的织入实况")
    for line in _render_runtime_weave(home):
        print(line)

    print()
    print("自愈经验")
    print(_selfheal_summary(home))

    return 0 if ok and registered and not legacy else 1


def _selfheal_summary(home: Path) -> str:
    """status 用的一行摘要.

    降级**不计入 status 的失败判定**：它是插件按预期自我保护的结果，
    不是环境错误。要看细节走 ``heal``。
    """
    try:
        healer = _build_healer(home)
    except Exception as error:
        return f"  {_WARN} 读取失败: {error}"

    if not healer.enabled:
        return f"  {_WARN} 已关闭（streaming.selfheal.enabled: false），仅保留粗粒度全局熔断"

    snapshot = healer.snapshot()
    degraded = snapshot["degraded"]
    totals = snapshot["totals"]
    if degraded:
        return (
            f"  {_FAIL} {len(degraded)} 类能力已单独降级: {'、'.join(degraded)}\n"
            f"     其余能力照常工作。详情与恢复进度: python -m hermes_lark_streaming heal"
        )
    return (
        f"  {_OK} 无降级 · 历史降级 {totals.get('degrades', 0)} 次"
        f" / 恢复 {totals.get('recoveries', 0)} 次"
        f" / 织入 {totals.get('sessions', 0)} 次"
    )


def cmd_verify() -> int:
    """只读验证织入前提，不安装任何东西.

    诊断命令必须在环境不全时也能跑完——因此每一项都独立容错，
    SDK 缺失只会让依赖它的检查标记为跳过，不会让整条命令崩掉。
    """
    print(f"hermes-lark-streaming v{__version__} — 织入前提验证")
    print()

    checks: list[tuple[str, bool, str]] = []

    # 1. 飞书 SDK —— 放最前：后续多数检查会间接导入它
    sdk_ok = False
    try:
        import lark_oapi  # noqa: F401

        sdk_ok = True
        checks.append(("飞书 SDK", True, "lark-oapi 可用"))
    except Exception as error:
        checks.append(("飞书 SDK", False, f"{error}（请在 Hermes 的 venv 中安装本插件）"))

    # 2. AIAgent 可定位
    agent_class = None
    try:
        from .bridge.weave import AgentWeaver

        agent_class = AgentWeaver.locate_agent_class()
        checks.append(("定位 AIAgent 类", True, f"{agent_class.__module__}.{agent_class.__name__}"))
    except Exception as error:
        checks.append(("定位 AIAgent 类", False, str(error)))

    # 3. 实例支持动态属性
    if agent_class is not None:
        try:
            from .bridge.weave import _has_blocking_slots

            blocked = _has_blocking_slots(agent_class)
            checks.append(
                (
                    "AIAgent 支持动态属性",
                    not blocked,
                    "无 __slots__ 阻断" if not blocked else "使用了 __slots__，无法织入",
                )
            )
        except Exception as error:
            checks.append(("AIAgent 支持动态属性", False, str(error)))

    # 4. 数据描述符机制（纯语言特性，不依赖任何外部环境）
    try:
        from .bridge.weave import _verify_descriptor_mechanics

        _verify_descriptor_mechanics()
        checks.append(("数据描述符机制", True, "工作正常"))
    except Exception as error:
        checks.append(("数据描述符机制", False, str(error)))

    # 5. 目标回调属性可织入（需要 SDK：构造 orchestrator 会拉起传输层）
    if agent_class is not None and sdk_ok:
        try:
            from .bridge.callbacks import build_factories
            from .bridge.weave import AgentWeaver as _W
            from .orchestrator import get_orchestrator

            names = list(build_factories(get_orchestrator()))
            _W.preflight(agent_class, names)
            checks.append((f"回调属性可织入（{len(names)} 个）", True, ", ".join(names)))
        except Exception as error:
            checks.append(("回调属性可织入", False, str(error)))
    elif agent_class is not None:
        checks.append(("回调属性可织入", False, "已跳过：飞书 SDK 不可用"))

    # 6. 适配器基类可定位（可降级项）
    try:
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]

        checks.append(("定位适配器基类", True, BasePlatformAdapter.__name__))
    except Exception as error:
        checks.append(("定位适配器基类", False, f"{error}（可降级：卡片将直发会话，无引用关系）"))

    # ── 类方法织入点（均为可降级项）──────────────────────────────
    # 这三项与上面的回调属性检查性质不同：回调是**实例属性**，静态查不到
    # 「Hermes 还会不会赋值」；类方法是**类属性**，此刻就能确定存在与否。
    # 所以它们必须进 verify——漏检等于把可预警的失效推迟到运行时才暴露。

    # 7. 对话主方法（精确终态的来源）
    if agent_class is not None:
        try:
            from .bridge.lifecycle import _CONVERSATION_METHODS

            found = [name for name in _CONVERSATION_METHODS if callable(getattr(agent_class, name, None))]
            checks.append(
                (
                    "对话主方法可织入",
                    bool(found),
                    ", ".join(found)
                    if found
                    else f"未找到 {'/'.join(_CONVERSATION_METHODS)}（可降级：终态由空闲守护兜底，无 model/token 明细）",
                )
            )
        except Exception as error:
            checks.append(("对话主方法可织入", False, str(error)))

    # 8. 中断方法（即时定格的来源）
    if agent_class is not None:
        try:
            from .bridge.interrupt import _INTERRUPT_METHOD

            has_interrupt = callable(getattr(agent_class, _INTERRUPT_METHOD, None))
            checks.append(
                (
                    "中断方法可织入",
                    has_interrupt,
                    f"AIAgent.{_INTERRUPT_METHOD}"
                    if has_interrupt
                    else f"未找到 {_INTERRUPT_METHOD}（可降级：中断需等空闲守护，最长 90 秒）",
                )
            )
        except Exception as error:
            checks.append(("中断方法可织入", False, str(error)))

    # 9. 子任务生命周期服务
    try:
        from .bridge.subagent import locate_service_class

        service_class = locate_service_class()
        found_methods = [name for name in ("launch", "_run") if callable(getattr(service_class, name, None))]
        checks.append(
            (
                "子任务服务可织入",
                bool(found_methods),
                f"{service_class.__name__}.{{{', '.join(found_methods)}}}"
                if found_methods
                else "服务存在但 launch/_run 均缺失（可降级：委派子任务不进卡片）",
            )
        )
    except Exception as error:
        checks.append(("子任务服务可织入", False, f"{error}（可降级：委派子任务不进卡片）"))

    failed = 0
    degradable = 0
    for name, ok, detail in checks:
        mark = _OK if ok else _FAIL
        print(f"  {mark} {name}: {detail}")
        if not ok:
            failed += 1
            if "可降级" in detail:
                degradable += 1

    print()
    if failed == 0:
        print("全部通过。插件会在 gateway 启动时自动织入，无需安装步骤。")
        return 0
    if failed == degradable:
        print(f"{failed} 项未通过，且全部为可降级项：插件仍会启用，仅对应能力缺失。")
        return 0
    print(f"{failed} 项未通过（其中 {degradable} 项可降级）。非降级项会导致插件完全不启用。")
    return 1


def cmd_selftest() -> int:
    """在当前进程实际执行一次织入，验证端到端可行性后立即回滚."""
    print(f"hermes-lark-streaming v{__version__} — 织入演练")
    print()

    from .bridge.plugin import bootstrap, teardown

    report = bootstrap(force=True)
    print(report.render())
    print()

    teardown()
    print("演练结束，已回滚：回调描述符、对话主方法、适配器、中断、子任务五类织入全部还原，")
    print("Hermes 的类属性逐字复原，幂等标记一并清除，当前进程不保留任何织入。")
    return 0 if report.ok else 1


def cmd_doctor() -> int:
    """综合诊断并给出可执行建议."""
    print(f"hermes-lark-streaming v{__version__} — 诊断")
    print("=" * 60)
    print()

    status_code = cmd_status()
    print()
    print("=" * 60)
    print()
    verify_code = cmd_verify()

    print()
    print("=" * 60)
    print()
    print("自愈经验")
    from .config import Config, hermes_home

    try:
        print(_build_healer(hermes_home()).render())
    except Exception as error:
        print(f"  {_WARN} 读取失败: {error}")

    print()
    print("=" * 60)
    print()
    print("建议")

    suggestions: list[str] = []

    cfg = Config(hermes_home())
    if not cfg.enabled:
        suggestions.append("在 ~/.hermes/config.yaml 中设置 streaming.enabled: true")
    if not cfg.has_credentials:
        suggestions.append(
            "配置飞书凭据：在 ~/.hermes/.env 写入 FEISHU_APP_ID / FEISHU_APP_SECRET，"
            "或在 config.yaml 的 feishu 段配置"
        )

    legacy = _detect_legacy_injection(_hermes_install_dir())
    if legacy:
        suggestions.append("先卸载会注入源码的旧插件，否则会出现重复卡片")

    dist_level, dist_detail = _detect_distribution_conflict()
    if dist_level == "warn":
        suggestions.append(dist_detail)

    try:
        healer = _build_healer(hermes_home())
        degraded = healer.snapshot()["degraded"] if healer.enabled else []
    except Exception:
        degraded = []
    if degraded:
        suggestions.append(
            f"以下能力已被自动降级：{'、'.join(degraded)}。插件会周期性试探恢复，"
            "其余能力不受影响；若你已修好根因，执行 `heal reset` 可立即清空经验重学"
        )

    _registered, plugin_problems = _plugin_registration_status(hermes_home())
    suggestions.extend(plugin_problems)

    # 升级前占用检查：有任务在跑时先提醒，避免升级打断正在进行的工作
    import time as _time

    from .selfheal import read_activity

    _activity = read_activity(hermes_home())
    _age = max(0, int(_time.time() - int(_activity.get("at") or 0))) if isinstance(_activity, dict) else 0
    _idle_ok, _idle_detail = _activity_verdict(_activity, _age)
    if not _idle_ok:
        suggestions.append(f"{_idle_detail}（详情：python -m hermes_lark_streaming activity）")

    hermes_py = _hermes_python()
    if hermes_py and Path(sys.executable).resolve() != hermes_py.resolve():
        suggestions.append(f"用 Hermes 自己的解释器安装本插件：{hermes_py} -m pip install -e .")

    from .events.normalize import hermes_constants_available, lifecycle_constants_borrowed

    if not hermes_constants_available():
        suggestions.append(
            "未能读取 Hermes 的压缩状态常量，状态消息分类已降级为内置规则（功能可用，"
            "识别精度略降）"
        )

    if not lifecycle_constants_borrowed():
        suggestions.append(
            "未能读取 Hermes 的 gateway 关闭 / 重启常量，改用内置短语表（功能可用，"
            "但 Hermes 改这两条文案时不会自动跟随）"
        )

    if not suggestions:
        print("  未发现需要处理的问题。")
    else:
        for index, item in enumerate(suggestions, start=1):
            print(f"  {index}. {item}")

    print()
    print("关于 Hermes 升级")
    print("  本插件不修改 Hermes 源码，`hermes update` 后无需重装、无需重新注入。")
    print("  若 Hermes 改动了内部装配方式导致织入失效，gateway 启动日志会明确报错，")
    print("  且插件会完全退出，Hermes 的消息收发不受影响。")
    print("  升级后织入失败时，日志与 status 会带上「历史对照」——指出相比上次成功")
    print("  织入具体少了哪个回调，而不是只报一句笼统失败。")
    print("  注：v1.0 未实现源码注入降级路径，织入不可用时插件直接退出。")

    return 0 if status_code == 0 and verify_code == 0 else 1


def _check_weave_preconditions() -> tuple[bool, str]:
    try:
        from .bridge.callbacks import build_factories
        from .bridge.weave import AgentWeaver
        from .orchestrator import get_orchestrator

        agent_class = AgentWeaver.locate_agent_class()
        AgentWeaver.preflight(agent_class, list(build_factories(get_orchestrator())))
        return True, "运行时织入可用（不修改 Hermes 任何文件）"
    except Exception as error:
        return False, f"运行时织入不可用：{error}"


if __name__ == "__main__":
    raise SystemExit(main())

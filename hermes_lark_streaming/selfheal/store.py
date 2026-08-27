"""自愈状态的持久化 — 跨进程记忆的唯一落点.

四条约束：

1. **绝不写 Hermes 的 config.yaml**。学习结果只落插件自己的状态文件；
   用户的显式配置永远优先，插件不会偷改用户设定
2. **写失败必须静默**。磁盘满、权限不足、文件损坏都不能影响卡片链路
3. **原子写**。先写同目录临时文件再 ``os.replace``，避免读到半截 JSON
4. **类型不可信**。状态文件可能被手改或跨版本残留，读入后逐字段校验，
   任何不合法的值一律退回默认，绝不让脏数据驱动降级决策
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ..observability import logger

#: 状态文件 schema 版本。不兼容变更时递增，旧版本整体作废重学
SCHEMA_VERSION = 1

#: 状态目录名。**必须不是合法 Python 标识符**（这里用连字符，与 pip 分发名一致）。
#:
#: 曾用 ``hermes_lark_streaming``（与包名相同），结果插件在真实 gateway 里永远
#: 加载不上：gateway 的 cwd 是 ``~/.hermes``，而 ``python -m`` 会把 cwd 放进
#: ``sys.path[0]``，于是这个只装着两个 JSON 的目录被当成**命名空间包**，抢在
#: editable finder 之前遮蔽了真正的包，Hermes 报
#: ``cannot import name '__version__' from 'hermes_lark_streaming' (unknown location)``。
#:
#: 连字符不能出现在 Python 标识符里，因此改名后彻底免疫这类遮蔽。
#: 由 ``tests/test_smoke.py`` 的静态检查守住这条不变式。
_STATE_DIRNAME = "hermes-lark-streaming"

#: 旧状态目录名。仅用于一次性迁移，迁完不再引用
_LEGACY_STATE_DIRNAME = "hermes_lark_streaming"

_STATE_FILENAME = "state.json"
#: 运行活动心跳。与经验文件同目录但语义不同：经验是「学到的」，心跳是
#: 「此刻的」，且心跳的读者是**另一个进程**（CLI）——gateway 内存里的活跃
#: turn，独立的 CLI 进程无从得知，只能靠落盘传递
_ACTIVITY_FILENAME = "activity.json"

# flock 仅 POSIX 可用。缺失时降级为无锁——原子 replace 本身已保证
# 不会读到半截文件，锁只是进一步降低并发写互相覆盖的概率
try:  # pragma: no cover - 取决于平台
    import fcntl

    _FLOCK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FLOCK_AVAILABLE = False


def state_dir(home: Path) -> Path:
    """状态目录：``<hermes_home>/hermes-lark-streaming/``.

    刻意放在 Hermes 主目录下而非包安装目录：``pip install -e .`` 的包目录
    可能只读，且 Hermes 升级不会碰主目录，经验因此能跨升级留存。

    目录名用连字符而非下划线的原因见 :data:`_STATE_DIRNAME`——那不是风格
    偏好，是一个曾让插件完全无法加载的缺陷的修复。
    """
    return Path(home) / _STATE_DIRNAME


def migrate_legacy_dir(home: Path) -> bool:
    """把旧的 ``hermes_lark_streaming/`` 状态目录迁到新名字下.

    返回是否实际做了迁移。全程静默容错：迁移失败最坏结果是经验从零重学，
    不值得因此影响插件加载。已存在新目录时只搬缺失的文件，不覆盖新数据。
    """
    legacy = Path(home) / _LEGACY_STATE_DIRNAME
    if not legacy.is_dir():
        return False

    target = state_dir(home)
    moved = False
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in (_STATE_FILENAME, _ACTIVITY_FILENAME):
            src = legacy / name
            dst = target / name
            if src.is_file() and not dst.exists():
                src.replace(dst)
                moved = True
        # 只在确实空了才删，避免误删用户放进去的东西
        if not any(legacy.iterdir()):
            legacy.rmdir()
        if moved:
            logger.info("状态目录已迁移: %s → %s", legacy.name, target.name)
    except Exception:
        logger.debug("状态目录迁移失败，按空白经验继续", exc_info=True)
    return moved


def state_path(home: Path) -> Path:
    return state_dir(home) / _STATE_FILENAME


def activity_path(home: Path) -> Path:
    return state_dir(home) / _ACTIVITY_FILENAME


def write_activity(home: Path, payload: dict[str, Any]) -> bool:
    """写运行活动心跳，返回是否成功.

    与 :func:`save` 共用原子写：心跳被另一个进程读取，读到半截 JSON 会让
    ``activity`` 命令给出错误结论（比如把有任务在跑说成空闲）。
    """
    directory = state_dir(home)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        _atomic_write(directory, activity_path(home), text)
        return True
    except (OSError, TypeError, ValueError):
        logger.debug("活动心跳写入失败", exc_info=True)
        return False


def read_activity(home: Path) -> dict[str, Any] | None:
    """读运行活动心跳；文件缺失或损坏返回 ``None``.

    调用方必须自己判断 ``at`` 字段的新鲜度——文件存在不代表 gateway 还活着，
    进程被 kill 时不会留下任何「我停了」的记录。
    """
    path = activity_path(home)
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("活动心跳读取失败: %s", path, exc_info=True)
        return None
    return raw if isinstance(raw, dict) else None


def default_state(plugin_version: str) -> dict[str, Any]:
    """空白经验。首次运行、schema 升级、文件损坏三种情况都回到这里."""
    return {
        "schema": SCHEMA_VERSION,
        "plugin_version": plugin_version,
        "updated_at": 0,
        # 上次织入的能力指纹。digest 变化即视为环境已变，全部经验作废
        "fingerprint": {
            "digest": "",
            "ok": False,
            "at": 0,
            "hermes_version": "",
            "callbacks": [],
            "adapter": [],
            "conversation": [],
            # 历史上真实被 Hermes 装配过的回调（描述符被触发过）。
            # 这是判断「Hermes 是否还在用某个回调」的唯一可靠信号——
            # 静态检查看不到实例属性，描述符被触发才证明装配路径存在
            "observed": [],
            "detail": "",
        },
        # 每个能力维度的失败经验，键为 CAPABILITIES 中的维度名
        "capabilities": {},
        "totals": {"degrades": 0, "probes": 0, "recoveries": 0, "sessions": 0},
    }


def _as_int(value: Any, default: int = 0, *, minimum: int = 0) -> int:
    """转整数；布尔不视为整数，负值夹到 minimum."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(minimum, int(value))


def _as_str_list(value: Any, limit: int = 32) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)][:limit]


def _normalize(raw: Any, plugin_version: str) -> dict[str, Any]:
    """把磁盘上的任意内容收敛为合法状态.

    状态文件可能被手工编辑、被旧版本写过、或写入中途断电。任何不合法的
    字段都退回默认值——脏数据驱动降级决策比没有经验危险得多。
    """
    state = default_state(plugin_version)
    if not isinstance(raw, dict):
        return state
    if _as_int(raw.get("schema"), -1) != SCHEMA_VERSION:
        # schema 不匹配：整体作废重学，不做迁移。经验是可再生资源，
        # 维护迁移逻辑的复杂度远高于重新学一遍的成本
        return state

    state["plugin_version"] = raw["plugin_version"] if isinstance(raw.get("plugin_version"), str) else ""
    state["updated_at"] = _as_int(raw.get("updated_at"))

    source_fp = raw.get("fingerprint")
    if isinstance(source_fp, dict):
        target_fp = state["fingerprint"]
        target_fp["digest"] = source_fp["digest"] if isinstance(source_fp.get("digest"), str) else ""
        target_fp["ok"] = source_fp.get("ok") is True
        target_fp["at"] = _as_int(source_fp.get("at"))
        for key in ("hermes_version", "detail"):
            value = source_fp.get(key)
            target_fp[key] = value[:200] if isinstance(value, str) else ""
        for key in ("callbacks", "adapter", "conversation", "observed"):
            target_fp[key] = _as_str_list(source_fp.get(key))

    source_caps = raw.get("capabilities")
    if isinstance(source_caps, dict):
        for name, entry in source_caps.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            last_error = entry.get("last_error")
            state["capabilities"][name] = {
                "streak": _as_int(entry.get("streak")),
                "total": _as_int(entry.get("total")),
                "degraded_at": _as_int(entry.get("degraded_at")),
                "digest": entry["digest"] if isinstance(entry.get("digest"), str) else "",
                "last_error": last_error[:300] if isinstance(last_error, str) else "",
                "probe_in": _as_int(entry.get("probe_in")),
            }

    source_totals = raw.get("totals")
    if isinstance(source_totals, dict):
        for key in state["totals"]:
            state["totals"][key] = _as_int(source_totals.get(key))
    return state


def load(home: Path, plugin_version: str) -> dict[str, Any]:
    """读取经验。文件不存在、损坏、schema 不符都返回空白经验."""
    migrate_legacy_dir(home)
    path = state_path(home)
    try:
        if not path.exists():
            return default_state(plugin_version)
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("自愈状态读取失败，按空白经验处理: %s", path, exc_info=True)
        return default_state(plugin_version)
    return _normalize(raw, plugin_version)


def save(home: Path, state: dict[str, Any]) -> bool:
    """原子写入经验，返回是否成功.

    失败只记 debug 日志：自愈层是旁路的，写不进去最多是下次重新学一遍。
    """
    directory = state_dir(home)
    path = state_path(home)
    payload = dict(state)
    payload["schema"] = SCHEMA_VERSION
    payload["updated_at"] = int(time.time())
    try:
        directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        _atomic_write(directory, path, text)
        return True
    except (OSError, TypeError, ValueError):
        logger.debug("自愈状态写入失败: %s", path, exc_info=True)
        return False


def _atomic_write(directory: Path, path: Path, text: str) -> None:
    """同目录临时文件 + ``os.replace``，配合 flock 降低并发覆盖概率.

    临时文件必须与目标同目录：跨文件系统时 ``os.replace`` 不是原子操作。
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, prefix=".state-", suffix=".tmp", delete=False
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            if _FLOCK_AVAILABLE:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass  # 锁不可用不阻断写入，原子 replace 仍然保证完整性
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

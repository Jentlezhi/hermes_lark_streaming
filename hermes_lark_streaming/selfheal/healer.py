"""自愈决策 — 精准降级、经验继承、指纹作废、试探恢复.

与 :class:`..transport.resilience.CircuitBreaker` 的分工：

- **熔断器**管「核心流式坏了怎么办」——答案、推理、工具面板失败意味着插件
  失去全部价值，直接全局退回原生是正确的
- **本模块**管「某一类收纳坏了怎么办」——notice / review / clarify / approval
  任一类失败只关掉那一类，其余照常。取代原先「一坏全坏」的粗粒度行为

降级决策的完整优先级（用户永远赢）::

    用户显式配置 capture.<kind>  >  学到的降级经验  >  内置默认（全开）

也就是说：用户写死 ``capture.notice: true`` 时，即使这一类连续失败一百次，
插件也会继续尝试——**插件不会偷偷改掉用户的设定**，只会在报告里说明。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from ..observability import METRICS, logger, redact
from . import store

#: 可独立降级的能力维度。与配置里的 ``streaming.capture.*`` 严格对齐，
#: 这样「学到的降级」与「用户手动关闭」表达的是同一件事，语义不分裂。
CAPABILITIES: tuple[str, ...] = ("notice", "review", "clarify", "approval", "subagent")

#: 中文名，用于报告
_LABELS = {
    "notice": "状态提示收纳",
    "review": "自我改进收纳",
    "clarify": "澄清收纳",
    "approval": "命令授权收纳",
    "subagent": "子任务收纳",
}


def _digest_of(callbacks: list[str], conversation: list[str]) -> str:
    """能力集合指纹.

    **刻意不用版本号作为指纹**：Hermes 的语义版本（``hermes_cli.__version__``）
    变化远慢于实际代码——同一个 0.20.5 可能对应几十个 commit。而织入成功的
    回调集合本身就是「这套环境长什么样」的直接度量，回调集合没变就说明
    织入面没变，经验依然适用。版本号只作为报告里给人看的附加信息。

    **刻意不含适配器方法**：适配器是 Hermes 创建平台实例时才织入的，启动时
    尚未确定。把它计入指纹会让指纹随时机波动，导致经验反复作废。
    """
    payload = json.dumps(
        {"c": sorted(callbacks), "v": sorted(conversation)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class SelfHealer:
    """经验的持有者与降级决策者.

    线程安全：orchestrator 会从 worker 线程与事件循环两侧调用，全部方法
    在 RLock 保护下操作内存态。落盘只在状态发生**语义变化**时触发
    （降级、恢复、指纹变更），单纯的失败计数递增不写盘，避免高频 IO。
    """

    __slots__ = (
        "_degrade_threshold",
        "_digest",
        "_enabled",
        "_home",
        "_lock",
        "_probe_interval",
        "_session_observed",
        "_state",
    )

    def __init__(
        self,
        home: Path,
        plugin_version: str,
        *,
        enabled: bool = True,
        degrade_threshold: int = 3,
        probe_interval: int = 20,
    ) -> None:
        self._home = Path(home)
        self._enabled = bool(enabled)
        self._degrade_threshold = max(1, int(degrade_threshold))
        self._probe_interval = max(1, int(probe_interval))
        self._lock = threading.RLock()
        self._state = store.load(self._home, plugin_version) if enabled else store.default_state(plugin_version)
        # 本进程实际观测到的装配（内存态）。与落盘的历史累积对比，才能看出
        # 「上次有、这次没有」——单看历史累积永远不会显示缺失
        self._session_observed: set[str] = set()
        # 本进程尚未织入时，沿用上次落盘的指纹：这样 CLI（doctor / status）
        # 不必先织入也能读出经验，判断「上次为什么降级」
        self._digest = str(self._state["fingerprint"].get("digest") or "")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def digest(self) -> str:
        return self._digest

    # ── 织入指纹 ──────────────────────────────────────────────────

    def record_weave(
        self,
        *,
        ok: bool,
        hermes_version: str = "",
        callbacks: list[str] | None = None,
        adapter: list[str] | None = None,
        conversation: list[str] | None = None,
        detail: str = "",
    ) -> None:
        """记录一次织入结果，并在能力集合变化时作废全部降级经验.

        指纹变化意味着 Hermes 或本插件的织入面已经不同，上次「notice 必失败」
        的结论在新环境里没有依据——继续沿用会让插件在本可工作的环境里
        白白关闭能力。因此一律清空重学。
        """
        if not self._enabled:
            return
        with self._lock:
            names = list(callbacks or [])
            digest = _digest_of(names, list(conversation or [])) if ok else ""
            changed = bool(digest) and digest != self._digest and bool(self._digest)
            if changed:
                dropped = len(self._state["capabilities"])
                self._state["capabilities"] = {}
                if dropped:
                    logger.info(
                        "织入能力集合已变化（指纹 %s → %s），%d 项降级经验作废，重新学习",
                        self._digest or "-",
                        digest,
                        dropped,
                    )
                    METRICS.incr("selfheal.experience_invalidated")
            if digest:
                self._digest = digest
            if ok:
                # observed 必须跨织入保留：它是「上次运行实际装配了什么」的
                # 基线，正是升级后判断某回调是否消失的唯一依据
                previous_observed = list(self._state["fingerprint"].get("observed") or [])
                self._state["fingerprint"] = {
                    "digest": digest,
                    "ok": True,
                    "at": int(time.time()),
                    "hermes_version": hermes_version[:200],
                    "callbacks": names,
                    "adapter": list(adapter or []),
                    "conversation": list(conversation or []),
                    "observed": previous_observed,
                    "detail": "",
                }
                self._state["totals"]["sessions"] = int(self._state["totals"].get("sessions", 0)) + 1
            else:
                # 失败不覆盖上次成功的指纹——那份记录正是用来做对比的基线
                self._state["fingerprint"]["detail"] = redact(detail)[:200]
            self._flush()

    def regression(self, current: list[str]) -> tuple[list[str], list[str]]:
        """相比上次成功织入，本次**缺失**与**新增**的回调.

        这是升级后诊断的核心：把「织入失败」这个笼统结论，变成
        「相比 v0.20.5 少了 reasoning_callback，其余 7 个正常」。
        无历史基线时返回两个空列表，调用方据此退回泛化提示。
        """
        with self._lock:
            baseline = set(self._state["fingerprint"].get("callbacks") or [])
        if not baseline:
            return [], []
        present = set(current)
        return sorted(baseline - present), sorted(present - baseline)

    def record_observed(self, name: str) -> None:
        """记录某个回调**确实被 Hermes 装配过**（描述符被触发）.

        这是判断「Hermes 是否还在用这个回调」的唯一可靠信号：回调是实例属性，
        静态检查看不到；而描述符被触发证明装配路径真实存在。

        写盘只在集合首次扩大时发生——回调装配每个 turn 只有个位数次，
        且集合单调增长，实际落盘次数在插件生命周期内不超过回调总数。
        """
        if not self._enabled or not name:
            return
        with self._lock:
            self._session_observed.add(name)
            observed = self._state["fingerprint"].setdefault("observed", [])
            if not isinstance(observed, list) or name in observed:
                return
            observed.append(name)
            observed.sort()
            self._flush()

    def observation_gap(self) -> tuple[list[str], int, int]:
        """历史装配过、但本进程尚未观测到的回调.

        返回 ``(疑似缺失, 本进程观测数, 历史观测数)``。

        **刻意不自动判定为「失效」**：部分回调是条件性装配的（例如
        ``reasoning_callback`` 只在开启推理展示时才赋值），本进程没观测到
        完全可能只是这一轮没触发条件。因此这里只呈现差异，由人判断——
        自动结论在这里必然误报，而误报比不报更糟。
        """
        with self._lock:
            history = [item for item in (self._state["fingerprint"].get("observed") or []) if isinstance(item, str)]
            session = set(self._session_observed)
        return sorted(set(history) - session), len(session), len(history)

    def last_success(self) -> dict[str, Any] | None:
        """上次成功织入的记录副本；从未成功过则为 None."""
        with self._lock:
            fingerprint = self._state["fingerprint"]
            return dict(fingerprint) if fingerprint.get("ok") else None

    # ── 精准降级 ──────────────────────────────────────────────────

    @staticmethod
    def _new_entry() -> dict[str, Any]:
        return {"streak": 0, "total": 0, "degraded_at": 0, "digest": "", "last_error": "", "probe_in": 0}

    def is_degraded(self, capability: str) -> bool:
        """该能力当前是否处于降级状态（应跳过收纳、直接透传原生）.

        内含试探逻辑：降级期间每被拦截 ``probe_interval`` 次就放行一次，
        用「被拦截次数」而非墙上时间作为尺度——消息密集时试探得快，
        空闲时不做无谓探测，天然自适应。
        """
        if not self._enabled:
            return False
        with self._lock:
            entry = self._state["capabilities"].get(capability)
            if not entry or not entry["degraded_at"]:
                return False
            if entry["digest"] != self._digest:
                # 指纹已变（Hermes 或插件升级），旧结论失去依据，放行
                return False
            remaining = int(entry["probe_in"])
            if remaining <= 0:
                entry["probe_in"] = self._probe_interval
                self._state["totals"]["probes"] = int(self._state["totals"].get("probes", 0)) + 1
                METRICS.incr("selfheal.probe")
                logger.info("能力「%s」处于降级态，本次试探性放行一次", _LABELS.get(capability, capability))
                return False
            # 递减不落盘：这类高频计数不值得每次写磁盘，进程重启后
            # 最多让下一次试探晚一点，代价可忽略
            entry["probe_in"] = remaining - 1
            return True

    def record_failure(self, capability: str, error: BaseException | str = "") -> bool:
        """记录一次收纳失败，返回本次是否**新触发**降级."""
        if not self._enabled:
            return False
        text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        label = _LABELS.get(capability, capability)
        with self._lock:
            entry = self._state["capabilities"].setdefault(capability, self._new_entry())
            entry["streak"] = int(entry["streak"]) + 1
            entry["total"] = int(entry["total"]) + 1
            entry["last_error"] = redact(text)[:300] if text else entry["last_error"]
            entry["digest"] = self._digest
            if entry["degraded_at"]:
                # 试探失败：重置试探窗口继续降级，不重复计入新降级
                entry["probe_in"] = self._probe_interval
                self._flush()
                return False
            if int(entry["streak"]) < self._degrade_threshold:
                return False  # 未达阈值，只改内存，等它自己恢复
            entry["degraded_at"] = int(time.time())
            entry["probe_in"] = self._probe_interval
            self._state["totals"]["degrades"] = int(self._state["totals"].get("degrades", 0)) + 1
            streak = int(entry["streak"])
            self._flush()
        METRICS.incr("selfheal.degraded")
        logger.warning(
            "「%s」连续失败 %d 次，已单独降级为原生透传；其余能力不受影响。"
            "经验已落盘，下次启动直接生效，每 %d 次拦截后会自动试探恢复",
            label,
            streak,
            self._probe_interval,
        )
        return True

    def record_success(self, capability: str) -> None:
        """记录一次收纳成功；若处于降级态则视为试探成功并恢复."""
        if not self._enabled:
            return
        recovered = False
        with self._lock:
            entry = self._state["capabilities"].get(capability)
            if entry is None:
                return
            if entry["degraded_at"]:
                del self._state["capabilities"][capability]
                self._state["totals"]["recoveries"] = int(self._state["totals"].get("recoveries", 0)) + 1
                recovered = True
                self._flush()
            elif int(entry["streak"]):
                entry["streak"] = 0  # 只清连击，保留 total 供报告呈现历史
        if recovered:
            METRICS.incr("selfheal.recovered")
            logger.info("「%s」试探成功，已恢复收纳，降级经验一并清除", _LABELS.get(capability, capability))

    def degraded_capabilities(self) -> list[str]:
        with self._lock:
            return sorted(
                name
                for name, entry in self._state["capabilities"].items()
                if entry["degraded_at"] and entry["digest"] == self._digest
            )

    # ── 持久化与呈现 ──────────────────────────────────────────────

    def _flush(self) -> None:
        """写盘。调用方必须已持有 ``self._lock``."""
        store.save(self._home, self._state)

    def snapshot(self) -> dict[str, Any]:
        """供 ``stats()`` 与测试读取的状态快照."""
        gap, session_count, history_count = self.observation_gap()
        with self._lock:
            return {
                "enabled": self._enabled,
                "digest": self._digest,
                "degraded": self.degraded_capabilities(),
                "capabilities": {name: dict(entry) for name, entry in self._state["capabilities"].items()},
                "totals": dict(self._state["totals"]),
                "observed_session": session_count,
                "observed_history": history_count,
                "observation_gap": gap,
                "state_file": str(store.state_path(self._home)),
            }

    def reset(self) -> None:
        """清空全部经验（供 ``selftest`` 与人工干预使用）."""
        with self._lock:
            plugin_version = str(self._state.get("plugin_version") or "")
            self._state = store.default_state(plugin_version)
            self._session_observed.clear()
            self._digest = ""
            self._flush()

    def render(self) -> str:
        """人类可读报告，供 ``doctor`` / ``status`` 输出."""
        with self._lock:
            fingerprint = dict(self._state["fingerprint"])
            capabilities = {name: dict(entry) for name, entry in self._state["capabilities"].items()}
            totals = dict(self._state["totals"])
            digest = self._digest

        if not self._enabled:
            return "  自愈层已关闭（streaming.selfheal.enabled: false）"

        lines = [f"  状态文件: {store.state_path(self._home)}"]
        if fingerprint.get("ok"):
            lines.append(
                f"  织入指纹: {fingerprint['digest'] or '-'}"
                f" · Hermes {fingerprint.get('hermes_version') or '未知'}"
                f" · {len(fingerprint.get('callbacks') or [])} 回调"
                f" / {len(fingerprint.get('conversation') or [])} 终态方法"
            )
            at = int(fingerprint.get("at") or 0)
            if at:
                lines.append(f"  上次成功: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(at))}")
        else:
            lines.append("  织入指纹: 尚无成功记录")
        if fingerprint.get("detail"):
            # 织入失败不覆盖成功基线，但失败原因必须可见——否则升级后
            # 报告会显示「上次成功」却不提本次已经失败，产生误导
            lines.append(f"  最近失败: {fingerprint['detail']}")

        gap, session_count, history_count = self.observation_gap()
        if history_count:
            lines.append(f"  装配观测: 本进程 {session_count} 个 · 历史累计 {history_count} 个")
            if gap:
                lines.append(f"    本进程未观测到: {', '.join(gap)}")
                lines.append("    （条件性装配的回调可能只是本轮未触发条件，不必然是失效）")

        lines.append("  能力状态:")
        for name in CAPABILITIES:
            label = _LABELS[name]
            entry = capabilities.get(name)
            if entry is None:
                lines.append(f"    ✔ {label} — 正常")
                continue
            if entry["degraded_at"] and entry["digest"] == digest:
                when = time.strftime("%m-%d %H:%M", time.localtime(int(entry["degraded_at"])))
                lines.append(
                    f"    ✘ {label} — 已降级（{when} 起，连续 {entry['streak']} 次失败，累计 {entry['total']} 次）"
                )
                if entry["last_error"]:
                    lines.append(f"       最后错误: {entry['last_error']}")
                lines.append(f"       将在 {entry['probe_in']} 次拦截后自动试探恢复")
            elif entry["degraded_at"]:
                lines.append(f"    ✔ {label} — 正常（历史降级经验已因环境变化作废）")
            else:
                lines.append(f"    ✔ {label} — 正常（近期失败 {entry['total']} 次，未达降级阈值）")

        lines.append(
            f"  累计: 降级 {totals.get('degrades', 0)} 次"
            f" · 试探 {totals.get('probes', 0)} 次"
            f" · 恢复 {totals.get('recoveries', 0)} 次"
            f" · 织入 {totals.get('sessions', 0)} 次"
        )
        return "\n".join(lines)


# ── 单例（按 profile home 隔离）─────────────────────────────────────

_HEALERS: dict[str, SelfHealer] = {}
_HEALER_LOCK = threading.Lock()


def get_healer(
    home: Path,
    plugin_version: str,
    *,
    enabled: bool = True,
    degrade_threshold: int = 3,
    probe_interval: int = 20,
) -> SelfHealer:
    """按 Hermes 主目录取 healer 单例.

    多 profile 各自独立：经验绑定于具体环境，跨 profile 共享会把 A 环境的
    失效结论错误地应用到 B 环境，而两者的 Hermes 版本与凭据都可能不同。
    """
    key = str(Path(home).resolve())
    with _HEALER_LOCK:
        healer = _HEALERS.get(key)
        if healer is None:
            healer = SelfHealer(
                Path(home),
                plugin_version,
                enabled=enabled,
                degrade_threshold=degrade_threshold,
                probe_interval=probe_interval,
            )
            _HEALERS[key] = healer
        return healer


def reset_healers() -> None:
    """清空单例缓存。仅供测试与 ``selftest`` 使用."""
    with _HEALER_LOCK:
        _HEALERS.clear()

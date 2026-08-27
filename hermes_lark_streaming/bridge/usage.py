"""订阅额度 — 进程内直接查询，不起子进程.

HFC 用 ``subprocess.run([python, "-c", script])`` 调 Hermes 的
``agent.account_usage.fetch_account_usage``——因为它是 sidecar 独立进程，
不在 Hermes 的 venv 上下文里。我们在 gateway 进程内，直接 import 即可：
省掉进程启动开销与超时窗口，也少一个失效点。

两处比 HFC 完整：

* **provider 从配置读**而非硬编码 ``openai-codex``。Hermes 为 codex /
  anthropic / openrouter 三家实现了额度接口，硬编码会让另两家永远无数据
* **进程内缓存**：额度是慢变量，每个 turn 终态都打一次外部 API 没有意义

**查不到数据是正常情况**：Hermes 只为上述三家实现了接口，其余服务商
（deepseek、本地模型等）一律返回空，此时 footer 不展示该字段而非显示占位。
"""

from __future__ import annotations

import threading
import time

from ..config import Config
from ..observability import logger

#: Hermes 的窗口标签 -> 展示名。与 HFC 的 ``_WINDOW_LABELS`` 同源：
#: 不同服务商对同一个窗口用不同叫法，归一化后卡片上才是一致的
_WINDOW_LABELS = {
    "session": "5h",
    "primary": "5h",
    "weekly": "周",
    "week": "周",
    "secondary": "周",
}

#: 支持额度查询的服务商。不在此列的直接跳过，不做无谓的网络请求
_SUPPORTED = frozenset({"openai-codex", "anthropic", "openrouter"})

_cache_lock = threading.Lock()
_cache_value = ""
_cache_at = 0.0


def _render(snapshot: object) -> str:
    """把 ``AccountUsageSnapshot`` 压成一行.

    只取各窗口的已用百分比：footer 空间有限，reset_at 与 details 放进去会挤掉
    模型名和耗时这些每轮都需要的信息。
    """
    if not getattr(snapshot, "available", False):
        return ""
    parts: list[str] = []
    for window in getattr(snapshot, "windows", ()) or ():
        percent = getattr(window, "used_percent", None)
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            continue
        raw_label = str(getattr(window, "label", "") or "").strip().lower()
        label = _WINDOW_LABELS.get(raw_label, raw_label or "额度")
        parts.append(f"{label} {round(float(percent))}%")
    return " · ".join(parts[:3])


def fetch_usage_line(cfg: Config) -> str:
    """查询订阅额度并渲染成一行；不可用时返回空字符串.

    **绝不抛异常**：额度是装饰性信息，它的失败不能影响收卡。
    """
    global _cache_value, _cache_at

    if not cfg.usage_enabled:
        return ""

    provider = cfg.hermes_provider.strip().lower()
    if provider not in _SUPPORTED:
        return ""

    ttl = cfg.usage_ttl_sec
    now = time.monotonic()
    with _cache_lock:
        if _cache_at and now - _cache_at < ttl:
            return _cache_value

    line = ""
    try:
        from agent.account_usage import fetch_account_usage  # type: ignore[import-not-found]

        line = _render(fetch_account_usage(provider))
    except Exception:
        logger.debug("订阅额度查询失败（provider=%s）", provider, exc_info=True)

    with _cache_lock:
        # 失败也写缓存：否则每轮都会重试一个已知不通的接口，把失败成本
        # 叠加到每一次收卡上
        _cache_value = line
        _cache_at = time.monotonic()
    return line


def reset_cache() -> None:
    """清空缓存，仅供测试与人工干预使用."""
    global _cache_value, _cache_at

    with _cache_lock:
        _cache_value = ""
        _cache_at = 0.0

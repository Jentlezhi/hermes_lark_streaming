"""可观测性 — 日志、指标与脱敏.

三条原则：

1. **脱敏在写日志之前完成**，不依赖下游过滤
2. **指标是进程内内存计数**，不落盘、不外发，只供 ``status`` 命令与排障读取
3. **日志绝不抛异常**，任何格式化失败都静默吞掉——可观测性不能成为故障源
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from typing import Any

from . import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)


# ── 脱敏 ──────────────────────────────────────────────────────────

# 敏感字段名。命中这些名字的赋值、命令行参数一律打码
_SENSITIVE_NAME_RE = re.compile(
    r"token|secret|password|api[_-]?key|authorization|cookie|credential"
    r"|bearer|session[_-]?id|client[_-]?secret|access[_-]?key",
    re.IGNORECASE,
)
# key="value" / key='value' / key=value
_INLINE_ASSIGNMENT_RE = re.compile(r'(^|[\s"\'`])([A-Za-z_][A-Za-z0-9_]*)(=(?:"[^"]*"|\'[^\']*\'|[^\s"\'`]+))')
# Authorization: Bearer xxx
_AUTH_HEADER_RE = re.compile(r"(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)([^'\"\s]+)", re.IGNORECASE)
# --token xxx / --api-key=xxx
_SECRET_FLAG_RE = re.compile(
    r'((?:^|[\s"\'`])(--?[A-Za-z0-9][A-Za-z0-9-]*)(=|\s+)("(?:[^"]*)"|\'(?:[^\']*)\'|[^\s"\'`]+))'
)
# 飞书租户 token / app secret 的常见形态
_FEISHU_TOKEN_RE = re.compile(r"\b([ut]-[A-Za-z0-9_.-]{10,})")


def redact(value: str) -> str:
    """对任意文本做尽力脱敏.

    覆盖三种泄露形态：``key=secret`` 赋值、``Authorization`` 头、``--flag secret``
    命令行参数，外加飞书 token 字面量。不保证穷尽，但覆盖实际观测到的形态。
    """
    if not value:
        return value

    def _redact_assign(match: re.Match[str]) -> str:
        key = str(match.group(2))
        if _SENSITIVE_NAME_RE.search(key):
            return f"{match.group(1)}{key}=[redacted]"
        return str(match.group(0))

    def _redact_flag(match: re.Match[str]) -> str:
        flag = re.sub(r"^-+", "", str(match.group(2)))
        if _SENSITIVE_NAME_RE.search(flag):
            return f"{match.group(1)}{match.group(2)}{match.group(3)}[redacted]"
        return str(match.group(0))

    try:
        result = _INLINE_ASSIGNMENT_RE.sub(_redact_assign, value)
        result = _AUTH_HEADER_RE.sub(r"\1[redacted]", result)
        result = _SECRET_FLAG_RE.sub(_redact_flag, result)
        return _FEISHU_TOKEN_RE.sub("[redacted-token]", result)
    except Exception:
        # 脱敏本身失败时宁可丢内容也不能泄露原文
        return "[redaction-failed]"


def short(value: str | None, size: int = 12) -> str:
    """截断标识符用于日志，避免整条 message_id / chat_id 落盘."""
    if not value:
        return "-"
    return value[:size]


# ── 指标 ──────────────────────────────────────────────────────────


class Metrics:
    """进程内指标计数器.

    只做计数与最近错误留存，不做时间序列——排障需要的是「有没有、多少次、
    最后一次错在哪」，不是趋势图。
    """

    __slots__ = ("_counters", "_last_errors", "_lock")

    _MAX_LAST_ERRORS = 20

    def __init__(self) -> None:
        self._counters: Counter[str] = Counter()
        self._last_errors: list[str] = []
        self._lock = threading.Lock()

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def record_error(self, scope: str, error: BaseException | str) -> None:
        """记录一条脱敏后的错误摘要."""
        text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        entry = redact(f"{scope} | {text}")[:300]
        with self._lock:
            self._counters[f"error.{scope}"] += 1
            self._last_errors.append(entry)
            if len(self._last_errors) > self._MAX_LAST_ERRORS:
                del self._last_errors[0]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "last_errors": list(self._last_errors),
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._last_errors.clear()


# 全局单例：指标是进程级的，不随 turn 生灭
METRICS = Metrics()


def log_turn(level: int, turn_key: str, message: str, *args: Any) -> None:
    """带 turn 标识的结构化日志.

    统一前缀便于 grep：``[turn=xxxxxxxx] 具体内容``
    """
    try:
        logger.log(level, "[turn=%s] " + message, short(turn_key), *args)
    except Exception:
        pass

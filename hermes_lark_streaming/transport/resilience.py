"""错误分类与重试策略.

飞书 CardKit 的错误码语义差异极大：有的重试有意义（网关超时、内部错误），
有的重试只会浪费配额（元素超限、流式已关闭），有的必须立即停止整条流水线
（消息已删除）。把这套判断集中在这里，调用方只问「该怎么办」。
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Final

# ── 错误码 ────────────────────────────────────────────────────────

CARDKIT_RATE_LIMITED: Final = 230020  # 频控
CARDKIT_CONTENT_FAILED: Final = 230099  # 内容创建失败（通用码，需看子码）
CARDKIT_ELEMENT_LIMIT: Final = 11310  # 子码：元素数量超限
CARDKIT_STREAMING_CLOSED: Final = 300309  # 流式模式已关闭
CARDKIT_ELEMENT_MISSING: Final = 300313  # 目标元素不存在
CARDKIT_GATEWAY_TIMEOUT: Final = 2200
CARDKIT_INTERNAL_ERROR: Final = 1663
CARDKIT_SERVER_ERROR: Final = 300000

MSG_NOT_FOUND: Final = 1000023
MSG_DELETED: Final = 231003
MSG_RECALLED: Final = 230011

#: 可重试的瞬时错误
TRANSIENT_CODES: Final[frozenset[int]] = frozenset(
    {CARDKIT_GATEWAY_TIMEOUT, CARDKIT_INTERNAL_ERROR, CARDKIT_SERVER_ERROR}
)

#: 消息级终态错误 — 命中后整条流水线必须停止，继续调用只会刷错误日志
TERMINAL_MESSAGE_CODES: Final[frozenset[int]] = frozenset({MSG_NOT_FOUND, MSG_DELETED, MSG_RECALLED})

#: 重试退避（秒）
RETRY_DELAYS: Final[tuple[float, ...]] = (0.15, 0.5, 1.0)


class Action(StrEnum):
    """遇到错误后该做什么."""

    RETRY = "retry"  # 退避后重试
    ABORT_PIPELINE = "abort_pipeline"  # 消息已不存在，停止该 turn 的一切更新
    REBUILD_ELEMENT = "rebuild_element"  # 元素丢失，下轮用 add_elements 重建
    SPLIT_CARD = "split_card"  # 元素超限，拆卡
    GIVE_UP = "give_up"  # 静默放弃本次更新（如频控、流式已关闭）
    FAIL = "fail"  # 未知错误，记录并放弃


class FeishuAPIError(RuntimeError):
    """飞书 API 错误，携带错误码."""

    __slots__ = ("code", "operation")

    def __init__(self, message: str, code: int = 0, operation: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.operation = operation

    def sub_code(self) -> int | None:
        """从错误信息中提取子错误码.

        飞书把子码塞在 msg 字符串里，形如 ``ext=ErrCode: 11310; ...``
        """
        import re

        match = re.search(r"ErrCode:\s*(\d+)", str(self))
        return int(match.group(1)) if match else None

    def missing_element_id(self) -> str | None:
        """从 300313 错误中提取缺失的 element_id."""
        if self.code != CARDKIT_ELEMENT_MISSING:
            return None
        import re

        match = re.search(r"element_id[=:\s\"']+([A-Za-z0-9_\-]+)", str(self))
        return match.group(1) if match else None


def classify(error: BaseException) -> Action:
    """判断错误应如何处置."""
    if not isinstance(error, FeishuAPIError):
        # 网络异常等非 API 错误：可重试
        if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return Action.RETRY
        return Action.FAIL

    code = error.code

    if code in TERMINAL_MESSAGE_CODES:
        return Action.ABORT_PIPELINE
    if code in TRANSIENT_CODES:
        return Action.RETRY
    if code == CARDKIT_RATE_LIMITED:
        # 频控不重试：下一轮 flush 自然会带上最新内容，重试只会加重频控
        return Action.GIVE_UP
    if code == CARDKIT_STREAMING_CLOSED:
        # 卡片已封存，继续写是无意义的
        return Action.GIVE_UP
    if code == CARDKIT_ELEMENT_MISSING:
        return Action.REBUILD_ELEMENT
    if code == CARDKIT_CONTENT_FAILED:
        return Action.SPLIT_CARD if error.sub_code() == CARDKIT_ELEMENT_LIMIT else Action.FAIL
    return Action.FAIL


def is_terminal_message_error(error: BaseException) -> bool:
    return isinstance(error, FeishuAPIError) and error.code in TERMINAL_MESSAGE_CODES


class CircuitBreaker:
    """简单熔断器.

    连续失败达到阈值后打开，之后所有请求直接短路（不再尝试）。
    用于「织入层收纳持续失败」的兜底：宁可退回原生消息，也不能让插件
    在每条消息上重复失败、拖慢 Agent。

    不做半开探测：本进程内一旦熔断就保持到 gateway 重启。理由是这类
    失败通常是配置/权限/版本问题，不会自愈，反复探测只是噪音。
    """

    __slots__ = ("_failures", "_open", "_threshold")

    def __init__(self, threshold: int) -> None:
        self._threshold = max(1, threshold)
        self._failures = 0
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def failures(self) -> int:
        return self._failures

    def record_success(self) -> None:
        if not self._open:
            self._failures = 0

    def record_failure(self) -> bool:
        """记录失败，返回本次是否触发熔断."""
        if self._open:
            return False
        self._failures += 1
        if self._failures >= self._threshold:
            self._open = True
            return True
        return False

    def trip(self) -> bool:
        """直接打开熔断，跳过计数累积；返回本次是否由关闭态转为打开.

        用于调用方**已经独立判定为系统性故障**的场景（例如多类能力同时
        降级，说明是凭据 / 网络 / 权限问题而非单个回调的语义变化）。此时
        再要求慢慢累计到阈值毫无意义——问题已经确定，继续尝试只是徒劳，
        而每次徒劳都在拖慢 Agent。
        """
        if self._open:
            return False
        self._failures = max(self._failures, self._threshold)
        self._open = True
        return True

    def reset(self) -> None:
        self._failures = 0
        self._open = False

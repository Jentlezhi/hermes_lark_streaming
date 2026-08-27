"""L0 桥接层 — 唯一接触 Hermes 的层.

本层之外的任何模块都不应出现 Hermes 的符号。Hermes 升级的影响半径
被完全限制在这里。
"""

from __future__ import annotations

from .plugin import BootstrapReport, bootstrap, last_report, register, teardown
from .weave import WEAVER, AgentWeaver, WeaveError, WeaveReport

__all__ = [
    "WEAVER",
    "AgentWeaver",
    "BootstrapReport",
    "WeaveError",
    "WeaveReport",
    "bootstrap",
    "last_report",
    "register",
    "teardown",
]

"""自愈层 — 把一次性观测变成跨进程累积的经验.

**这一层是完全旁路的**：它读其他层的观测结果、写自己的状态文件，
再把学到的经验以「能力是否降级」的形式反馈回去。任何一步失败都被吞掉，
绝不影响卡片链路——可观测性与自愈能力都不允许成为故障源。

依赖方向：``orchestrator`` → ``selfheal``。本层不 import 除
:mod:`..observability` 与 :mod:`..config` 常量之外的任何上层模块，
状态文件由本层独占读写，其他层只通过 :class:`SelfHealer` 的方法访问。

能做什么：

- **精准降级**：某类收纳（notice / review / clarify / approval）连续失败
  达阈值时只关闭该类，其余能力照常工作，取代原先「一坏全坏」的全局熔断
- **经验继承**：降级结论落盘，下次 gateway 启动直接预降级，不必再白白失败
- **指纹作废**：Hermes 或插件的织入能力集合一变，全部经验立即作废重学
- **试探恢复**：预降级的能力每 N 个 turn 试探一次，成功即恢复并清除记录
"""

from __future__ import annotations

from .healer import (
    CAPABILITIES,
    SelfHealer,
    get_healer,
    reset_healers,
)
from .store import (
    SCHEMA_VERSION,
    activity_path,
    migrate_legacy_dir,
    read_activity,
    state_dir,
    state_path,
    write_activity,
)

__all__ = [
    "CAPABILITIES",
    "SCHEMA_VERSION",
    "SelfHealer",
    "activity_path",
    "get_healer",
    "migrate_legacy_dir",
    "read_activity",
    "reset_healers",
    "state_dir",
    "state_path",
    "write_activity",
]

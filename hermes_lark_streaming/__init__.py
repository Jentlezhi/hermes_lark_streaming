"""Hermes 飞书流式卡片插件.

架构分层（依赖方向严格单向，禁止反向依赖）::

    bridge/     L0 桥接层 — 唯一接触 Hermes 的层
    events/     L1 事件层 — Hermes 语义归一化为插件事件模型
    core/       L2 领域层 — 与飞书无关的纯逻辑
    render/     L3 渲染层 — Segment 转 CardKit JSON
    transport/  L4 传输层 — 飞书 API 调用与调度
    selfheal/   旁路层 — 跨进程经验积累与精准降级（不被任何层依赖）

设计要点见 docs/02-架构设计.md。
"""

from __future__ import annotations

__version__ = "1.0.0"

# 日志器名称统一常量，避免各模块散落字面量
LOGGER_NAME = "hermes_lark_streaming"

__all__ = ["LOGGER_NAME", "__version__"]

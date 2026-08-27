"""双语文案.

飞书卡片支持 ``i18n_content``：同一个元素带多语言文案，由**客户端**按用户
语言自动切换。因此服务端不需要判断用户语言，只要同时给出两份文案即可。
"""

from __future__ import annotations

from typing import Any, Final

#: 飞书 locale 声明。zh_cn 为默认，en_us 为回退
LOCALES: Final[list[str]] = ["zh_cn", "en_us"]

#: 文案表：key -> (中文, 英文)
_TEXTS: Final[dict[str, tuple[str, str]]] = {
    # 状态
    "processing": ("处理中…", "Processing…"),
    "thinking": ("思考中…", "Thinking…"),
    "status_completed": ("已完成", "Completed"),
    "status_error": ("执行失败", "Failed"),
    "status_stopped": ("已停止", "Stopped"),
    "status_interrupted": ("已中断 · 新消息已接续", "Interrupted · superseded"),
    "status_timeout": ("已超时收尾", "Timed out"),
    "status_waiting": ("等待确认", "Waiting"),
    "done": ("（本轮无文字输出）", "(no text output)"),
    # 面板标题
    "thought": ("思考过程", "Thought"),
    "thought_for": ("思考了 {}", "Thought for {}"),
    "thinking_panel": ("正在思考…", "Thinking…"),
    "tool_use": ("工具调用", "Tool use"),
    "tool_pending": ("准备执行工具…", "Preparing tools…"),
    "steps": ("{} 步", "{} step{}"),
    "notice_panel": ("运行提示", "Notices"),
    "review_panel": ("记忆与改进", "Memory & review"),
    # 工具状态
    "tool_running": ("执行中", "Running"),
    "tool_success": ("成功", "Succeeded"),
    "tool_error": ("失败", "Failed"),
    "tool_result": ("结果", "Result"),
    "tool_error_label": ("错误", "Error"),
    # 交互
    "clarify_pending": ("等待你的回复", "Waiting for your reply"),
    "clarify_resolved": ("已回复", "Answered"),
    "approval_pending": ("等待授权确认", "Waiting for approval"),
    "approval_resolved": ("已处理", "Handled"),
    "interaction_timeout": ("未在本轮内完成", "Not completed this turn"),
    # Footer
    "elapsed": ("耗时 {}", "Elapsed {}"),
    "context": ("上下文 {}", "Context {}"),
    "usage": ("额度 {}", "Usage {}"),
    # 折叠与截断
    "notice_overflow": ("另有 {} 条提示已折叠", "{} more notices collapsed"),
    "tool_dropped": ("另有 {} 步未展示", "{} more steps omitted"),
    "card_archived": ("已归档（续见下一张卡片）", "Archived (continued below)"),
    # 独立卡片
    "cron_title": ("定时任务", "Scheduled task"),
    "background_title": ("后台任务完成", "Background task done"),
    "no_response": ("（无输出）", "(no response)"),
}


def t(key: str) -> tuple[str, str]:
    """取双语文案，缺失时回退为 key 本身，避免 KeyError 打断渲染."""
    return _TEXTS.get(key, (key, key))


def zh(key: str) -> str:
    return t(key)[0]


def en(key: str) -> str:
    return t(key)[1]


def i18n(zh_text: str, en_text: str) -> dict[str, str]:
    """构造飞书 i18n_content 结构."""
    return {"zh_cn": zh_text, "en_us": en_text}


def i18n_of(key: str) -> dict[str, str]:
    zh_text, en_text = t(key)
    return i18n(zh_text, en_text)


def bilingual(key: str) -> dict[str, Any]:
    """构造带 i18n 的 plain_text 内容片段（content 用中文，作为默认展示）."""
    zh_text, en_text = t(key)
    return {"content": zh_text, "i18n_content": i18n(zh_text, en_text)}

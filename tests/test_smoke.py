"""冒烟测试 — 不依赖 Hermes 运行环境.

覆盖纯逻辑层（config / events / core / render），这部分占代码量的大头，
且不需要飞书 SDK 或 Hermes 源码即可验证。

运行::

    python3 -m pytest tests/ -v
    # 或不装 pytest 直接跑：
    python3 tests/test_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── 事件层 ────────────────────────────────────────────────────────


def test_event_model() -> None:
    from hermes_lark_streaming.events import EventKind, StreamEvent, make_event

    event = make_event(EventKind.ANSWER_DELTA, "turn-1", text="你好")
    assert event.kind == EventKind.ANSWER_DELTA
    assert event.turn_key == "turn-1"
    assert event.text() == "你好"
    assert event.text("missing") == ""
    assert event.number("missing") == 0.0
    assert event.mapping("missing") == {}
    assert not event.is_terminal

    terminal = StreamEvent(kind=EventKind.TURN_COMPLETED, turn_key="t")
    assert terminal.is_terminal


def test_status_classification() -> None:
    from hermes_lark_streaming.events import EventKind, classify_status_text, is_noise_status
    from hermes_lark_streaming.events.model import NoticeLevel

    # 噪音应被识别并丢弃
    assert is_noise_status("")
    assert is_noise_status("compression summary failed")
    assert not is_noise_status("Compacting context")

    kind, level = classify_status_text("💾 Memory updated")
    assert kind == EventKind.REVIEW

    kind, level = classify_status_text("Compacting context — summarizing")
    assert kind == EventKind.NOTICE
    assert level == NoticeLevel.INFO

    kind, level = classify_status_text("Rate limited. Waiting 3s, retrying")
    assert level == NoticeLevel.WARNING


def test_reasoning_split() -> None:
    from hermes_lark_streaming.events import split_reasoning_text, strip_reasoning_tags

    assert strip_reasoning_tags("普通文本") == "普通文本"
    assert strip_reasoning_tags("<think></think>") == ""

    reasoning, answer = split_reasoning_text("<think>推理内容</think>正式回答")
    assert reasoning == "推理内容"
    assert answer == "正式回答"

    reasoning, answer = split_reasoning_text("没有标签")
    assert reasoning == ""
    assert answer == "没有标签"

    # 未闭合标签：剩余全部算推理，不能泄露到正文
    reasoning, answer = split_reasoning_text("前言<think>未闭合")
    assert answer == "前言"
    assert reasoning == "未闭合"


# ── 领域层 ────────────────────────────────────────────────────────


def test_segment_ordering() -> None:
    """内容必须按到达顺序排列——这是单卡收敛的正确性基础."""
    from hermes_lark_streaming.core.segments import SegmentState, SegmentType

    state = SegmentState()
    state.append_reasoning("想一下")
    state.append_answer("回答A")
    state.on_tool_event(1)
    state.append_answer("回答B")

    types = [seg.type for seg in state.segments]
    assert types == [
        SegmentType.REASONING,
        SegmentType.ANSWER,
        SegmentType.TOOL,
        SegmentType.ANSWER,
    ]
    # 同类相邻应合并，不同类之间不应合并
    assert state.segments[1].text == "回答A"
    assert state.segments[3].text == "回答B"


def test_segment_notice_merge() -> None:
    """连续提示合并进一个元素，避免撑爆卡片元素预算."""
    from hermes_lark_streaming.core.segments import NoticeItem, SegmentState, SegmentType

    state = SegmentState()
    for index in range(5):
        state.append_notice(NoticeItem(text=f"提示{index}"), SegmentType.NOTICE)

    notice_segments = [seg for seg in state.segments if seg.type == SegmentType.NOTICE]
    assert len(notice_segments) == 1
    assert len(notice_segments[0].notices) == 5


def test_segment_interaction() -> None:
    from hermes_lark_streaming.core.segments import (
        InteractionKind,
        InteractionState,
        InteractionStatus,
        SegmentState,
    )

    state = SegmentState()
    state.open_interaction(InteractionState(kind=InteractionKind.APPROVAL, title="执行命令"))
    assert state.pending_interaction() is not None

    resolved = state.resolve_interaction(InteractionKind.APPROVAL, InteractionStatus.RESOLVED, "已批准")
    assert resolved is not None
    assert state.pending_interaction() is None

    # 终态时未决交互必须被收敛，否则卡片会永远停在「等待中」
    state.open_interaction(InteractionState(kind=InteractionKind.CLARIFY, title="请选择"))
    state.finalize(0)
    assert state.pending_interaction() is None


def test_turn_state_machine() -> None:
    from hermes_lark_streaming.core.turn import Turn, TurnState

    turn = Turn(turn_key="t1", message_id="om_1", chat_id="oc_1")
    assert turn.state == TurnState.IDLE

    assert turn.transition(TurnState.CREATING)
    assert turn.transition(TurnState.STREAMING)
    assert turn.state.allows_flush

    # 终态不可回退
    assert turn.transition(TurnState.COMPLETED)
    assert not turn.transition(TurnState.STREAMING)
    assert turn.state == TurnState.COMPLETED


def test_turn_content_gating() -> None:
    """终态之后不得再写入内容."""
    from hermes_lark_streaming.core.turn import Turn, TurnState

    turn = Turn(turn_key="t2", message_id="om_2", chat_id="oc_2")
    turn.transition(TurnState.STREAMING)
    assert turn.add_answer("内容")
    turn.transition(TurnState.COMPLETED)
    assert not turn.add_answer("终态后的内容")


def test_turn_summary_reflects_state() -> None:
    """会话列表摘要必须随状态变化——这是「切走看不出是否完成」的治理点."""
    from hermes_lark_streaming.config import Config
    from hermes_lark_streaming.core.turn import Turn, TurnState

    cfg = Config(Path("/nonexistent-profile-home"))
    turn = Turn(turn_key="t3", message_id="om_3", chat_id="oc_3")

    assert "启动" in turn.summary_text(cfg)

    turn.transition(TurnState.CREATING)
    turn.transition(TurnState.STREAMING)
    assert turn.summary_text(cfg)

    turn.add_answer("这是最终答案")
    turn.transition(TurnState.COMPLETED)
    summary = turn.summary_text(cfg)
    assert summary.startswith("✅")
    assert "最终答案" in summary


def test_delivery_semantics() -> None:
    """只有 TAKEN 允许抑制原生输出，UNKNOWN 必须放行以免丢消息."""
    from hermes_lark_streaming.core.turn import Delivery

    assert Delivery.TAKEN.should_suppress_native
    assert not Delivery.DECLINED.should_suppress_native
    assert not Delivery.UNKNOWN.should_suppress_native


def test_registry_lookup_and_recycle() -> None:
    from hermes_lark_streaming.config import Config
    from hermes_lark_streaming.core.registry import TurnRegistry
    from hermes_lark_streaming.core.turn import TurnState

    registry = TurnRegistry(Config(Path("/nonexistent-profile-home")))
    turn = registry.create(
        turn_key="om_x",
        message_id="om_x",
        chat_id="oc_x",
        session_key="sess_x",
    )
    assert turn is not None

    # 三种线索都应能定位到同一个 turn
    assert registry.get("om_x") is turn
    assert registry.get_active_by_chat("oc_x") is turn
    assert registry.get_active_by_session("sess_x") is turn
    assert registry.resolve_active(chat_id="oc_x") is turn

    # 重复创建同键且未终态时应被拒绝
    assert registry.create(turn_key="om_x", message_id="om_x", chat_id="oc_x") is None

    # 终态后不再作为活跃 turn 返回
    turn.state = TurnState.COMPLETED
    assert registry.get_active_by_chat("oc_x") is None

    registry.remove("om_x")
    assert registry.get("om_x") is None


def test_tool_tracker_and_redaction() -> None:
    from hermes_lark_streaming.core.tooltrack import ToolTracker

    tracker = ToolTracker()
    tracker.record_start("bash", 'curl -H "Authorization: Bearer sk-secret123456" https://x.com')
    assert tracker.current_action()
    tracker.record_end("bash", output='{"ok": true}')

    steps = tracker.build_display_steps()
    assert len(steps) == 1
    assert steps[0]["status"] == "success"
    # 凭据必须在进入卡片前被打码
    assert "sk-secret123456" not in steps[0]["detail"]
    assert steps[0]["result_block"] is not None


def test_tool_tracker_step_cap() -> None:
    from hermes_lark_streaming.core.tooltrack import MAX_TOOL_STEPS, ToolTracker

    tracker = ToolTracker()
    for index in range(MAX_TOOL_STEPS + 10):
        tracker.record_start(f"tool{index}")
    assert tracker.step_count == MAX_TOOL_STEPS
    assert tracker.dropped == 10


# ── 渲染层 ────────────────────────────────────────────────────────


def test_streaming_card_enables_typewriter() -> None:
    """打字机效果的三要素之一：卡片必须声明 streaming_mode."""
    from hermes_lark_streaming.render import build_streaming_card

    card = build_streaming_card(header_enabled=True, width_mode="default", summary="处理中")
    assert card["schema"] == "2.0"
    assert card["config"]["streaming_mode"] is True
    # 客户端插值渲染参数——服务端节流与用户观感由此解耦
    assert card["config"]["streaming_config"]["print_frequency_ms"]["default"] == 15
    assert card["config"]["summary"]["content"] == "处理中"
    assert "header" in card
    # 占位卡只含 loading 元素，作为后续 add_elements 的锚点
    assert len(card["body"]["elements"]) == 1


def test_complete_card_renders_all_segment_kinds() -> None:
    """六种 Segment 都要能渲染——这是单卡收敛的渲染保证."""
    from hermes_lark_streaming.core.segments import (
        InteractionKind,
        InteractionState,
        NoticeItem,
        SegmentState,
        SegmentType,
    )
    from hermes_lark_streaming.render import build_complete_card

    state = SegmentState()
    state.append_reasoning("思考内容")
    state.append_answer("回答内容")
    state.append_notice(NoticeItem(text="正在压缩上下文"), SegmentType.NOTICE)
    state.append_notice(NoticeItem(text="记忆已更新"), SegmentType.REVIEW)
    state.open_interaction(InteractionState(kind=InteractionKind.APPROVAL, title="执行命令"))
    state.finalize(0)

    card = build_complete_card(
        segments=state.segments,
        all_tool_steps=[],
        footer_data={"duration": 1.5, "model": "test-model"},
        footer_fields=[["status", "elapsed", "model"]],
        footer_show_label=False,
        footer_enabled=True,
        footer_text_size="notation",
        body_text_size="normal_v2",
        panel_expanded=False,
        show_tool_use=True,
        header_enabled=True,
        width_mode="default",
        summary="已完成",
    )
    assert card["schema"] == "2.0"
    # 终态卡不应再带 streaming_mode
    assert "streaming_mode" not in card["config"]
    assert card["header"]["template"] == "green"

    serialized = str(card)
    assert "思考内容" in serialized
    assert "回答内容" in serialized
    assert "正在压缩上下文" in serialized
    assert "记忆已更新" in serialized
    assert "执行命令" in serialized


def test_markdown_split_preserves_code_fences() -> None:
    """长文本切分绝不能切在代码围栏中间，否则整段渲染成代码."""
    from hermes_lark_streaming.render.markdown import split_long_text

    text = "前言\n\n```python\n" + "x = 1\n" * 300 + "```\n\n后记"
    chunks = split_long_text(text, limit=500)
    assert len(chunks) > 1
    for chunk in chunks:
        # 每一块的围栏数量必须成对
        assert chunk.count("```") % 2 == 0


def test_markdown_table_downgrade() -> None:
    from hermes_lark_streaming.render.markdown import downgrade_wide_tables

    wide = (
        "| A | B | C | D | E | F |\n"
        "|---|---|---|---|---|---|\n"
        "| 1 | 2 | 3 | 4 | 5 | 6 |\n"
    )
    result = downgrade_wide_tables(wide, max_columns=5)
    assert "**A**" in result  # 已降级为字段列表

    narrow = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    assert downgrade_wide_tables(narrow, max_columns=5) == narrow.rstrip("\n")


def test_image_ref_roundtrip() -> None:
    from hermes_lark_streaming.render.markdown import find_image_refs, replace_image_refs

    text = "看图 ![alt](https://example.com/a.png) 结束"
    urls = find_image_refs(text)
    assert urls == ["https://example.com/a.png"]

    replaced = replace_image_refs(text, {"https://example.com/a.png": "img_key_123"})
    assert "img_key_123" in replaced
    # 未映射的图片保持原样，不能因单张失败丢内容
    assert replace_image_refs(text, {}) == text


def test_notice_text_cannot_break_out_of_font_tag() -> None:
    """提示文本要挡住标签起始符，但不能顺手把 markdown 也转义掉.

    提示的来源包括子任务摘要（模型生成）和 Hermes 状态消息，都是不可信输入。
    不转义 ``<`` 有两个后果：``</font>`` 提前闭合让后面的文字失去配色；更常见的
    是模型写出 ``a < b``，飞书把后面的内容当未知标签吞掉——那是丢内容。

    但这里不能用 ``escape_inline``：它连 ``**粗体**`` 一起转义，提示里的 markdown
    是希望正常渲染的。
    """
    from hermes_lark_streaming.core.segments import NoticeItem
    from hermes_lark_streaming.events import NoticeLevel
    from hermes_lark_streaming.render.elements import notice_block
    from hermes_lark_streaming.render.markdown import escape_inline, escape_tags

    content = notice_block(
        [NoticeItem(text="子任务完成</font><font color='red'>注入的红字")],
        is_review=False,
    )["content"]
    # 注入的标签被转义。剥掉所有已转义的起始符后，未转义的 font 只剩我们自己
    # 插入的那一个——直接 count("<font") 会把 "\<font" 里的子串也数进去
    assert "\\</font>" in content
    assert "\\<font" in content
    assert content.replace("\\<", "").count("<font") == 1

    # 丢内容的常见形态
    assert "a \\< b" in notice_block([NoticeItem(text="a < b")], is_review=False)["content"]

    # markdown 保留 —— 这是不用 escape_inline 的理由，顺手把两者的差别钉住
    assert escape_tags("**耗时** `bash`") == "**耗时** `bash`"
    assert escape_inline("**耗时**") == "\\*\\*耗时\\*\\*"

    # 我们自己写的提示文案不含标签起始符，不受影响
    plain = "⏱️ 超过 90 秒无更新，卡片自动收尾（任务可能仍在运行）"
    assert plain in notice_block([NoticeItem(text=plain, level=NoticeLevel.WARNING)], is_review=False)["content"]


def test_card_icons_are_configurable() -> None:
    """卡片符号可配，且默认值与配置化之前逐字相同.

    这些符号原先散在 5 个文件的 31 个 f-string 里。集中之后最要紧的是**默认行为
    不变**：不配任何东西时，每个位置显示的符号必须和硬编码时一模一样。
    """
    import tempfile
    import textwrap

    from hermes_lark_streaming import icons
    from hermes_lark_streaming.config import Config
    from hermes_lark_streaming.core.segments import InteractionKind, InteractionState, NoticeItem
    from hermes_lark_streaming.core.turn import Turn, TurnState
    from hermes_lark_streaming.events import NoticeLevel
    from hermes_lark_streaming.render import card as card_mod
    from hermes_lark_streaming.render import elements

    def home(body: str = "") -> Path:
        path = Path(tempfile.mkdtemp())
        if body:
            (path / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    # 默认表覆盖全部 19 个语义键，且都非空
    assert len(icons.DEFAULT_ICONS) == 19
    assert all(icons.DEFAULT_ICONS.values())

    default = icons.resolve(Config(home()))
    assert default == icons.DEFAULT_ICONS

    # 默认行为与硬编码时逐字相同 —— 这几处是配置化前的原样
    assert elements.reasoning_panel("想…", marks=default)["header"]["title"]["content"].startswith("💭 ")
    assert elements.tool_panel([], marks=default)["header"]["title"]["content"].startswith("🛠️ ")
    assert elements.notice_block(
        [NoticeItem(text="压缩中", level=NoticeLevel.WARNING)], is_review=False, marks=default
    )["content"].startswith("ℹ️ <font color='orange'>⚠ ")
    assert elements.notice_block([NoticeItem(text="x")], is_review=True, marks=default)[
        "content"
    ].startswith("🧠 ")
    assert elements.interaction_block(
        InteractionState(kind=InteractionKind.APPROVAL, title="rm"), marks=default
    )["content"].startswith("🔐 ")
    assert elements._footer_field("status", {}, False, False, False, "", default)[0] == "✅ 已完成"
    assert card_mod.build_cron_card("x", task_name="日报", marks=default)["header"]["title"][
        "content"
    ].startswith("⏰ ")

    # 覆盖生效，未配的键保持默认，未知键忽略
    custom = icons.resolve(
        Config(
            home("""
                streaming:
                  enabled: true
                  icons:
                    reasoning: "🤔"
                    subagent: ""
                    根本没这个键: "💀"
            """)
        )
    )
    assert custom["reasoning"] == "🤔"
    assert custom["completed"] == icons.DEFAULT_ICONS["completed"]
    assert "根本没这个键" not in custom
    assert elements.reasoning_panel("想…", marks=custom)["header"]["title"]["content"].startswith("🤔 ")

    # 空字符串 = 不要符号，且连尾随空格一起省掉，文案不会诡异地缩进一格
    assert icons.with_space(custom, "subagent") == ""
    assert icons.get(custom, "subagent") == ""

    # 会话列表预览也走同一张表
    turn = Turn(turn_key="t", message_id="m", chat_id="c")
    turn.transition(TurnState.STREAMING)
    cfg = Config(home())
    assert turn.summary_text(cfg, marks=default) == "💭 思考中…"
    assert turn.summary_text(cfg, marks=custom) == "🤔 思考中…"
    # 不传 marks 时退回默认表：漏传只是用默认符号，不该抛错
    assert turn.summary_text(cfg) == "💭 思考中…"


def test_element_budget_estimation() -> None:
    """预算必须宁可高估：低估会导致整次更新失败而非截断."""
    from hermes_lark_streaming.core.segments import SegmentState
    from hermes_lark_streaming.render import estimate_segment, exceeds

    state = SegmentState()
    reasoning = state.append_reasoning("x")
    answer = state.append_answer("y")

    assert estimate_segment(reasoning, []) >= 4
    assert estimate_segment(answer, []) == 1
    assert exceeds(178, 5, 180)
    assert not exceeds(10, 5, 180)


def test_growing_tool_panel_stays_within_card_limit() -> None:
    """工具面板在同一段内持续增长时，单卡元素数不得越过飞书上限.

    工具步骤是追加进**同一个** segment 的（``on_tool_event`` 里相邻同类只标脏），
    所以它的元素数会一直涨，而拆卡的触发点在「新段创建」分支里——纯工具增长
    产生不了新段。若预算不跟着对账、段不在步边界截断，面板会一路涨到 200 上限
    之外，``batch_update`` 从此每次整批失败且不自愈（拿基线跑同一场景：61 步
    时单卡真实元素 429，12 轮里 7 轮越限）。
    """
    import asyncio
    import tempfile
    from typing import Any

    from hermes_lark_streaming.core.segments import SegmentType
    from hermes_lark_streaming.core.turn import Turn, TurnState
    from hermes_lark_streaming.orchestrator import Orchestrator, _TurnRuntime
    from hermes_lark_streaming.render.budget import estimate_tool_elements
    from hermes_lark_streaming.transport import FlushScheduler, MessageGuard

    #: 飞书单卡元素硬上限。越过即整次更新失败，不是截断
    feishu_hard_limit = 200

    class _SilentClient:
        async def batch_update(self, card_id: str, actions: list[Any], *, sequence: int) -> None:
            return None

        async def stream_element(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def create_card(self, card: dict[str, Any]) -> str:
            return "card-next"

        async def reply_with_card(self, anchor_id: str, card_id: str) -> str:
            return "msg-next"

    def live_elements(turn: Turn) -> int:
        """当前这张卡上真实存活的元素数：loading + 本卡已创建的工具段."""
        steps = turn.tools.build_display_steps()
        total = 1
        for seg in turn.segment_state.segments[turn.split_index :]:
            if seg.type == SegmentType.TOOL and seg.created:
                total += estimate_tool_elements(
                    seg.tool_offset, seg.tool_end_offset or len(steps), steps
                )
        return total

    async def scenario() -> tuple[int, int, int]:
        orch = Orchestrator(Path(tempfile.mkdtemp()))
        orch._client = _SilentClient()  # type: ignore[assignment]

        turn = Turn(turn_key="t", message_id="m", chat_id="c")
        turn.bind_card(card_id="card-1", card_msg_id="msg-1")
        turn.transition(TurnState.STREAMING)
        turn.element_count = 1  # loading 占位
        scheduler = FlushScheduler(lambda: orch._flush(turn), loop=asyncio.get_running_loop())
        scheduler.set_ready(True)
        orch._runtimes[turn.turn_key] = _TurnRuntime(
            scheduler=scheduler,
            guard=MessageGuard(
                anchor_id="m",
                card_msg_id_getter=lambda: turn.card_msg_id,
                on_terminate=turn.mark_fallback,
            ),
        )

        turn.add_tool_start("bash", "make build")
        await orch._flush(turn)
        peak = live_elements(turn)
        for _ in range(12):
            for _ in range(5):
                turn.add_tool_end("bash", output='{"ok":true,"lines":120}')
                turn.add_tool_start("bash", "next")
            await orch._flush(turn)
            peak = max(peak, live_elements(turn))
        return peak, len(turn.segment_state.segments), turn.element_count

    peak, segment_count, element_count = asyncio.run(scenario())

    # 单卡从不越过飞书硬上限
    assert peak <= feishu_hard_limit, peak
    # 超预算的面板确实被切开了（基线只有 1 段）
    assert segment_count > 1, segment_count
    # 预算跟着真实占用走，不再停在创建那一刻
    assert element_count > 1, element_count


def test_cron_and_background_cards() -> None:
    from hermes_lark_streaming.render import build_background_card, build_cron_card

    cron = build_cron_card("任务结果", task_name="每日报告", run_time="2026-08-23T09:00:00")
    assert cron["schema"] == "2.0"
    assert "每日报告" in cron["header"]["title"]["content"]

    background = build_background_card("分析日志", "分析完成")
    assert background["schema"] == "2.0"
    assert "分析日志" in background["header"]["title"]["content"]


# ── 配置层 ────────────────────────────────────────────────────────


def test_config_safe_defaults() -> None:
    """配置缺失时必须回落到安全默认值，不能抛异常."""
    from hermes_lark_streaming.config import Config

    cfg = Config(Path("/nonexistent-profile-home"))
    assert cfg.enabled is False  # 未显式开启则不启用
    # 凭据可能来自 Hermes 的 secret_scope（进程级，不随 profile_home 变化），
    # 所以只在确实拿不到进程级凭据时才断言为 False——在 Hermes 环境里读到
    # 凭据是正确行为，不是缺陷
    if not (cfg.env_app_id and cfg.env_app_secret):
        assert cfg.has_credentials is False
    assert cfg.header_enabled is True  # 状态条默认开启
    assert cfg.summary_enabled is True
    assert cfg.width_mode == "default"
    assert cfg.element_threshold == 180
    assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]
    assert cfg.capture_enabled("notice") is True
    assert cfg.capture_enabled("approval") is True


def test_redaction() -> None:
    from hermes_lark_streaming.observability import redact

    assert "secret123456" not in redact("api_key=secret123456")
    assert "abcdefghijk" not in redact("Authorization: Bearer abcdefghijk")
    assert redact("普通文本") == "普通文本"


def test_circuit_breaker() -> None:
    from hermes_lark_streaming.transport.resilience import CircuitBreaker

    breaker = CircuitBreaker(threshold=3)
    assert not breaker.is_open
    assert not breaker.record_failure()
    assert not breaker.record_failure()
    assert breaker.record_failure()  # 第三次触发熔断
    assert breaker.is_open


def test_error_classification() -> None:
    from hermes_lark_streaming.transport.resilience import (
        CARDKIT_RATE_LIMITED,
        MSG_DELETED,
        Action,
        FeishuAPIError,
        classify,
    )

    assert classify(FeishuAPIError("deleted", MSG_DELETED)) == Action.ABORT_PIPELINE
    assert classify(FeishuAPIError("rate", CARDKIT_RATE_LIMITED)) == Action.GIVE_UP
    assert classify(FeishuAPIError("timeout", 2200)) == Action.RETRY


# ── 织入层（纯语言机制，不依赖 Hermes）──────────────────────────────


def test_descriptor_mechanics() -> None:
    """整个升级免疫方案的语言学基础：数据描述符优先于实例 __dict__."""
    from hermes_lark_streaming.bridge.weave import _verify_descriptor_mechanics

    _verify_descriptor_mechanics()  # 不抛异常即通过


def test_slots_detection() -> None:
    from hermes_lark_streaming.bridge.weave import _has_blocking_slots

    class Plain:
        pass

    class Slotted:
        __slots__ = ("a",)

    class SlottedWithDict:
        __slots__ = ("a", "__dict__")

    assert not _has_blocking_slots(Plain)
    assert _has_blocking_slots(Slotted)
    assert not _has_blocking_slots(SlottedWithDict)


# ── 中断捕获 ──────────────────────────────────────────────────────


def test_adapter_method_names_match_interceptors() -> None:
    """卸载清单与拦截器清单必须一致，否则卸载会漏掉方法."""
    import tempfile

    from hermes_lark_streaming.bridge.adapter import _METHOD_NAMES, _interceptors
    from hermes_lark_streaming.orchestrator import Orchestrator

    orch = Orchestrator(Path(tempfile.mkdtemp()))
    assert set(_interceptors(orch)) == set(_METHOD_NAMES)


def test_uninstall_entrypoints_are_safe_when_not_woven() -> None:
    """未织入时调卸载必须返回 False 而不是抛异常.

    ``_rollback`` 会在启动失败的任意阶段被调用，此时可能只装了一部分——
    任何一个卸载入口抛异常都会中断后续还原，留下半卸载状态。
    """
    from hermes_lark_streaming.bridge.adapter import uninstall_adapter_hook
    from hermes_lark_streaming.bridge.interrupt import uninstall_interrupt_hook
    from hermes_lark_streaming.bridge.lifecycle import uninstall_conversation_hook
    from hermes_lark_streaming.bridge.subagent import uninstall_subagent_hook

    for fn in (
        uninstall_adapter_hook,
        uninstall_interrupt_hook,
        uninstall_conversation_hook,
        uninstall_subagent_hook,
    ):
        assert fn() is False, f"{fn.__name__} 在未织入时应返回 False"


def test_rollback_restores_every_class_method_weave() -> None:
    """回滚必须覆盖全部类方法织入点，否则「已回滚」是假话.

    守住实际踩过的缺陷：``_rollback`` 一度只还原回调描述符，对话主方法 /
    适配器 / 中断 / 子任务四处类方法替换全都留在进程里，而 ``selftest``
    照旧输出「已回滚（当前进程不保留织入）」。
    """
    import tempfile

    try:
        from hermes_lark_streaming.bridge.weave import AgentWeaver

        agent_class = AgentWeaver.locate_agent_class()
    except Exception:
        return  # 非 Hermes 环境：上面两条测试已覆盖可测的部分

    from hermes_lark_streaming.bridge import plugin
    from hermes_lark_streaming.bridge.interrupt import _INTERRUPT_METHOD, install_interrupt_hook
    from hermes_lark_streaming.bridge.lifecycle import _CONVERSATION_METHODS, install_conversation_hook
    from hermes_lark_streaming.orchestrator import Orchestrator

    orch = Orchestrator(Path(tempfile.mkdtemp()))
    before = {name: agent_class.__dict__.get(name) for name in (_INTERRUPT_METHOD, *_CONVERSATION_METHODS)}

    assert install_interrupt_hook(orch) is True
    install_conversation_hook(orch)
    assert agent_class.__dict__.get(_INTERRUPT_METHOD) is not before[_INTERRUPT_METHOD]

    plugin._rollback()

    for name, original in before.items():
        assert agent_class.__dict__.get(name) is original, f"{name} 未还原"


def test_fmt_duration() -> None:
    from hermes_lark_streaming.__main__ import _fmt_duration

    assert _fmt_duration(45) == "45s"
    assert _fmt_duration(60) == "1m"
    assert _fmt_duration(125) == "2m5s"
    assert _fmt_duration(3600) == "1h"
    assert _fmt_duration(3725) == "1h2m"


def test_activity_verdict() -> None:
    """升级建议要区分四种情形，它们对用户意味着完全不同的动作."""
    import os

    from hermes_lark_streaming.__main__ import _ACTIVITY_FRESH_SEC, _activity_verdict

    ok, detail = _activity_verdict(None, 0)
    assert ok and "尚无" in detail

    # 心跳过期说明 gateway 已停，此时可以直接升级
    ok, detail = _activity_verdict({"at": 1, "active": [{"age_sec": 10}]}, _ACTIVITY_FRESH_SEC + 1)
    assert ok and "过期" in detail

    ok, detail = _activity_verdict({"at": 1, "active": []}, 5)
    assert ok and "无活跃" in detail

    ok, detail = _activity_verdict(
        {"at": 1, "pid": os.getpid(), "active": [{"age_sec": 750}, {"age_sec": 30}]}, 5
    )
    assert not ok, "有任务在跑时不该建议升级"
    assert "12m30s" in detail, f"应取最久的那个任务: {detail}"

    # 心跳新鲜但写它的进程已退出（selftest 演练 / gateway 崩溃残留）：
    # 不识别这种情况会让用户白等一轮
    ok, detail = _activity_verdict({"at": 1, "pid": 2**31 - 1, "active": [{"age_sec": 5}]}, 5)
    assert ok and "已退出" in detail

    # 类型不可信：心跳可能被手改或跨版本残留，脏数据不能让判定崩掉
    assert _activity_verdict({"at": 1, "active": "bad"}, 5)[0] is True
    assert _activity_verdict({"at": 1, "active": ["x"]}, 5)[0] is True
    assert _activity_verdict({"at": 1, "pid": "bad", "active": []}, 5)[0] is True


def test_process_alive_probe() -> None:
    """pid 存活探测不得发送任何信号，也不得因异常 pid 崩掉."""
    import os

    from hermes_lark_streaming.__main__ import _process_alive

    assert _process_alive(os.getpid()) is True
    assert _process_alive(0) is False
    assert _process_alive(-1) is False
    # 32 位 pid 上限附近几乎不可能有真实进程
    assert _process_alive(2**31 - 1) is False


def test_activity_heartbeat_roundtrip() -> None:
    """心跳要能被另一个进程读回；损坏时安全降级为「无记录」."""
    import tempfile

    from hermes_lark_streaming.selfheal import activity_path, read_activity, write_activity

    home = Path(tempfile.mkdtemp())
    assert read_activity(home) is None, "文件不存在时应返回 None"

    payload = {"at": 123, "pid": 1, "active": []}
    assert write_activity(home, payload) is True
    assert read_activity(home) == payload

    activity_path(home).write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert read_activity(home) is None


def test_activity_snapshot_excludes_message_content() -> None:
    """心跳只写标识与状态，绝不含消息正文，且标识必须截断."""
    import json
    import tempfile

    from hermes_lark_streaming.core.turn import TurnState
    from hermes_lark_streaming.orchestrator import Orchestrator
    from hermes_lark_streaming.selfheal import activity_path

    home = Path(tempfile.mkdtemp())
    orch = Orchestrator(home)
    turn = orch.registry.create(
        turn_key="turn-key-should-be-truncated", message_id="om_x", chat_id="oc_should_be_truncated"
    )
    turn.transition(TurnState.CREATING)
    turn.transition(TurnState.STREAMING)
    turn.add_answer("这是不该出现在心跳里的正文")

    assert orch.publish_activity() is True
    raw = activity_path(home).read_text(encoding="utf-8")
    assert "不该出现在心跳里" not in raw, "心跳绝不能包含消息正文"

    entry = json.loads(raw)["active"][0]
    assert entry["chat"] == "oc_shoul", "chat_id 必须截断"
    assert len(entry["turn"]) <= 8
    assert entry["state"] == "streaming"


def test_lifecycle_notice_detection() -> None:
    """gateway 关闭/重启通知必须被识别，以便同时进卡片与聊天.

    这类消息在 ``stop()`` 开头发出，事件循环随时会停；若被卡片「接管」而
    卡片又来不及更新，用户就彻底看不到它。识别失败即等于丢消息。
    """
    from hermes_lark_streaming.events import is_lifecycle_notice

    assert is_lifecycle_notice("⚠️ Gateway shutting down — Your current task will be interrupted.")
    assert is_lifecycle_notice(
        "⚠️ Gateway restarting — Your current task will be interrupted. "
        "Send any message after restart and I'll try to resume where you left off."
    )
    # 普通状态提示不该命中——它们正常收进卡片并抑制原生输出
    assert not is_lifecycle_notice("↪ Redirected current run (iteration 1/90).")
    assert not is_lifecycle_notice("💾 Self-improvement review: Skill 'x' created.")
    assert not is_lifecycle_notice("")


def test_interrupt_classification() -> None:
    """中断三态分类，依据 Hermes 自己的 hard_cancel / message 语义."""
    from hermes_lark_streaming.bridge.interrupt import _classify

    assert _classify((), {"hard_cancel": True}) == "stopped"
    assert _classify(("下一条消息",), {}) == "interrupted"
    assert _classify((), {"message": "下一条消息"}) == "interrupted"
    # 显式停止优先：/stop 时 Hermes 也可能同时带上文本
    assert _classify(("文本",), {"hard_cancel": True}) == "stopped"
    # redirect：无消息也非硬停，没有后继消息，不能说「已接续」
    assert _classify((), {}) == "stopped"
    assert _classify((None,), {}) == "stopped"
    assert _classify(("   ",), {}) == "stopped"


def test_abort_reason_distinguishes_stop_from_supersede() -> None:
    """用户主动 /stop 与被新消息顶掉，卡片文案与配色都要分开."""
    from hermes_lark_streaming.config import Config
    from hermes_lark_streaming.core.turn import Turn, TurnState
    from hermes_lark_streaming.render import elements

    cfg = Config(Path("/nonexistent-profile-home"))
    turn = Turn(turn_key="t-stop", message_id="om_s", chat_id="oc_s")
    turn.transition(TurnState.CREATING)
    turn.transition(TurnState.STREAMING)
    turn.transition(TurnState.ABORTED)

    assert turn.summary_text(cfg) == "⏹️ 已停止"
    turn.abort_reason = "interrupted"
    assert "接续" in turn.summary_text(cfg)

    # header 配色：主动停止用红色，被接续用橙色（不是错误）
    assert elements.header("stopped")["template"] == "red"
    assert elements.header("interrupted")["template"] == "orange"
    assert "中断" in elements.header("interrupted")["title"]["content"]


# ── 订阅额度与多 bot 投递 ──────────────────────────────────────────


def test_usage_footer_field() -> None:
    """额度字段有值才渲染；查不到时整个字段消失而非留占位符."""
    from hermes_lark_streaming.render import elements

    filled = elements.footer_elements(
        {"usage": "5h 42% · 周 18%"}, fields=[["usage"]], show_label=False, text_size="notation"
    )
    assert "42%" in str(filled)

    empty = elements.footer_elements({}, fields=[["usage"]], show_label=False, text_size="notation")
    assert "额度" not in str(empty)


def test_usage_disabled_by_default() -> None:
    """额度查询要打外部 API，属可见副作用，默认必须关闭."""
    import tempfile

    from hermes_lark_streaming.bridge.usage import fetch_usage_line, reset_cache
    from hermes_lark_streaming.config import Config

    home = Path(tempfile.mkdtemp())
    cfg = Config(home)
    assert cfg.usage_enabled is False
    reset_cache()
    assert fetch_usage_line(cfg) == ""

    # 开启但服务商不支持额度接口时同样返回空，不做无谓的网络请求
    (home / "config.yaml").write_text(
        "model:\n  provider: deepseek\nstreaming:\n  usage:\n    enabled: true\n", encoding="utf-8"
    )
    cfg2 = Config(home)
    assert cfg2.usage_enabled is True
    assert cfg2.hermes_provider == "deepseek"
    reset_cache()
    assert fetch_usage_line(cfg2) == ""


def test_multi_bot_config_parsing() -> None:
    """多 bot 配置：半套凭据与悬空绑定都要被丢弃，base_url 继承顶层."""
    import tempfile

    from hermes_lark_streaming.config import Config

    home = Path(tempfile.mkdtemp())
    (home / "config.yaml").write_text(
        "feishu:\n"
        "  app_id: cli_default\n"
        "  app_secret: default_secret\n"
        "  base_url: https://open.larksuite.com\n"
        "  bots:\n"
        "    ops:\n"
        "      app_id: cli_ops\n"
        "      app_secret: ops_secret\n"
        "    broken:\n"
        "      app_id: cli_broken\n"
        "  chat_bindings:\n"
        "    oc_ops: ops\n"
        "    oc_ghost: nonexistent\n"
        "  native_chats:\n"
        "    - oc_public\n",
        encoding="utf-8",
    )
    cfg = Config(home)

    assert set(cfg.bots) == {"ops"}, "缺 app_secret 的 bot 必须丢弃"
    # 断言继承关系本身而非具体域名：顶层 base_url 在有进程级凭据时来自
    # 环境推导，硬编码期望值会让测试依赖运行环境
    assert cfg.bots["ops"]["base_url"] == cfg.base_url, "未指定时应继承顶层域名"
    assert cfg.chat_bindings == {"oc_ops": "ops"}, "指向不存在 bot 的绑定必须丢弃"
    assert cfg.native_chats == frozenset({"oc_public"})


def test_native_chat_is_not_captured() -> None:
    """配置为原生的会话一张卡都不建."""
    import tempfile

    from hermes_lark_streaming.orchestrator import Orchestrator

    home = Path(tempfile.mkdtemp())
    (home / "config.yaml").write_text(
        "feishu:\n  app_id: a\n  app_secret: b\n  native_chats:\n    - oc_public\n", encoding="utf-8"
    )
    orch = Orchestrator(home)
    assert orch.is_native_chat("oc_public")
    assert not orch.is_native_chat("oc_other")
    assert not orch.is_native_chat(None)




def test_subagent_terminal_styles_match_hermes_states() -> None:
    """子任务终态映射要与 Hermes 的 ``SubagentState`` 对齐.

    漏一档就会在卡片上显示成「子任务结束（XXX）」这种半成品文案。在 Hermes
    环境里这条测试能直接发现「Hermes 新增了终态」。
    """
    from hermes_lark_streaming.bridge.subagent import _TERMINAL_STYLE

    try:
        from agent.subagent_lifecycle import SubagentState  # type: ignore[import-not-found]
    except Exception:
        assert set(_TERMINAL_STYLE) == {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELLED"}
        return

    non_terminal = {"PENDING", "STARTING", "RUNNING", "CANCEL_REQUESTED", "UNKNOWN"}
    terminal = {state.value for state in SubagentState} - non_terminal
    missing = terminal - set(_TERMINAL_STYLE)
    assert not missing, f"Hermes 新增了未覆盖的子任务终态: {sorted(missing)}"


def test_capabilities_cover_all_capture_kinds() -> None:
    """自愈的能力维度必须与 capture 配置项一一对应.

    两边不一致就会出现裂缝：配置能关但学不到（无法精准降级），或学到了
    却没有对应配置项（用户无法覆盖插件的决定）。
    """
    from hermes_lark_streaming.selfheal import CAPABILITIES

    assert set(CAPABILITIES) == {"notice", "review", "clarify", "approval", "subagent"}


def _healer(**kwargs: object):
    """在独立临时目录上构造 healer，避免测试间互相污染."""
    import tempfile

    from hermes_lark_streaming.selfheal import SelfHealer

    params: dict = {"degrade_threshold": 3, "probe_interval": 4}
    params.update(kwargs)
    home = Path(tempfile.mkdtemp())
    healer = SelfHealer(home, "1.0.0", **params)
    healer.record_weave(
        ok=True, hermes_version="0.20.5", callbacks=["cb_a", "cb_b"], conversation=["run_conversation"]
    )
    return healer, home


def test_selfheal_precise_degrade() -> None:
    """单个能力连续失败只降级它自己，其余能力照常工作."""
    healer, _ = _healer()

    assert healer.record_failure("notice") is False
    assert healer.record_failure("notice") is False
    assert healer.record_failure("notice") is True  # 第 3 次达阈值

    assert healer.is_degraded("notice")
    assert not healer.is_degraded("review")
    assert not healer.is_degraded("approval")
    assert healer.degraded_capabilities() == ["notice"]


def test_selfheal_experience_survives_restart() -> None:
    """降级结论落盘，新进程直接继承，不必再白白失败一遍."""
    from hermes_lark_streaming.selfheal import SelfHealer

    healer, home = _healer()
    for _ in range(3):
        healer.record_failure("review", RuntimeError("boom secret=s3cr3t"))
    assert healer.is_degraded("review")

    reborn = SelfHealer(home, "1.0.0", degrade_threshold=3, probe_interval=4)
    assert reborn.is_degraded("review")
    # 脱敏必须在落盘前完成，报告里不得出现原始凭据
    assert "s3cr3t" not in reborn.render()


def test_selfheal_probe_then_recover() -> None:
    """降级后周期性试探；试探成功即恢复并清除经验."""
    healer, _ = _healer(probe_interval=3)
    for _ in range(3):
        healer.record_failure("clarify")
    assert healer.is_degraded("clarify")

    verdicts = [healer.is_degraded("clarify") for _ in range(8)]
    assert False in verdicts, "降级后应周期性放行一次做试探"
    assert verdicts.count(True) >= 4, "试探不应过于频繁"

    healer.record_success("clarify")
    assert not healer.is_degraded("clarify")
    assert healer.degraded_capabilities() == []


def test_selfheal_fingerprint_invalidates_experience() -> None:
    """织入能力集合变化即视为环境已变，旧降级结论全部作废."""
    from hermes_lark_streaming.selfheal import SelfHealer

    healer, home = _healer()
    for _ in range(3):
        healer.record_failure("approval")
    assert healer.is_degraded("approval")

    upgraded = SelfHealer(home, "1.0.0", degrade_threshold=3, probe_interval=4)
    upgraded.record_weave(
        ok=True, hermes_version="0.21.0", callbacks=["cb_a"], conversation=["run_conversation"]
    )
    assert not upgraded.is_degraded("approval"), "指纹变化后旧经验必须失效"


def test_selfheal_regression_pinpoints_missing_callback() -> None:
    """升级后织入失败时，能指出相比上次成功具体少了哪个回调."""
    from hermes_lark_streaming.selfheal import SelfHealer

    _, home = _healer()
    after = SelfHealer(home, "1.0.0")
    after.record_weave(ok=False, detail="preflight 失败")

    missing, added = after.regression(["cb_a", "cb_new"])
    assert missing == ["cb_b"]
    assert added == ["cb_new"]
    # 失败不得覆盖成功基线——那份记录正是对比用的
    assert after.last_success() is not None


def test_selfheal_observation_gap() -> None:
    """装配观测：历史有、本进程无 → 呈现为疑似缺失，但不自动判定失效."""
    from hermes_lark_streaming.selfheal import SelfHealer

    healer, home = _healer()
    healer.record_observed("cb_a")
    healer.record_observed("cb_b")
    gap, session, history = healer.observation_gap()
    assert gap == [] and session == 2 and history == 2

    reborn = SelfHealer(home, "1.0.0")
    reborn.record_observed("cb_a")  # 本进程只观测到一个
    gap, session, history = reborn.observation_gap()
    assert gap == ["cb_b"] and session == 1 and history == 2


def test_selfheal_disabled_is_noop() -> None:
    """关闭自愈层后一切降级逻辑失效，且不落任何状态文件."""
    import tempfile

    from hermes_lark_streaming.selfheal import SelfHealer, state_path

    home = Path(tempfile.mkdtemp())
    healer = SelfHealer(home, "1.0.0", enabled=False, degrade_threshold=1)
    healer.record_weave(ok=True, callbacks=["cb_a"], conversation=["c"])
    for _ in range(10):
        healer.record_failure("notice", "boom")
    assert not healer.is_degraded("notice")
    assert healer.degraded_capabilities() == []
    assert not state_path(home).exists(), "关闭时不得写状态文件"
    assert "已关闭" in healer.render()


def test_selfheal_tolerates_corrupt_state_file() -> None:
    """状态文件被手改坏、或跨版本残留，一律退回空白经验而非崩溃."""
    import tempfile

    from hermes_lark_streaming.selfheal import SelfHealer, state_path

    home = Path(tempfile.mkdtemp())
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)

    for content in ('{"schema": 999, "capabilities": {"notice": {"degraded_at": 1}}}', "{ 不是合法 JSON", ""):
        path.write_text(content, encoding="utf-8")
        healer = SelfHealer(home, "1.0.0")
        assert not healer.is_degraded("notice")
        assert healer.degraded_capabilities() == []

    # 字段类型不可信：streak 是字符串、capabilities 是列表
    path.write_text('{"schema": 1, "capabilities": [], "totals": "x"}', encoding="utf-8")
    assert SelfHealer(home, "1.0.0").snapshot()["totals"]["degrades"] == 0


def test_descriptor_selftest_does_not_pollute_observation() -> None:
    """描述符机制自测用的探针赋值不得被记为真实装配.

    这是实际踩到的坑：``preflight`` 里的 ``_verify_descriptor_mechanics`` 会在
    探针类上赋值一次，若上报给自愈层，观测集合会混入 ``probe_cb`` 这个并不存在
    的回调名，让「历史装配过哪些回调」的基线彻底失去意义。
    """
    from hermes_lark_streaming.bridge.weave import _verify_descriptor_mechanics, set_observer

    seen: list[str] = []
    set_observer(seen.append)
    try:
        _verify_descriptor_mechanics()
    finally:
        set_observer(None)
    assert seen == [], f"自测探针不应上报装配观测，实际上报了 {seen}"


def test_bootstrap_diagnosis_pinpoints_missing_callback() -> None:
    """织入失败时报告应带上与历史基线的精确对照，而不只是一句 FAILED."""
    import tempfile

    from hermes_lark_streaming.bridge.plugin import BootstrapReport, _record_experience
    from hermes_lark_streaming.orchestrator import Orchestrator

    orch = Orchestrator(Path(tempfile.mkdtemp()))

    success = BootstrapReport()
    success.ok = True
    success.callbacks = ["cb_a", "cb_b", "cb_c"]
    success.conversation = ["run_conversation"]
    _record_experience(success, orch)

    # 模拟升级后 cb_b 不可织入，只织上了另外两个
    failed = BootstrapReport()
    failed.detail = "属性 cb_b 已被 property 占用，不能织入"
    failed.callbacks = ["cb_a", "cb_c"]
    _record_experience(failed, orch)

    assert "上次成功织入 3 个回调" in failed.diagnosis
    assert "cb_b" in failed.diagnosis
    rendered = failed.render()
    assert "FAILED" in rendered and "历史对照" in rendered


def test_circuit_breaker_trip_skips_counting() -> None:
    """``trip()`` 直接打开熔断，供「已独立判定为系统性故障」的场景使用."""
    from hermes_lark_streaming.transport import CircuitBreaker

    breaker = CircuitBreaker(threshold=5)
    breaker.record_failure()
    assert not breaker.is_open, "单次失败远未达阈值"

    assert breaker.trip() is True
    assert breaker.is_open
    assert breaker.trip() is False, "已打开时重复 trip 不应再次上报"

    breaker.reset()
    assert not breaker.is_open


def test_systemic_failure_escalates_to_global_bypass() -> None:
    """单类失败只降级该类；失败跨越多类才升级为全局熔断.

    这条边界很关键：一类回调语义变化不该让整个插件失效，但凭据失效 / 网络
    不可达会让所有类同时失败，那种情况必须全局退回原生。
    """
    import tempfile

    from hermes_lark_streaming.orchestrator import SYSTEMIC_DEGRADE_COUNT, Orchestrator

    orch = Orchestrator(Path(tempfile.mkdtemp()))
    threshold = orch.config.degrade_after_failures

    # 第一类失败到降级：只关掉它，不动全局
    for _ in range(threshold):
        orch.record_capture_failure("notice", "boom")
    assert not orch.bypassed, "单类降级不应触发全局熔断"
    assert not orch.capture_active("notice")
    assert orch.capture_active("review"), "其余能力必须照常工作"

    # 逐类累加，直到跨越系统性阈值
    for capability in ("review", "clarify", "approval")[: SYSTEMIC_DEGRADE_COUNT - 1]:
        for _ in range(threshold):
            orch.record_capture_failure(capability, "boom")

    degraded = orch.healer.degraded_capabilities()
    assert len(degraded) >= SYSTEMIC_DEGRADE_COUNT
    assert orch.bypassed, "失败跨越多类应判定为系统性故障并全局熔断"


def test_capture_active_respects_explicit_user_config() -> None:
    """用户显式开启的能力，即使学到它总失败也必须继续尝试."""
    import tempfile

    from hermes_lark_streaming.orchestrator import Orchestrator

    home = Path(tempfile.mkdtemp())
    (home / "config.yaml").write_text(
        "streaming:\n  capture:\n    notice: true\n    review: false\n", encoding="utf-8"
    )
    orch = Orchestrator(home)
    for _ in range(orch.config.degrade_after_failures * 3):
        orch.record_capture_failure("notice", "boom")

    assert orch.healer.is_degraded("notice"), "经验仍应如实记录失败"
    assert orch.capture_active("notice"), "但用户显式 true 必须压过学习经验"
    assert not orch.capture_active("review"), "用户显式 false 一律不收纳"
    assert orch.capture_active("clarify"), "未配置且未降级 → 默认收纳"


def test_capture_explicit_distinguishes_unset_from_true() -> None:
    """显式 true 与未配置必须可区分——这是「用户配置永远赢」的实现基础."""
    import tempfile

    from hermes_lark_streaming.config import Config

    home = Path(tempfile.mkdtemp())
    config = home / "config.yaml"
    config.write_text("streaming:\n  capture:\n    notice: true\n    review: false\n", encoding="utf-8")

    cfg = Config(home)
    assert cfg.capture_explicit("notice") is True
    assert cfg.capture_explicit("review") is False
    assert cfg.capture_explicit("clarify") is None, "未配置必须返回 None 而非 False"
    assert cfg.capture_enabled("clarify") is True, "未配置时默认开启"

    # 无法解析的值按未配置处理，从而服从学习到的经验
    config.write_text("streaming:\n  capture:\n    notice: maybe\n", encoding="utf-8")
    assert Config(home).capture_explicit("notice") is None


def test_selfheal_config_defaults() -> None:
    import tempfile

    from hermes_lark_streaming.config import Config

    cfg = Config(Path(tempfile.mkdtemp()))
    assert cfg.selfheal_enabled is True
    assert cfg.degrade_after_failures == 3
    assert cfg.selfheal_probe_interval == 20
    # 精准降级的代价远小于全局熔断，因此必须更早触发
    assert cfg.degrade_after_failures < cfg.bypass_after_failures



def test_registry_defers_active_turn_for_finalize() -> None:
    """回收活跃 turn 必须交还调用方收卡，不能静默丢弃.

    这是 HLS 线上故障的形状：它按 created_at 剪枝后直接丢掉 turn，卡片
    永远停在 loading。本项守住「摘除即入队」这条不变式。
    """
    import tempfile

    from hermes_lark_streaming.config import Config
    from hermes_lark_streaming.core.registry import TurnRegistry
    from hermes_lark_streaming.core.turn import REASON_EVICTED, REASON_EXPIRED, TurnState

    home = Path(tempfile.mkdtemp())
    (home / "config.yaml").write_text(
        "streaming:\n  enabled: true\n  limits:\n    max_turns: 8\n    turn_ttl_sec: 600\n",
        encoding="utf-8",
    )
    cfg = Config(home)
    assert cfg.max_turns == 8  # 配置下限就是 8，不能再小

    registry = TurnRegistry(cfg)
    created = []
    for index in range(cfg.max_turns):
        turn = registry.create(turn_key=f"t{index}", message_id=f"m{index}", chat_id=f"c{index}")
        assert turn is not None
        turn.state = TurnState.STREAMING
        created.append(turn)
    assert registry.take_pending_finalize() == []

    # 再加一个触发 LRU 淘汰，最旧的仍活跃 → 必须入队
    registry.create(turn_key="overflow", message_id="mo", chat_id="co")
    pending = registry.take_pending_finalize()
    assert [(t.turn_key, reason) for t, reason in pending] == [("t0", REASON_EVICTED)]
    # 取走即清空，不重复交付
    assert registry.take_pending_finalize() == []

    # 已终态的被淘汰者不入队（卡片已经收好了）
    created[1].state = TurnState.COMPLETED
    registry.create(turn_key="overflow2", message_id="mo2", chat_id="co2")
    assert registry.take_pending_finalize() == []

    # TTL 回收走同一条路径。updated_at 拨到过去模拟长期无更新
    survivor = registry.get("t2")
    assert survivor is not None
    survivor.state = TurnState.WAITING  # 审批中：空闲守护刻意不管这一档
    survivor.updated_at -= cfg.turn_ttl_sec + 1
    registry.prune()
    pending = registry.take_pending_finalize()
    assert [(t.turn_key, reason) for t, reason in pending] == [("t2", REASON_EXPIRED)]


def test_pending_finalize_queue_is_bounded() -> None:
    """防泄漏的机制自己不能变成泄漏点."""
    import tempfile

    from hermes_lark_streaming.config import Config
    from hermes_lark_streaming.core.registry import PENDING_FINALIZE_LIMIT, TurnRegistry
    from hermes_lark_streaming.core.turn import REASON_EVICTED, Turn

    registry = TurnRegistry(Config(Path(tempfile.mkdtemp())))
    for index in range(PENDING_FINALIZE_LIMIT + 10):
        turn = Turn(turn_key=f"t{index}", message_id=f"m{index}", chat_id="c")
        registry._defer_finalize_locked(turn, REASON_EVICTED)

    pending = registry.take_pending_finalize()
    assert len(pending) == PENDING_FINALIZE_LIMIT
    # 丢的是最旧的，保留最新的——新卡片更可能还在用户视野里
    assert pending[-1][0].turn_key == f"t{PENDING_FINALIZE_LIMIT + 9}"


def test_timeout_finalize_is_not_rendered_as_success() -> None:
    """超时收尾必须与「已完成」区分开.

    空闲守护过去走 complete_turn，卡片显示 ✅ 已完成——而真相是一直没动静。
    用户看到 ✅ 会以为任务成功，这比不收卡更误导。
    """
    import tempfile

    from hermes_lark_streaming.config import Config
    from hermes_lark_streaming.core.turn import (
        REASON_EVICTED,
        REASON_EXPIRED,
        REASON_INTERRUPTED,
        REASON_STOPPED,
        REASON_TIMEOUT,
        TIMEOUT_REASONS,
        Turn,
        TurnState,
    )
    from hermes_lark_streaming.render import card as card_mod
    from hermes_lark_streaming.render import elements

    cfg = Config(Path(tempfile.mkdtemp()))

    # 三种超时原因共用一档呈现，具体原因由卡内 notice 区分
    assert TIMEOUT_REASONS == {REASON_TIMEOUT, REASON_EXPIRED, REASON_EVICTED}
    assert REASON_STOPPED not in TIMEOUT_REASONS
    assert REASON_INTERRUPTED not in TIMEOUT_REASONS

    for reason in TIMEOUT_REASONS:
        turn = Turn(turn_key="t", message_id="m", chat_id="c")
        turn.state = TurnState.ABORTED
        turn.abort_reason = reason
        summary = turn.summary_text(cfg)
        assert "超时" in summary, reason
        assert "✅" not in summary, reason

        zh, _ = elements._footer_field("status", {}, False, True, False, reason)
        assert zh is not None and "超时" in zh, reason

    # header 用橙色：任务可能还在跑，不是失败
    assert elements._HEADER_STYLE["timeout"] == ("orange", "status_timeout")
    built = card_mod.build_complete_card(
        segments=[],
        all_tool_steps=[],
        footer_data={},
        footer_fields=[["status"]],
        footer_show_label=False,
        footer_enabled=True,
        footer_text_size="notation",
        body_text_size="normal_v2",
        panel_expanded=False,
        show_tool_use=True,
        header_enabled=True,
        width_mode="default",
        summary="",
        is_error=False,
        is_aborted=True,
        abort_reason=REASON_TIMEOUT,
    )
    assert built["header"]["template"] == "orange"

    # 用户主动 /stop 仍是红色「已停止」，语义没被这次改动污染
    stopped = card_mod.build_complete_card(
        segments=[],
        all_tool_steps=[],
        footer_data={},
        footer_fields=[["status"]],
        footer_show_label=False,
        footer_enabled=True,
        footer_text_size="notation",
        body_text_size="normal_v2",
        panel_expanded=False,
        show_tool_use=True,
        header_enabled=True,
        width_mode="default",
        summary="",
        is_error=False,
        is_aborted=True,
        abort_reason=REASON_STOPPED,
    )
    assert stopped["header"]["template"] == "red"


def test_timeout_notice_explains_task_may_still_run() -> None:
    """说明文案必须区分「卡片停止跟踪」与「任务失败」."""
    import tempfile

    from hermes_lark_streaming.core.turn import REASON_EVICTED, REASON_EXPIRED, REASON_TIMEOUT
    from hermes_lark_streaming.orchestrator import Orchestrator

    orch = Orchestrator(Path(tempfile.mkdtemp()))
    for reason in (REASON_TIMEOUT, REASON_EXPIRED, REASON_EVICTED):
        text = orch._timeout_notice(reason)
        assert "⏱️" in text
        assert "失败" not in text
    assert "不受影响" in orch._timeout_notice(REASON_EVICTED)


def _sweep_once(orch: object, *, running_tool: bool, idle_for: float) -> list[tuple[str, str]]:
    """跑一轮空闲守护扫描，返回它决定收卡的 turn 列表.

    替掉 ``abort_turn`` 而不是给它准备一个假 client：这里要验的是**守护的判定**，
    收卡本身另有测试覆盖。
    """
    import asyncio
    import time
    from typing import Any

    from hermes_lark_streaming.core.turn import TurnState
    from hermes_lark_streaming.orchestrator import _IdleWatcher

    async def scenario() -> list[tuple[str, str]]:
        turn = orch.registry.create(turn_key="t", message_id="m", chat_id="c")  # type: ignore[attr-defined]
        assert turn is not None
        turn.bind_card(card_id="c1", card_msg_id="m1")
        turn.transition(TurnState.STREAMING)
        turn.add_tool_start("bash", "make build")
        if not running_tool:
            turn.add_tool_end("bash", output="done")
        turn.updated_at = time.time() - idle_for

        called: list[tuple[str, str]] = []

        async def fake_abort(turn_key: str, **kwargs: Any) -> None:
            called.append((turn_key, str(kwargs.get("reason") or "")))

        orch.abort_turn = fake_abort  # type: ignore[attr-defined]
        await _IdleWatcher(orch)._sweep()  # type: ignore[arg-type]
        return called

    return asyncio.run(scenario())


def test_idle_watcher_skips_turn_with_running_tool() -> None:
    """工具还在执行时不得判超时——那是在干活，不是卡死.

    工具执行期间 Hermes 不产生任何回调，``updated_at`` 一动不动，跟真卡死在时间
    维度上完全同形。不看一眼工具状态，一次几分钟的编译或测试就会被判成超时，
    卡片提前定格成「⏱️ 已超时收尾」而任务其实还在跑。
    """
    import tempfile

    from hermes_lark_streaming.orchestrator import Orchestrator

    # 工具仍在跑：远超阈值也不收卡
    assert _sweep_once(Orchestrator(Path(tempfile.mkdtemp())), running_tool=True, idle_for=100) == []

    # 工具已结束：超过阈值照常收卡，且原因是超时而非完成
    finalized = _sweep_once(Orchestrator(Path(tempfile.mkdtemp())), running_tool=False, idle_for=100)
    assert [key for key, _ in finalized] == ["t"]
    assert finalized[0][1] == "timeout"

    # 没到阈值就不该动
    assert _sweep_once(Orchestrator(Path(tempfile.mkdtemp())), running_tool=False, idle_for=50) == []


def test_idle_finalize_sec_is_configurable() -> None:
    """空闲阈值可配，且卡内说明文案必须跟着走.

    文案里写死一个数字，用户改了配置就会看到与实际不符的说明——那比不写数字
    更糟，因为它看起来是确切的。
    """
    import tempfile
    import textwrap

    from hermes_lark_streaming.config import DEFAULT_IDLE_FINALIZE_SEC, Config
    from hermes_lark_streaming.core.turn import REASON_TIMEOUT
    from hermes_lark_streaming.orchestrator import Orchestrator

    def home_with(value: object) -> Path:
        home = Path(tempfile.mkdtemp())
        (home / "config.yaml").write_text(
            textwrap.dedent(f"""
                streaming:
                  enabled: true
                  limits:
                    idle_finalize_sec: {value}
            """),
            encoding="utf-8",
        )
        return home

    # 未配置时用默认值
    assert Config(Path(tempfile.mkdtemp())).idle_finalize_sec == DEFAULT_IDLE_FINALIZE_SEC

    # 配置生效，且守护按新阈值判定
    orch = Orchestrator(home_with(30))
    assert orch.config.idle_finalize_sec == 30
    assert [key for key, _ in _sweep_once(orch, running_tool=False, idle_for=50)] == ["t"]
    # 说明文案跟着配置走，不是写死的 90
    assert "30 秒" in orch._timeout_notice(REASON_TIMEOUT)

    # 低于下限夹取；坏值回退默认。配置损坏绝不能让守护失灵
    assert Config(home_with(5)).idle_finalize_sec == 15
    assert Config(home_with("坏值")).idle_finalize_sec == DEFAULT_IDLE_FINALIZE_SEC


def test_request_timeout_is_configurable() -> None:
    """飞书 API 单次请求超时可配，且默认值与 SDK 自己的默认对齐.

    lark-oapi 的默认是 30 秒（``core/model/config.py`` 里
    ``timeout: Optional[float] = 30``），本插件的默认必须与它一致——配置缺失时
    行为要与不带这个特性时逐字相同。两处默认值漂移是这里真正要守的东西。

    SDK 把 timeout 同时传给同步（requests）与异步（httpx）两条路径，所以设一次
    就覆盖本插件的全部调用，不需要逐个方法单独设。
    """
    import tempfile
    import textwrap

    from hermes_lark_streaming.config import DEFAULT_REQUEST_TIMEOUT_SEC, Config
    from hermes_lark_streaming.transport.client import ClientConfig

    def home_with(value: object) -> Path:
        home = Path(tempfile.mkdtemp())
        (home / "config.yaml").write_text(
            textwrap.dedent(f"""
                streaming:
                  enabled: true
                  resilience:
                    request_timeout_sec: {value}
            """),
            encoding="utf-8",
        )
        return home

    # 传输层的默认值不得与配置层漂移
    assert ClientConfig(app_id="cli_x", app_secret="s").timeout_sec == float(DEFAULT_REQUEST_TIMEOUT_SEC)
    # 显式值原样带到传输层
    assert ClientConfig(app_id="cli_x", app_secret="s", timeout_sec=7.0).timeout_sec == 7.0

    assert Config(Path(tempfile.mkdtemp())).request_timeout_sec == DEFAULT_REQUEST_TIMEOUT_SEC
    assert Config(home_with(8)).request_timeout_sec == 8
    # 夹到下限：再小连一次 TLS 握手加往返都未必够，只会把正常请求判成失败
    assert Config(home_with(1)).request_timeout_sec == 3
    assert Config(home_with(9999)).request_timeout_sec == 300
    assert Config(home_with("坏值")).request_timeout_sec == DEFAULT_REQUEST_TIMEOUT_SEC


def test_image_download_blocks_internal_networks() -> None:
    """待上传的图片地址来自模型输出，必须挡住内网与环回.

    图片 URL 是从模型回答的 markdown 里抓出来的，属于不可信输入：一次 prompt
    injection（模型读了恶意网页或文件）就能让 gateway 去请求任意地址，而它跑在
    用户自己的机器上、看得到内网和云元数据服务。

    本测试刻意只用 IP 字面量和 localhost，不用域名——``getaddrinfo`` 对 IP 不查
    DNS，测试因此完全离线可跑。
    """
    from hermes_lark_streaming.transport.client import (
        ClientConfig,
        _blocked_ip,
        _blocked_url,
        _GuardedRedirectHandler,
    )

    # 环回、私有、链路本地（含云元数据 169.254.169.254）、未指定、多播、
    # RFC 6598 运营商级 NAT，以及 IPv6 对应形态
    for text in (
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "根本不是 IP",
    ):
        assert _blocked_ip(text), text

    # 公网地址必须放行，否则等于关掉图片功能
    for text in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"):
        assert not _blocked_ip(text), text

    for url in (
        "http://127.0.0.1:8080/a.png",
        "http://localhost/a.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/a.png",
        "http://192.168.1.1/logo.png",
        "https:///a.png",  # 无主机
    ):
        assert _blocked_url(url), url

    assert not _blocked_url("http://8.8.8.8/a.png")

    # 重定向的每一跳都要重新校验：公网 URL 不能 302 把我们带进内网。
    # 返回 None 表示不跟随，urllib 随后抛 HTTPError 走「这张图不上传」的降级
    handler = _GuardedRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", None, "http://127.0.0.1:9/x.png") is None
    assert _GuardedRedirectHandler.max_redirections <= 5

    # 默认必须是关闭的：安全默认不能靠用户去配
    assert ClientConfig(app_id="x", app_secret="y").allow_private_image_hosts is False


def test_finalized_card_summary_shows_terminal_state() -> None:
    """终态卡片的会话列表摘要必须是终态文案，不能停在「正在写」.

    收卡时 ``turn.state`` 还停在 FINALIZING（真终态要等 update_card 成功才敢
    落定），渲染 summary 时若不带上目标终态，已完成的任务在会话列表里就显示成
    「✍️ 正在写」——正是 summary 这个机制本身要治理的问题。
    """
    import asyncio
    import tempfile
    from typing import Any

    from hermes_lark_streaming.core.turn import Delivery, Turn, TurnState
    from hermes_lark_streaming.orchestrator import Orchestrator

    class _RecordingClient:
        """只记录整卡替换内容的替身，不碰网络."""

        def __init__(self) -> None:
            self.cards: list[dict[str, Any]] = []

        async def close_streaming(self, card_id: str, *, sequence: int) -> None:
            return None

        async def update_card(self, card_id: str, card: dict[str, Any], *, sequence: int) -> None:
            self.cards.append(card)

    orch = Orchestrator(Path(tempfile.mkdtemp()))
    client = _RecordingClient()
    orch._client = client  # type: ignore[assignment]

    turn = Turn(turn_key="t", message_id="m", chat_id="c")
    turn.bind_card(card_id="card-1", card_msg_id="msg-1")
    turn.transition(TurnState.STREAMING)
    turn.add_answer("最终回答")
    # 与 complete_turn 的成功路径一致：先进 FINALIZING，收卡成功后才落定终态
    turn.transition(TurnState.FINALIZING)

    assert asyncio.run(orch._finalize(turn, is_error=False, is_aborted=False)) is Delivery.TAKEN
    assert turn.state is TurnState.COMPLETED

    card = client.cards[-1]
    summary = card["config"]["summary"]["content"]
    assert summary.startswith("✅"), summary
    assert "✍️" not in summary
    assert card["header"]["template"] == "green"


def test_finalize_detached_skips_terminal_turn() -> None:
    """入队与取出之间已被正常收卡的 turn，不得再改一遍卡片."""
    import asyncio
    import tempfile

    from hermes_lark_streaming.core.turn import Delivery, REASON_EVICTED, Turn, TurnState
    from hermes_lark_streaming.orchestrator import Orchestrator

    orch = Orchestrator(Path(tempfile.mkdtemp()))

    done = Turn(turn_key="t1", message_id="m1", chat_id="c1")
    done.state = TurnState.COMPLETED
    assert asyncio.run(orch.finalize_detached(done, reason=REASON_EVICTED)) is Delivery.DECLINED

    # 没建成卡片的同样无需收尾
    cardless = Turn(turn_key="t2", message_id="m2", chat_id="c2")
    cardless.state = TurnState.STREAMING
    assert asyncio.run(orch.finalize_detached(cardless, reason=REASON_EVICTED)) is Delivery.DECLINED


def test_drain_pending_finalize_clears_queue_when_disabled() -> None:
    """插件未启用时也要清空队列，否则它会一直持有 Turn 引用."""
    import tempfile

    from hermes_lark_streaming.core.turn import REASON_EVICTED, Turn
    from hermes_lark_streaming.orchestrator import Orchestrator

    orch = Orchestrator(Path(tempfile.mkdtemp()))
    assert orch.enabled is False  # 无配置无凭据

    turn = Turn(turn_key="t1", message_id="m1", chat_id="c1")
    orch.registry._defer_finalize_locked(turn, REASON_EVICTED)
    assert orch.drain_pending_finalize() == 0
    # 关键：队列已被取空，不是攒着
    assert orch.registry.take_pending_finalize() == []


def test_drain_pending_finalize_schedules_one_coro_per_turn() -> None:
    """淘汰 → 收卡这条链路必须真的接通，不能只入队没人取.

    环境无关：用子类强制 enabled，避免依赖本机是否配了飞书凭据。
    """
    import tempfile

    from hermes_lark_streaming.core.turn import REASON_EVICTED, REASON_EXPIRED, Turn, TurnState
    from hermes_lark_streaming.orchestrator import Orchestrator

    class AlwaysOn(Orchestrator):
        @property
        def enabled(self) -> bool:
            return True

    orch = AlwaysOn(Path(tempfile.mkdtemp()))
    scheduled: list[str] = []

    def fake_spawn(coro: object) -> None:
        scheduled.append(getattr(coro, "__qualname__", type(coro).__name__))
        close = getattr(coro, "close", None)
        if callable(close):
            close()  # 不真正执行，避免测试触发飞书调用

    orch._spawn = fake_spawn  # type: ignore[method-assign]

    for index, reason in enumerate((REASON_EVICTED, REASON_EXPIRED)):
        turn = Turn(turn_key=f"t{index}", message_id=f"m{index}", chat_id="c")
        turn.state = TurnState.STREAMING
        orch.registry._defer_finalize_locked(turn, reason)

    assert orch.drain_pending_finalize() == 2
    assert len(scheduled) == 2, "每个待收卡的 turn 都要有一次调度"
    assert all("finalize_detached" in name for name in scheduled), scheduled
    # 队列已清空，不会被下一轮重复收卡
    assert orch.drain_pending_finalize() == 0


def test_state_dir_name_cannot_shadow_the_package() -> None:
    """状态目录名必须不是合法 Python 标识符.

    这条断言守的是一个曾让插件在真实 gateway 里**完全无法加载**的缺陷：

    状态目录原名 ``hermes_lark_streaming``，与包名相同，位于 ``~/.hermes/``。
    而 gateway 以 ``python -m hermes_cli.main gateway run`` 启动、cwd 正是
    ``~/.hermes``，``python -m`` 会把 cwd 放进 ``sys.path[0]``。于是这个只装着
    两个 JSON 的目录被 Python 当成**命名空间包**，抢在 editable finder 之前
    遮蔽了真正的包，Hermes 报：

        cannot import name '__version__' from 'hermes_lark_streaming'
        (unknown location)

    连字符不能出现在 Python 标识符里，改名后彻底免疫。任何把目录名改回
    下划线形式的改动都会让这条测试失败。
    """
    from hermes_lark_streaming.selfheal.store import _LEGACY_STATE_DIRNAME, _STATE_DIRNAME, state_dir

    assert not _STATE_DIRNAME.isidentifier(), (
        f"状态目录名 {_STATE_DIRNAME!r} 是合法 Python 标识符，"
        "会在 gateway 的 cwd 下遮蔽同名包，导致插件永远加载不上"
    )
    # 旧名字确实是标识符——这正是当初出问题的原因，留作对照
    assert _LEGACY_STATE_DIRNAME.isidentifier()
    assert _STATE_DIRNAME != _LEGACY_STATE_DIRNAME

    # 包名与目录名必须不同，否则换了名字也白搭
    import hermes_lark_streaming

    assert _STATE_DIRNAME != hermes_lark_streaming.__name__
    assert state_dir(Path("/tmp/x")).name == _STATE_DIRNAME


def test_legacy_state_dir_is_migrated() -> None:
    """旧状态目录要能一次性迁到新名字，且不覆盖已有新数据."""
    import json
    import tempfile

    from hermes_lark_streaming.selfheal.store import (
        _LEGACY_STATE_DIRNAME,
        migrate_legacy_dir,
        state_dir,
    )

    home = Path(tempfile.mkdtemp())
    legacy = home / _LEGACY_STATE_DIRNAME
    legacy.mkdir()
    (legacy / "state.json").write_text(json.dumps({"schema": 1, "marker": "old"}), encoding="utf-8")
    (legacy / "activity.json").write_text(json.dumps({"pid": 1}), encoding="utf-8")

    assert migrate_legacy_dir(home) is True
    target = state_dir(home)
    assert json.loads((target / "state.json").read_text(encoding="utf-8"))["marker"] == "old"
    assert (target / "activity.json").is_file()
    # 搬空后旧目录应被清理，不留下能遮蔽包的空目录
    assert not legacy.exists(), "旧目录必须删除——空目录同样会被当作命名空间包"

    # 幂等：再迁一次不报错也不做事
    assert migrate_legacy_dir(home) is False

    # 已有新数据时不被旧数据覆盖
    home2 = Path(tempfile.mkdtemp())
    legacy2 = home2 / _LEGACY_STATE_DIRNAME
    legacy2.mkdir()
    (legacy2 / "state.json").write_text(json.dumps({"marker": "old"}), encoding="utf-8")
    target2 = state_dir(home2)
    target2.mkdir(parents=True)
    (target2 / "state.json").write_text(json.dumps({"marker": "new"}), encoding="utf-8")
    migrate_legacy_dir(home2)
    assert json.loads((target2 / "state.json").read_text(encoding="utf-8"))["marker"] == "new"


def test_no_module_level_gateway_run_import() -> None:
    """本插件任何模块都不得在**模块顶层** import ``gateway.run``.

    ``gateway/run.py`` 的模块体里调用了 ``get_plugin_auxiliary_tasks()``，
    它会触发 Hermes 的插件发现。若在模块顶层 import 它，就会形成闭环：

        Hermes 加载本插件 → import 本插件模块 → import gateway.run
          → gateway.run 模块体触发插件发现 → 再次加载本插件
            → 本插件模块仍在初始化中，register() 尚未定义
              → Hermes 报 "has no register() function" 并记一条加载失败

    这个缺陷只在「pip 安装 + 由 Hermes 插件加载器加载」的真实形态下出现，
    直接调 bootstrap() 的测试路径构不成闭环——所以必须靠这条静态检查守住，
    它不依赖 Hermes 环境，任何机器上都能跑。
    """
    import ast

    forbidden = "gateway.run"
    root = Path(__file__).resolve().parent.parent / "hermes_lark_streaming"
    offenders: list[str] = []

    def scan(nodes: list[ast.stmt], path: Path) -> None:
        """只扫模块级语句；函数/类体内的 import 是惰性的，不受此限。"""
        for node in nodes:
            if isinstance(node, ast.ImportFrom):
                if node.module and (node.module == forbidden or node.module.startswith(forbidden + ".")):
                    offenders.append(f"{path.name}:{node.lineno} from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == forbidden or alias.name.startswith(forbidden + "."):
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.Try):
                # try/except 包裹仍然是模块级执行，同样有闭环风险
                scan(node.body, path)
                scan(node.orelse, path)
                scan(node.finalbody, path)
                for handler in node.handlers:
                    scan(handler.body, path)
            elif isinstance(node, ast.If):
                scan(node.body, path)
                scan(node.orelse, path)

    files = sorted(root.rglob("*.py"))
    assert files, "未找到任何模块，测试自身失效"
    for path in files:
        scan(ast.parse(path.read_text(encoding="utf-8"), str(path)).body, path)

    assert not offenders, "模块顶层 import gateway.run 会导致 Hermes 插件加载递归：\n  " + "\n  ".join(
        offenders
    )


def test_lifecycle_phrases_are_lazy_and_correct() -> None:
    """惰性借常量后，识别能力不得退化；降级判定不能靠比对短语值.

    **本测试有环境副作用需要清理**：在真实安装的 Hermes venv 里，探测常量会
    ``import gateway.run``，而它的模块体会触发 Hermes 的插件发现，从而真实执行
    一次 ``register()`` 织入。若不回滚，后续断言「未织入」的测试会被污染——
    这正是本文件末尾 finally 段存在的原因。
    """
    from hermes_lark_streaming.events import normalize as N

    try:
        # 短语表在被调用前应保持未探测状态（模块 import 不触发 gateway.run）
        fresh = N._lifecycle_phrases is None

        assert N.is_lifecycle_notice("⚠️ Gateway shutting down — Your current task will be interrupted.")
        assert N.is_lifecycle_notice("Gateway restarting now")
        assert not N.is_lifecycle_notice("↪ Redirected current run (iteration 1/90).")
        assert not N.is_lifecycle_notice("")

        # 探测过后必有结果，且带缓存（第二次不重新 import）
        assert N._lifecycle_phrases is not None
        first = N._lifecycle_phrases
        N.is_lifecycle_notice("x")
        assert N._lifecycle_phrases is first

        # Hermes 常量值恰好与内置兜底逐字相同，所以「是否降级」只能由标志位判定，
        # 比对短语值必然得出错误结论——这条断言锁死这个判据
        assert N._FALLBACK_LIFECYCLE_PHRASES == ("Gateway shutting down", "Gateway restarting")
        borrowed = N.lifecycle_constants_borrowed()
        assert isinstance(borrowed, bool)
        if borrowed:
            assert N._lifecycle_phrases  # 借到了就必须有值
        if fresh:
            assert N._lifecycle_phrases is not None
    finally:
        # 回滚可能被 Hermes 加载器顺带装上的织入。teardown 幂等，未织入时无副作用
        try:
            from hermes_lark_streaming.bridge.plugin import teardown

            teardown()
        except Exception:
            pass


def test_distribution_conflict_detection() -> None:
    """同名分发冲突必须能被识别——这是启用本插件时最容易踩的坑.

    本插件与参考实现 HLS 的分发名、顶层包名完全相同，只有 entry point
    指向的模块不同，因此判据必须落在 entry point 上。
    """
    from hermes_lark_streaming import __version__
    from hermes_lark_streaming.__main__ import _PLUGIN_ENTRY_TARGET, _detect_distribution_conflict

    # 与 pyproject.toml 的声明保持一致，改一处漏一处就会误判
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"{_PLUGIN_ENTRY_TARGET}"' in pyproject, "entry point 常量与 pyproject.toml 不一致"

    level, detail = _detect_distribution_conflict()
    assert level in {"ok", "warn", "absent"}
    assert detail

    # 本机装的是哪一个都不该让检测崩掉；若判为 ok 则版本必须与代码一致
    if level == "ok":
        assert __version__ in detail


def test_abort_footer_keeps_elapsed_time() -> None:
    """中断 / 超时收卡也要显示耗时，且不得覆盖正常收卡填好的 footer."""
    import tempfile

    from hermes_lark_streaming.core.turn import Turn
    from hermes_lark_streaming.orchestrator import Orchestrator

    orch = Orchestrator(Path(tempfile.mkdtemp()))

    turn = Turn(turn_key="t", message_id="m", chat_id="c")
    turn.created_at -= 12.5
    orch._ensure_duration_footer(turn)
    assert turn.footer.get("duration", 0) >= 12.5

    # 已有 footer（含 model/token 明细）时保持原样
    rich = Turn(turn_key="t2", message_id="m2", chat_id="c2")
    rich.set_footer(duration=3.0, model="gpt-5", tokens={"input_tokens": 10, "output_tokens": 20})
    orch._ensure_duration_footer(rich)
    assert rich.footer["model"] == "gpt-5"
    assert rich.footer["duration"] == 3.0


def test_config_ttl_cache_picks_up_edits() -> None:
    """改配置无需重启 gateway：TTL 到期后按文件指纹重新解析."""
    import tempfile

    from hermes_lark_streaming import config as config_mod
    from hermes_lark_streaming.config import Config

    home = Path(tempfile.mkdtemp())
    path = home / "config.yaml"
    path.write_text("streaming:\n  enabled: true\n  capture:\n    notice: true\n", encoding="utf-8")

    cfg = Config(home)
    assert cfg.capture_explicit("notice") is True

    path.write_text("streaming:\n  enabled: true\n  capture:\n    notice: false\n", encoding="utf-8")
    # TTL 未到期：仍是缓存值（这正是热路径不付 IO 代价的原因）
    assert cfg.capture_explicit("notice") is True

    # 把「上次加载时刻」拨到过去，模拟 TTL 到期
    cfg._loaded_at -= config_mod.CONFIG_CACHE_TTL_SEC + 1
    assert cfg.capture_explicit("notice") is False

    # 指纹未变时只续期不重解析：把缓存改脏后再读，应仍是脏值
    cfg._raw = {"streaming": {"enabled": True, "capture": {"notice": True}}}
    cfg._loaded_at -= config_mod.CONFIG_CACHE_TTL_SEC + 1
    assert cfg.capture_explicit("notice") is True

    # invalidate 强制丢弃缓存
    cfg.invalidate()
    assert cfg.capture_explicit("notice") is False


def test_config_reads_disk_only_once_within_ttl() -> None:
    """展示开关在流式回调里高频调用，TTL 内不得重复解析 yaml."""
    import tempfile

    from hermes_lark_streaming.config import Config

    class CountingConfig(Config):
        __slots__ = ("reads",)

        def __init__(self, home: Path) -> None:
            super().__init__(home)
            self.reads = 0

        def _read_disk(self) -> dict:
            self.reads += 1
            return super()._read_disk()

    home = Path(tempfile.mkdtemp())
    (home / "config.yaml").write_text(
        "display:\n  show_reasoning: true\n  show_tool_use: false\n", encoding="utf-8"
    )
    cfg = CountingConfig(home)

    for _ in range(50):
        assert cfg.show_reasoning is True
        assert cfg.show_tool_use is False
    assert cfg.reads == 1, f"TTL 内应只读盘一次，实际 {cfg.reads} 次"


def _run_all() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"  PASS  {name}")
        except Exception as error:  # noqa: BLE001 - 汇总所有失败
            failed += 1
            print(f"  FAIL  {name}: {type(error).__name__}: {error}")
    print()
    print(f"共 {len(tests)} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())

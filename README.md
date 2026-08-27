# hermes_lark_streaming

Hermes Gateway 飞书流式卡片插件 —— **单卡收敛 · CardKit v2.0 打字机 · 不改 Hermes 源码**。

本项目基于对 [hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card) 与 [hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming) 两个开源插件的完整源码调研重新设计，取前者的功能覆盖、后者的流式体验，并解决两者共有的三个结构性问题。

---

## 解决了什么

### 一、所有消息收进同一张卡片

审批、Self-improvement review、上下文压缩、工作轮状态、重试提示——这些在参考实现里都会**以独立灰色消息发出**，散落在卡片外面。

本插件把它们全部收进当前这一轮的卡片里：

```
┌─ 飞书卡片 ────────────────────────┐
│ 💭 思考了 2.3s          [折叠]    │
│ 🛠️ 工具调用 · 3 步       [折叠]    │
│ ℹ️ · 正在压缩上下文               │  ← 原本是独立消息
│ 🔐 命令执行授权 · 等待确认         │  ← 原本是独立消息
│ 🧠 记忆已更新                     │  ← 原本是独立消息
│                                   │
│ 正文回答（逐字打字机效果）...       │
│ ─────────────────────────────    │
│ ✅ 已完成 · 12.3s · 50K/200K      │
└───────────────────────────────────┘
```

一轮运行只产生一条飞书消息，卡片被后续消息顶走的概率随之大幅下降。

### 二、切走也能看出任务是否跑完

飞书会话列表显示的是卡片的 `summary` 字段。本插件把它作为一等状态通道，**随运行阶段实时更新**：

| 阶段 | 会话列表显示 |
|---|---|
| 刚开始 | `⏳ 已收到，正在启动…` |
| 执行工具 | `🛠️ 执行命令：npm test` |
| 等待确认 | `⏸️ 等待你确认命令执行` |
| 完成 | `✅ 根据日志，问题出在连接池配置…` |
| 失败 | `❌ 执行失败` |
| 中断 | `⏹️ 已停止` |

不用点进会话，在列表里就能判断每个对话跑到哪一步了。

### 三、Hermes 升级后插件不失效

参考实现都通过 AST 注入修改 `gateway/run.py`。而 `hermes update` 内部是 git reset，会把注入的代码整体抹掉，插件随之**静默失效**——不报错，只是卡片不再出现。

本插件**不修改 Hermes 的任何文件**。它利用 Python 数据描述符优先于实例 `__dict__` 的规则，在运行时拦截 Hermes 的回调装配：

```python
# Hermes 侧（gateway/run.py:5953），代码原样不动
agent.stream_delta_callback = _stream_delta_cb
#     ↑ 这一步被本插件在运行时拦截并包装
```

织入代码由 Hermes 官方插件 entry point 触发，而 entry point 位于 `site-packages`，**不在 Hermes 的 git 工作区内**，git reset 碰不到它。

因此：

- `hermes update` 之后**无需重装、无需重新注入**
- **不碰 Hermes 的任何文件**，卸载就是 `pip uninstall`
- 不与其他注入类插件抢源码，**无冲突**
- 万一 Hermes 改了内部装配方式，**启动时立刻报错**，而不是静默失效

唯一会留下的是插件自己的状态目录 `~/.hermes/hermes-lark-streaming/`，里面两个文件：`state.json`（自愈经验与织入指纹，可用 `selfheal.enabled: false` 关闭）与 `activity.json`（运行心跳，供 `activity` 命令跨进程读取当前有没有任务在跑）。两者与 Hermes 文件零交集，删掉整个目录即彻底干净。

> 目录名用连字符不是风格选择：`~/.hermes` 正是 gateway 的工作目录，而 `python -m` 会把 cwd 放进 `sys.path[0]`。若状态目录与 Python 包同名（`hermes_lark_streaming`），它会被当成命名空间包遮蔽真正的包，Hermes 报 `cannot import name '__version__' ... (unknown location)`，插件永远加载不上。连字符不是合法标识符，因此免疫。

详细论证见 [docs/03-升级韧性设计.md](docs/03-升级韧性设计.md)。

---

## 流式效果从哪来

打字机效果的三个必要条件，缺一则退化为整块跳变：

1. 卡片经 `cardkit/v1/cards` **实体化**，消息只引用 `card_id`
2. 卡片声明 `config.streaming_mode: true` 与 `streaming_config.print_frequency_ms: 15`
3. 文本更新走 `cardkit_stream_element` **单元素增量**，而非整卡 PATCH

其中第 2 条是决定性的：**打字机动画由飞书客户端本地插值渲染**，按 15 毫秒 1 字的节奏播放，与服务端推送频率完全解耦。所以服务端从容地按 100ms 节流合并，用户看到的仍是连续打字。

参考实现之一因为用的是 `im/v1/messages` 整卡 PATCH、没有启用流式模式，所以无论把节流参数调到多小，都只能得到每秒数次的整块跳变。

---

## 安装

```bash
# 1. 定位 Hermes 自己的 Python（插件必须装进 Hermes 的 venv）
HERMES_PYTHON=$(grep -oE 'exec "[^"]+"' "$(which hermes)" | sed 's/exec "//;s/"//')
HERMES_PYTHON=$(dirname "$HERMES_PYTHON")/python3
[ -x "$HERMES_PYTHON" ] || HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3

# 2. 安装
"$HERMES_PYTHON" -m pip install -e .
```

**3. 在 Hermes 配置里启用插件（这一步不能省）**

Hermes 的 pip 插件是**白名单机制**——`hermes_cli/plugins.py` 的 `_get_enabled_plugins()` 明确写着 *"Plugins are opt-in by default"*，未列入 `plugins.enabled` 的插件不会被加载，且 `plugins.disabled` 命中即跳过。漏了这一步的症状是**完全静默不工作**：日志里连本插件的名字都不会出现。

编辑 `~/.hermes/config.yaml`：

```yaml
plugins:
  enabled:
    - hermes-lark-streaming    # 必须加
  disabled: []                 # 确认这里没有 hermes-lark-streaming
```

```bash
# 4. 验证（会检查白名单、凭据、织入前提、旧插件冲突）
"$HERMES_PYTHON" -m hermes_lark_streaming status
"$HERMES_PYTHON" -m hermes_lark_streaming verify

# 5. 重启 gateway
hermes gateway restart
```

**没有 install / uninstall 命令**。运行时织入不需要安装步骤：`pip install` 即生效，`pip uninstall` 即停止一切干预（Hermes 文件从未被改动）。

> ⚠️ **与参考实现 HLS 同名**。两者的分发名（`hermes-lark-streaming`）与顶层包名
> （`hermes_lark_streaming`）完全相同，只有插件 entry point 指向的模块不同：本插件
> 是 `hermes_lark_streaming.bridge.plugin`，HLS 是顶层包。因此 `pip install -e .`
> 会**直接替换**已装的 HLS（想留着就先备份）。`status` 与 `doctor` 会读已安装分发
> 的 entry point 精确报出「venv 里现在装的是哪一个」——这一层用扫源码 marker 是查
> 不出来的：旧插件可能已经清掉注入痕迹（或被 `hermes update` 的 git reset 抹掉），
> 但包和 entry point 还在，Hermes 照样会调它的 `register()`。
>
> 若装的是会注入源码的插件，还请先执行它的 `uninstall` 清掉 marker，否则两者会同时
> 接管、产生重复卡片。

---

## 配置

在 `~/.hermes/config.yaml`：

```yaml
streaming:
  enabled: true              # 总开关
  width_mode: default        # default / compact / fill
  header:
    enabled: true            # 卡片顶部状态条（默认开启）
  body:
    text_size: normal_v2
  footer:
    enabled: true
    fields: [[status, elapsed, context, model]]
    show_label: false
  panel_expanded: false      # 完成态是否展开思考/工具面板

  summary:                   # 会话列表状态摘要
    enabled: true
    max_chars: 60

  capture:                   # 游离消息收纳（可逐项关闭以便排障）
    notice: true             # 状态提示、压缩、重试、工作轮
    review: true             # 自我改进 / 记忆更新
    clarify: true            # 澄清提问
    approval: true           # 命令授权
    subagent: true           # 委派出去的子任务（启动与终态）

  limits:
    max_turns: 256
    turn_ttl_sec: 600
    element_threshold: 180   # 卡片元素预算，接近飞书 200 上限时自动拆卡

  resilience:
    bypass_after_failures: 5 # 连续收纳失败达此值即熔断，全面退回原生

  selfheal:                  # 自愈：精准降级 + 经验继承
    enabled: true
    degrade_after_failures: 3 # 单类连续失败达此值即**只降级该类**
    probe_interval: 20        # 降级后每被拦截多少次试探一次恢复

  usage:                     # 订阅额度（默认关闭：要打服务商外部 API）
    enabled: false
    ttl_sec: 300             # 查询结果缓存时长，额度是慢变量
```

开启额度后还需把 `usage` 加进 `footer.fields` 才会显示，例如
`fields: [[status, elapsed, context, model], [usage]]`。Hermes 只为
openai-codex / anthropic / openrouter 实现了额度接口，其余服务商（deepseek、
本地模型等）查不到数据，此时该字段自动消失而不显示占位符。

**改配置多数不必重启 gateway**：配置走 5 秒 TTL 缓存，改完最长 5 秒生效——
`capture.*` 收纳开关、`native_chats`、`chat_bindings`、`footer.*`、`summary.*`、
`limits.*`、展示开关都在此列。缓存不是可省的：推理流式每秒会读几十次展示开关，
那条路径在 Agent worker 线程上同步执行，每次都解析 YAML 会直接拖慢打字机。

三类例外仍需 `hermes gateway restart`，因为它们是**进程构造期**读取的：

| 配置项 | 为什么 |
|---|---|
| `feishu.app_id` / `app_secret` / `bots` | 飞书 client 首次建卡时构造并长期复用 |
| `resilience.bypass_after_failures` | 熔断器实例化时固定阈值 |
| `selfheal.*` | 自愈层是按 home 缓存的单例，阈值在首次取用时定下 |

凭据（二选一）：

```bash
# ~/.hermes/.env
FEISHU_APP_ID=cli_xxxxx
FEISHU_APP_SECRET=xxxxx
```

```yaml
# 或 ~/.hermes/config.yaml
feishu:
  app_id: cli_xxxxx
  app_secret: xxxxx
  # 国际版 Lark 需要额外指定：
  # base_url: https://open.larksuite.com

  # ── 以下均为可选：不配则全部会话走上面这一套凭据 ──
  bots:                      # 多应用：不同会话由不同机器人发卡
    ops:
      app_id: cli_ops
      app_secret: xxxxx
      # base_url 省略时继承上面的顶层域名
  chat_bindings:             # 会话 → 用哪个 bot
    oc_xxxxxxxx: ops
  native_chats:              # 这些会话完全不接管，走 Hermes 原生输出
    - oc_public_group
```

`native_chats` 命中的会话一张卡都不建——公共大群这类不希望出现流式卡片的场景用它精确排除，比全局开关细。

---

## 命令

```bash
$HERMES_PYTHON -m hermes_lark_streaming status    # 环境、配置与冲突检查
$HERMES_PYTHON -m hermes_lark_streaming verify    # 验证全部 9 个织入前提（只读）
$HERMES_PYTHON -m hermes_lark_streaming doctor    # 详细诊断与修复建议
$HERMES_PYTHON -m hermes_lark_streaming selftest  # 实际演练一次织入后回滚
$HERMES_PYTHON -m hermes_lark_streaming heal      # 查看自愈经验（reset 可清空重学）
$HERMES_PYTHON -m hermes_lark_streaming activity  # 升级前检查：当前有没有任务在跑
```

`verify` 覆盖全部织入点：飞书 SDK、`AIAgent` 定位、动态属性、描述符机制、8 个
回调属性、适配器基类、对话主方法、`interrupt`、子任务服务。**类方法织入点必须
进 verify**——它们是类属性，此刻就能确定存在与否；漏检等于把本可提前预警的失效
推迟到运行时才暴露。可降级项单独统计：全部未通过项都可降级时 `verify` 仍返回 0，
因为插件确实还能用，只是对应能力缺失。

---

## 自愈与经验积累

插件会把运行中学到的东西落盘到 `~/.hermes/hermes-lark-streaming/state.json`，**跨 gateway 重启、跨 Hermes 升级留存**。

**精准降级取代一坏全坏**。某类收纳（状态提示 / 自我改进 / 澄清 / 授权）连续失败 3 次时，只把该类退回原生透传，其余能力照常工作；结论落盘后下次启动直接生效，不必再白白失败一遍。只有失败**跨越 3 类以上**才判定为系统性故障（凭据失效、网络不可达、权限缺失）并升级为全局熔断。

**自动试探恢复**。降级不是永久：每被拦截 20 次就放行一次试探，成功即恢复并清除经验。试探尺度用「拦截次数」而非墙上时间——消息密集时恢复得快，空闲时不做无谓探测。

**环境变化即作废重学**。指纹由实际织入的回调集合计算（不用版本号——同一个 Hermes 语义版本可能对应几十个 commit）。Hermes 或插件升级导致织入面变化时，旧的降级结论立即作废。

**升级后诊断从笼统变精确**。插件记录每次成功织入的回调集合，并通过描述符观测哪些回调**真实被 Hermes 装配过**。升级后织入失败时，报告会指出「相比上次成功的 8 个回调，本次缺失 `reasoning_callback`」，而不是只报一句失败。

三条红线：

- **绝不改你的 `~/.hermes/config.yaml`**。优先级严格为 **你的显式配置 > 学到的经验 > 内置默认**；你写死 `capture.notice: true` 时即使连续失败一百次也会继续尝试，插件只在 `heal` 报告里说明情况
- **绝不自动改代码**。自修复止于运行参数
- **完全旁路**。自愈层任何异常都被吞掉，`selfheal.enabled: false` 可整层关闭，关掉后行为与不带自愈时完全一致

---

## 消息覆盖

| 消息类型 | 归属 | 呈现 |
|---|---|---|
| 答案流式 | 卡内 | 打字机 |
| 思考 / 原生推理 | 卡内 | 折叠面板 |
| 工具调用 | 卡内 | 时间线面板（含参数、耗时、结果） |
| 上下文压缩 | 卡内 | 灰色提示行 |
| 重试 / 限流 | 卡内 | 橙色提示行 |
| 工作轮 / 迭代状态 | 卡内 | 灰色提示行 |
| 忙时新消息反馈 | 卡内 | 灰色提示行（steer / redirect / queued / interrupting 全组） |
| 额度 / 系统通知 | 卡内 | 灰色提示行 |
| Self-improvement review | 卡内 | 🧠 提示块 |
| 记忆更新 | 卡内 | 🧠 提示块 |
| 澄清提问 | 卡内状态 + 原生交互卡 | ❓ 状态块 |
| 命令授权 | 卡内状态 + 原生交互卡 | 🔐 状态块 |
| 定时任务结果 | 独立卡片 | 完整卡片（含任务名与时间） |
| 后台任务完成 | 独立卡片 | 完整卡片 |
| 委派子任务 | 卡内 | 🧩 提示块（启动含目标与模型，终态含结果摘要） |
| `/stop` 中断 | 卡内终态 | 定格为「已停止」（红） |
| 被新消息打断 | 卡内终态 | 定格为「已中断 · 新消息已接续」（橙） |
| 长时间无更新 / 会话被淘汰 | 卡内终态 | 定格为「⏱️ 已超时收尾」（橙）+ 说明任务可能仍在运行 |
| Gateway 关闭 / 重启 | **卡内 + 聊天双份** | 灰色提示行，且刻意不抑制原生消息 |

**为什么 gateway 关闭通知要双份**：这条消息在 `stop()` 最开头发出，此时事件循环随时会停，而卡片更新要经过 100ms 节流再走一次飞书 API 往返——协程很可能来不及执行。若照常抑制原生输出，用户就既看不到卡片更新、也看不到通知，消息彻底丢失。所以这一类走「卡片记一份 + 原生照发」，是「宁可重复也不丢」在这个场景的落点。识别用的短语直接从 Hermes 的 `_INTERRUPT_REASON_GATEWAY_SHUTDOWN` / `_INTERRUPT_REASON_GATEWAY_RESTART` 常量借，Hermes 改文案时自动跟随。

**澄清与授权为何不接管按钮**：这两者会阻塞 Agent 线程，接管意味着要自己实现等待、超时与结果回填，任何一处出错都会让 Agent 永久挂起。审批还是安全边界。因此首版只在卡片内如实反映状态，交互本身仍由 Hermes 原生完成——卡片信息完整，且零风险。

---

## 稳定性设计

- **全链路 fail-open**：任何一步失败都立即透传原方法，消息永不丢失
- **投递三态**：`TAKEN` / `DECLINED` / `UNKNOWN`，不确定时一律放行原生输出（宁可重复也不丢）
- **精准降级**：单类收纳持续失败只关闭该类，经验落盘并自动试探恢复（见上节）
- **熔断**：失败跨越 3 类以上判定为系统性故障，自动进入旁路，本进程后续全部走 Hermes 原生路径
- **双保险收卡**：对话方法织入提供精确终态；后台空闲守护兜底，保证卡片不会永久停在「处理中」
- **回收即收卡**：turn 因容量淘汰或 TTL 过期被摘除时，若卡片尚未定格则先强制收尾再丢对象。参考实现在这条路径上直接丢弃 turn，卡片会永远转圈
- **超时不冒充完成**：强制收尾的卡片显示「⏱️ 已超时收尾」并说明任务可能仍在运行，而不是「✅ 已完成」——把停止跟踪显示成成功比不收卡更误导
- **中断即时定格**：织入 `AIAgent.interrupt()` 捕获中断，并用 Hermes 自己的 `hard_cancel` 语义区分「用户 /stop」与「被新消息接续」，不必等 90 秒兜底
- **消息删除保护**：识别 231003 / 230011 / 1000023，消息被撤回后立即停止一切更新
- **元素预算**：接近飞书 200 元素上限时按语义边界自动拆卡，旧卡封存且数据完整
- **敏感信息脱敏**：命令、参数、日志中的 token / secret / API key 在写日志前完成打码

---

## 架构

```text
L0  bridge/     桥接层 — 唯一接触 Hermes 的层（运行时织入）
L1  events/     事件层 — Hermes 语义归一化
L2  core/       领域层 — Turn 状态机、Segment 模型（不含飞书概念）
L3  render/     渲染层 — Segment → CardKit JSON
L4  transport/  传输层 — 飞书 API、节流、重试、熔断
    selfheal/   旁路层 — 经验积累与精准降级（不被任何层依赖）
    orchestrator.py  编排层 — 串联以上各层
```

依赖方向严格单向。Hermes 升级的影响半径被完全限制在 L0。`selfheal/` 不被任何层反向依赖：它由编排层调用、通过依赖注入接收桥接层的装配观测，因此可以整层关闭而不影响其余部分。

---

## 文档

- [01-源码调研与对比](docs/01-源码调研与对比.md) —— 两个参考插件的实现原理、优缺点、流式差异根因
- [02-架构设计](docs/02-架构设计.md) —— 分层、数据模型、核心机制、风险登记
- [03-升级韧性设计](docs/03-升级韧性设计.md) —— Hermes 升级为何会让插件失效，以及本插件的应对

---

## 运行要求

- Hermes Agent（已配置飞书平台）
- Python ≥ 3.11
- `lark-oapi` ≥ 1.4.0、`PyYAML` ≥ 6.0
- 飞书应用权限：消息卡片（CardKit）读写、消息发送与回复、图片上传

---

## License

MIT

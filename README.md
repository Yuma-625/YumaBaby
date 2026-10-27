# Yuma小鱼

一个跑在 QQ 群里的轻量智能体：**LLM 闲聊 + 人设自进化**，通过 NapCatQQ（OneBot v11 正向 WebSocket）接入 QQ，对外提供 FastAPI HTTP 接口。

> 当前状态：**股票/基金行情功能整体搁置**，对应技能仍是占位实现，市场简报定时任务默认关闭。已修复的是 QQ 连接、人设审核、调度与工程底座。

## 快速开始

```powershell
# 1. 依赖（已装好可跳过）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 配置
Copy-Item .env.example .env
# 编辑 .env：至少填 LLM_API_KEY；接入 QQ 时填 QQ_WS_URL / QQ_TARGET_GROUPS / QQ_BOT_UID / QQ_PERSONA_ADMIN

# 3. 启动
.\.venv\Scripts\python.exe run.py
```

> 同一时间只能运行一个实例：`run.py` 会持有 `data/yumababy.lock`，重复启动会直接退出。多开会导致同一条消息被回复两次。

启动后：

- HTTP: `http://127.0.0.1:8000/api/v1/health`
- 接口文档: `http://127.0.0.1:8000/docs`
- 前置条件：NapCatQQ 已开启「HTTP/WS 服务端」，默认地址 `ws://127.0.0.1:3001`

## 目录结构

```
app/
├─ main.py          FastAPI 入口 + lifespan（拉起 QQ 适配器、调度器）
├─ config.py        配置读取（.env 固定在项目根目录）
├─ shared.py        qq_adapter 全局引用
├─ api/routes.py    /health /skills /chat /broadcast
├─ models/schemas.py
├─ agent/
│  ├─ core.py       技能注册 + 关键词分发（回退路径）
│  ├─ loop.py       工具调用循环（步数上限 / 重复检测 / 收尾）
│  ├─ service.py    取历史 + 记忆 → 循环 → 落库
│  ├─ context.py    人设 + 记忆拼装 system prompt
│  ├─ demo.py       命令行演示：默认离线剧本，--live 走真实 API
│  └─ scheduler.py  定时任务（20:00 人设审核；市场简报受开关控制）
├─ tools/
│  ├─ registry.py   工具注册表（JSON Schema + 参数校验 + 超时 + 统一错误包）
│  └─ builtin.py    时间 / 网页抓取 / Bing 搜索 / 记忆读写
├─ memory/
│  └─ store.py      SQLite：对话 / 记忆 / 主题 / 向量 / 行为日志 / 打分 / 数据集
├─ review/
│  ├─ server.py     本地标注台 API（0–10 分、导出 JSONL）
│  └─ page.py       标注页面（单文件 HTML）
├─ adapters/
│  ├─ base.py       适配器抽象基类
│  └─ qq.py         NapCat 适配器：重连、去重、CQ 码清洗、自消息过滤
├─ skills/
│  ├─ base.py       BaseSkill(name / description / keywords / match / execute)
│  ├─ stock/        股票行情（占位，暂缓）
│  ├─ fund/         基金净值（占位，暂缓）
│  └─ chat/         LLM 闲聊 + 帮助
└─ utils/
   ├─ llm.py        AsyncOpenAI 封装（chat / complete / 工具调用 / 超时）
   ├─ usage.py      LLM 用量记账与每日预算守卫
   ├─ persona.py    人设三态：base / evolved / pending，原子落盘
   └─ helpers.py    日志配置
tests/              unittest（无需 pytest）
data/persona.json   人设，运行时生成（已忽略）
data/memory.db      SQLite 记忆库，运行时生成（已忽略）
data/usage.jsonl    LLM 用量明细（已忽略）
data/datasets/      标注导出的数据集（已忽略）
```

## 消息处理链路

```
NapCat → QQAdapter
  ├ 过滤 message_sent / 自己发的消息 / 重复 message_id
  ├ 群消息：目标群白名单 → GROUP_TRIGGER_MODE 判定（@ / 关键词 / 全部）
  ├ 私聊：QQ_PRIVATE_WHITELIST + 管理员；管理员审核窗口内按审核意见处理
  └ AgentService（AGENT_ENABLED=true）
       取最近 N 轮 + 相关记忆 → AgentLoop → 工具 → 回复 → 落库

AGENT_ENABLED=false 时回退旧路径：AgentCore → stock/fund/chat 关键词技能
```

## Agent 循环（M0）

`app/agent/loop.py` + `app/tools/registry.py` 是最小可用的工具调用循环，目前**只接命令行，尚未接入 QQ**。

```powershell
python -m app.agent.demo                                  # 离线剧本，不花钱
python -m app.agent.demo --live --message "现在几点了？"    # 真实模型，自己决定调 get_time
python -m app.agent.demo --live --message "12 乘 34 是多少" # 自己决定调 multiply
```

一轮的形状：

```
system + history + user
  → LLM(tools=注册表)
  → 有 tool_calls → 执行 → tool 消息回填 → 再问
  → 没有 tool_calls → 答复
```

三道保险：

| 保险 | 行为 |
|---|---|
| 步数上限 `max_steps` | 到达后补一次不带工具的收尾调用，让模型基于已有信息总结 |
| 重复调用检测 | 同工具 + 同参数（含键序不同）再次出现 → 立即停止，返回「卡住了」 |
| 工具超时 | 注册表按工具配置超时，超时作为错误回填给模型 |

工具执行结果统一为 `{"ok": true, "result": ...}` 或 `{"ok": false, "error": ..., "details": [...]}`，参数错误、未知工具、异常都会被回填，模型可以据此自我修正。

已验证的行为（真实 API）：

| 输入 | 模型动作 |
|---|---|
| 「现在几点了？」 | 调用 `get_time` → 回报时间 |
| 「12 乘 34 是多少」 | 调用 `multiply(a=12, b=34)` → 408 |
| 「介绍一下你自己」 | **不调用任何工具**，直接回答 |

## Agent 接入 QQ（M1）

`AGENT_ENABLED=true`（默认）时，通过触发规则的消息走 AgentLoop，不再是关键词路由。

新增能力：

| 模块 | 作用 |
|---|---|
| [app/memory/store.py](app/memory/store.py) | SQLite：对话、记忆、行为日志、回复评价 |
| [app/agent/service.py](app/agent/service.py) | 取历史 + 检索记忆 + 跑循环 + 落库 + 记录行为 |
| [app/agent/context.py](app/agent/context.py) | 人设 + 记忆拼装 system prompt |
| [app/agent/admin.py](app/agent/admin.py) | 管理员命令：好评/差评、今日活动、LLM 用量 |
| [app/tools/builtin.py](app/tools/builtin.py) | `get_time`、`web_search`、`web_fetch`、`remember`、`recall`、`forget` |

记忆的 scope 由代码注入，模型看不到也改不了：

| 场景 | scope | chat_id |
|---|---|---|
| 群聊 | `("group", 群号)` | `group:<群号>` |
| 私聊 | `("user", QQ号)` | `user:<QQ号>` |

`/health` 增加 `memory` 字段（是否启用、记忆条数）。`/chat` 在服务启动后同样走 AgentLoop。

### 对话记录、行为日志与评价

只存与机器人交互的消息（私聊全部 + 群里触发后的对话），不存群聊全量。

| 表 | 内容 |
|---|---|
| `messages` | 收/发消息，含平台 `message_id`，用于引用评价 |
| `events` | 每轮回复（steps/stopped/tools）与每次工具调用（参数、耗时、成败） |
| `reply_feedback` | 对某条回复的好评/差评 |

管理员私聊命令（不调用 LLM）：

| 发送 | 效果 |
|---|---|
| `好` / `好评` / `赞` / `不错` | 给最近一条回复记好评 |
| `差` / `差评` / `不行` | 记差评 |
| 引用某条回复再发 `好` / `差` | 评价被引用的那条 |
| `今天干了什么` / `日报` | 返回当日收发条数、工具调用、好评差评、花费 |
| `llm check` / `llm 用量` | 今日与累计 token、估算花费、当前会话上下文长度、预算状态 |

HTTP：`GET /api/v1/activity?date=YYYY-MM-DD`（省略 date 为今天），返回同样的统计。

私聊白名单：只有 `QQ_PRIVATE_WHITELIST` 里的 QQ 能私聊，其他人一律忽略；管理员始终在白名单内。

### 0–10 分标注台（独立系统）

QQ 里的「好/差」只是快速标记；真正的评分和数据集由独立的本地标注台完成，它和机器人共用同一个 SQLite。

```powershell
python -m app.review.server
# 打开 http://127.0.0.1:8100
```

流程：

1. 选日期，页面按时间顺序列出当天所有机器人回复，并带上它前面那条用户消息
2. 逐条点 0–10 分（可写备注），点选即保存；也可用 `0`–`9`、`a` 快捷键
3. 全部打完后点「提交今日并导出」→ 写入 `data/datasets/<日期>.jsonl` 并标记完成

数据集每行：

```json
{"date":"2026-10-23","chat_id":"user:1293585554","user":"小鱼晚上","assistant":"晚上好……","score":8,"note":"语气自然","message_id":42}
```

标记为完成的天会记录在 `review_days` 表，重复提交会覆盖同一天的导出文件。标注台默认只监听 `127.0.0.1`，不对外网开放。

网页抓取带 SSRF 防护：只允许 http/https，解析域名后拒绝私网、回环、链路本地和保留地址，重定向逐跳校验，响应体有大小上限。

联网搜索走 Bing 页面解析（`web_search`），结果缓存 5 分钟；Bing 部分结果页会反爬，模型会如实说明抓不到，而不是编造。

连发合并（M1.5）：同一会话在 `DEBOUNCE_SECONDS` 窗口内的多条消息合并成一次调用。

- 缓冲按「群 + 人」隔离，群友之间的消息不会互相混入
- 第一条命中 @ / 关键词后，窗口内后续碎片消息**不再要求触发词**（「小鱼 在吗」→ 两条合成一条）
- 超过 `DEBOUNCE_MAX_CHARS` 或 `DEBOUNCE_MAX_MESSAGES` 立即处理，不等窗口
- `DEBOUNCE_SECONDS=0` 关闭该行为
- 适配器停止时取消所有待处理缓冲

### 记忆权重与维护

- 每条记忆带 `weight`：工具写入（用户明确要求记住）1.5，普通 1.0
- 召回分数 = `weight × exp(-年龄天数 / MEMORY_DECAY_DAYS)`，关键词命中再 ×1.8
- 开启主题后，主题只做加分与分组（`+0.10`），不排除其他主题
- 每天 04:30 清理：衰减后低于 0.05 且超过 180 天的删除，每个 scope 截到 `MEMORY_MAX_PER_SCOPE`

### 长期记忆：自动提炼、主题与向量

A0（默认关闭，`MEMORY_EXTRACT_ENABLED`）：

- 每 N 条消息（默认 10）后台提炼一次，独立 LLM 调用，不阻塞回复
- 只读 `messages` 表（和机器人的对话），不读群聊缓冲；只读群不提炼
- 只抽用户明确说过的长期事实，保留 `source_message_ids` 可回溯
- 精确 hash + `difflib` 近似去重；重复只更新 importance
- 每日 05:10 补跑有积压的会话，每天最多 `MEMORY_EXTRACT_DAILY_LIMIT` 次

C（`MEMORY_HIERARCHY_ENABLED`）：

- 提炼时让模型给 2–8 字主题名和同义词关键词，主题按 scope 隔离
- 检索时主题只加分、prompt 里按主题分组；**不做硬门禁**
- 主题事实 ≥5 条时生成 1–2 句摘要（压缩），每天 05:20 刷新
- 主题上限 50，超出并入「其他」；空主题自动清理

A1（`EMBEDDING_ENABLED`）：

- 走 OpenAI 兼容 `/embeddings`；DeepSeek 本身没有 embedding
- 向量存 SQLite BLOB，numpy 余弦；失败自动降级关键词
- 补向量：`.\.venv\Scripts\python.exe -m app.memory.reindex`
- 旧记忆补主题：`.\.venv\Scripts\python.exe -m app.memory.backfill`

### 会话压缩

`HISTORY_COMPACT_ENABLED=true` 时：

- 一个会话超过 `HISTORY_COMPACT_AFTER` 条消息后，后台把旧对话压成摘要
- 只保留最近 `HISTORY_COMPACT_KEEP` 条原文，摘要存 `chat_summaries`
- 后续上下文 = “此前对话摘要 + 最近若干条”，原始消息不删除
- 摘要增量更新：新摘要会带上旧摘要，不会从头重压
- 压缩失败不写入，下一轮重试

### 沙箱文件工具（仅管理员私聊）

`SANDBOX_ENABLED=true` 时，管理员私聊可让模型使用：

```
fs_list  fs_read  fs_write  fs_edit  fs_glob  fs_grep
```

- 根目录固定 `SANDBOX_ROOT`，拒绝绝对路径、`..`、盘符、UNC、符号链接
- 只能读写文本，读上限 `SANDBOX_MAX_READ_BYTES`，写上限 `SANDBOX_MAX_WRITE_BYTES`
- 默认不覆盖；覆盖要显式 `overwrite=true`
- 没有删除/移动命令
- **群里和其他人私聊不会注册这些工具**；写操作记 `events(kind="sandbox")`

⚠️ 这是路径 jail + 权限 + 审计，不是 OS 级安全隔离；机器人仍以你的用户权限运行。

### 图片与表情

- 图片会下载、转成 base64 后作为 `image_url` 内容段发给模型（`deepseek-chat` 本身支持视觉）
- 表情包（`mface`/`bface`/`marketface`）若带 PNG/JPEG/WebP 链接，也会一并送进视觉；GIF 暂时跳过，只保留文字占位
- 只发送**当前这一条消息**里的图片，历史里的图不重复发送，避免每轮重复计费
- 每轮最多 `VISION_MAX_IMAGES` 张、单张不超过 `VISION_MAX_BYTES`，超限跳过
- 图片下载走 SSRF 校验（拒绝私网/回环地址）
- 表情仍按文字：`[表情:微笑]`；系统表情只有 id 时记 `[表情]`
- 元数据（url/file/summary）存进 `messages.attachments`，数据集导出时带 `user_attachments`
- ⚠️ 图片会发送到模型服务商，涉及隐私的图片建议先关闭 `VISION_ENABLED`

实测：左右红蓝分色图，模型回答「左边红色，右边蓝色」。

### 群聊短期上下文

群里所有消息都会进一个滚动缓冲（`group_context` 表），被 @ 或触发时注入最近 10 条：

- 解决“先发表情包、再 @ 它说‘用这个’”这类指代问题
- 只保留每个群最近 `GROUP_CONTEXT_MAX` 条；只读/分析群改为按 `GROUP_CONTEXT_KEEP_DAYS` 天保留，供日报使用
- 注入窗口 `GROUP_CONTEXT_MINUTES` 分钟，不进 `messages`、不进数据集、不导出
- 如果最近 3 条里有图片/表情包且当前消息没带图，会取最后一张送进视觉（`VISION_CONTEXT_IMAGES`）
- 问到「昨晚 / 昨天 / 今早」时，会按时间段从 `group_context` 取记录注入（`GROUP_HISTORY_LIMIT`、`GROUP_HISTORY_CHARS`）；取不到会明确说没有记录，避免编造
- 关闭：`GROUP_CONTEXT_ENABLED=false`

真实测试：先让上下文里放一张左右分色图，再问「我刚才发的图是什么颜色」，回答「左边红右边蓝」。

### 只读群与每日群聊分析

`QQ_READONLY_GROUPS` 里的群：

- 照常接收消息、写入 `group_context`，供上下文和总结使用
- **永不发言**：不回复 @、不参与旁听、不响应任何指令
- 记录完直接返回，不会进入 AgentLoop

`GROUP_ANALYSIS_GROUPS` 里的群每天 **23:40** 自动分析：

- 机器统计：消息量、发言人数、人均字数、图片/表情占比、活跃时段、发言最多的人、口头禅
- 模型观察：今天聊了什么、成员的说话与表情习惯
- 报告存进 `group_reports` 表，并私聊发给管理员，不发群
- 每个群每天只出一次，`events` 里记 `group_report`

### 群聊总结

管理员在群里发：

```text
总结最近 100 条群聊
总结一下聊天
汇总最近 50 条对话
```

- 条数默认 `SUMMARY_DEFAULT_COUNT`，范围 5–`SUMMARY_MAX_COUNT`（默认 120）
- 数据来自 `group_context` 滚动缓冲（每群 `GROUP_CONTEXT_MAX` 条），不需要提前导出
- 输出 2–4 点：主要话题、结论/决定、待跟进事项，控制在 200 字内
- 同一群 `SUMMARY_COOLDOWN_SECONDS`（默认 600 秒）内只能总结一次
- 每个群每天最多 `SUMMARY_DAILY_LIMIT`（默认 10）次
- 默认所有人可用；`SUMMARY_ADMIN_ONLY=true` 可收回给管理员

### 表情收藏（保存仅管理员）

管理员发「存这个表情包 / 保存这个表情 / 收藏这个表情包」时生效，其他人说同样的话按普通聊天处理。

取图顺序：

1. 当前消息自带的图片/表情
2. 引用消息里的图片（`get_msg` 回填）
3. 最近 `STICKER_LOOKBACK_MINUTES` 分钟内群上下文里的最后一张图

保存到 `data/stickers/<sha1>.<ext>`，`stickers` 表记录 hash、名称、来源、时间；同一张图按内容 hash 去重。文件名以外的信息都不会外发。

存的时候可以顺手命名：

```text
存这个表情包，命名奶龙坐
保存这个表情 命名：奶龙
```

同名图片重复存不会新增；对已存过的图重新命名会更新名字。之后可以直接按名字发：

```text
发奶龙坐表情
发送大狗无语
```

按名字发送不要求句子里有「表情」二字，只要以「发 / 发送 / 来 / 把」开头并点到了已存名字即可。

发回来不限管理员，谁都能用：

```text
发 1 号表情
发表情 2
发个表情        # 随机一个
```

发送时用 `base64://` 的 CQ 码，不依赖 NapCat 的 `enableLocalFile2Url` 开关。

### 群聊自动旁听

在 `QQ_PARTICIPATION_GROUPS` 里的群：

- **@ 或关键词 → 必定回复**（不走概率）
- 其他消息按 `PARTICIPATION_RATE` 抽样（问句概率翻倍），并受 `PARTICIPATION_COOLDOWN_SECONDS` 冷却
- 被抽中的消息交给模型，模型可以选择**不接话**（输出 `[SKIP]`，不发送也不入库）
- 接话与跳过都会写 `events`，方便回看它为什么开口
- 消息里有人被 @ 时，模型会输出 `[[at:QQ]]`，适配器发送前转成真正的 `[CQ:at,qq=QQ]`

主动消息也收紧了：人设总结只在**有待审规则**、**最近 10 分钟没有对话**、**当天未推送**时才发。

关闭 Agent 回退旧行为：`AGENT_ENABLED=false`。

## 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEBUG` | false | 开启 uvicorn reload |
| `HOST` / `PORT` | 0.0.0.0 / 8000 | 监听地址 |
| `TIMEZONE` | Asia/Shanghai | 定时任务时区 |
| `REVIEW_HOST` / `REVIEW_PORT` | 127.0.0.1 / 8100 | 标注台监听地址 |
| `DATASETS_DIR` | data/datasets | 标注导出的数据集目录 |
| `API_TOKEN` | 空 | HTTP API 令牌；设置后 `/chat`、`/broadcast` 必须带 `Authorization: Bearer <token>` |
| `API_AUTH_REQUIRED` | false | 为 true 且未设 Token 时直接 503，避免裸接口上线 |
| `QQ_ENABLED` | false | 是否启动 QQ 适配器 |
| `QQ_WS_URL` | ws://127.0.0.1:6700 | NapCat 正向 WS 地址 |
| `QQ_ACCESS_TOKEN` | 空 | NapCat 的 access token，填入后走 `Authorization: Bearer` |
| `QQ_TARGET_GROUPS` | 空 | 群白名单，逗号分隔；为空则不处理群消息 |
| `QQ_BOT_UID` | 空 | 机器人 QQ，用于识别 `[CQ:at,...]` |
| `QQ_AUTO_REPLY_KEYWORDS` | 空 | 命中即回复的关键词，逗号分隔 |
| `QQ_PERSONA_ADMIN` | 空 | 管理员 QQ；为空则私聊与审核流程关闭 |
| `QQ_PRIVATE_WHITELIST` | 空 | 允许私聊的用户，逗号分隔；管理员始终允许 |
| `GROUP_TRIGGER_MODE` | mention_or_keyword | 群里响应方式：`mention` / `keyword` / `mention_or_keyword` / `all` |
| `QQ_PARTICIPATION_GROUPS` | 空 | 允许自动旁听接话的群；@/关键词仍必定回复 |
| `QQ_READONLY_GROUPS` | 空 | 只读群：只记录消息，任何情况下都不发言 |
| `GROUP_ANALYSIS_GROUPS` | 空 | 每晚 23:40 分析这些群，报告私聊给管理员 |
| `PARTICIPATION_RATE` | 0.15 | 旁听消息进入模型的概率（问句翻倍），0 关闭 |
| `PARTICIPATION_COOLDOWN_SECONDS` | 120 | 同一群两次自动接话的最小间隔 |
| `GROUP_CONTEXT_ENABLED` | true | 是否记录群聊短期上下文 |
| `GROUP_CONTEXT_MAX` | 300 | 普通群保留的最近消息条数 |
| `GROUP_CONTEXT_KEEP_DAYS` | 3 | 只读/分析群按天保留的时长 |
| `GROUP_CONTEXT_MINUTES` | 30 | 注入上下文的时间窗口 |
| `GROUP_CONTEXT_INJECT` | 10 | 每次注入多少条 |
| `GROUP_HISTORY_LIMIT` | 60 | 按时间回看群聊时最多取多少条 |
| `GROUP_HISTORY_CHARS` | 3000 | 回看群聊的字符上限 |
| `VISION_CONTEXT_IMAGES` | 1 | 上下文里最多带几张历史图片 |
| `MEMORY_DECAY_DAYS` | 30 | 记忆权重衰减时间常数（天） |
| `MEMORY_MAX_PER_SCOPE` | 200 | 每个用户/群保留的记忆上限 |
| `DEBOUNCE_SECONDS` | 1.5 | 连发合并窗口；0 表示关闭（每条立即处理） |
| `DEBOUNCE_MAX_CHARS` | 400 | 合并文本超过这个长度立即处理 |
| `DEBOUNCE_MAX_MESSAGES` | 5 | 合并条数达到即立即处理 |
| `AGENT_ENABLED` | true | 是否用 AgentLoop 处理 QQ/HTTP 对话 |
| `AGENT_MAX_STEPS` | 6 | 单轮工具调用步数上限 |
| `AGENT_HISTORY_TURNS` | 8 | 带多少轮历史（1 轮 = 1 问 1 答） |
| `AGENT_MEMORY_LIMIT` | 5 | 每轮注入多少条记忆 |
| `HISTORY_COMPACT_ENABLED` | false | 是否压缩旧对话 |
| `HISTORY_COMPACT_AFTER` | 30 | 超过多少条消息触发压缩 |
| `HISTORY_COMPACT_KEEP` | 12 | 压缩后保留多少条原文 |
| `SANDBOX_ENABLED` | false | 是否启用沙箱文件工具（仅管理员私聊） |
| `SANDBOX_ROOT` | data/sandbox | 沙箱根目录 |
| `SANDBOX_MAX_READ_BYTES` | 200000 | 单文件读取上限 |
| `SANDBOX_MAX_WRITE_BYTES` | 1000000 | 单文件写入上限 |
| `MEMORY_DB` | data/memory.db | SQLite 路径 |
| `MEMORY_EXTRACT_ENABLED` | false | 是否后台自动提炼长期记忆 |
| `MEMORY_EXTRACT_EVERY` | 10 | 攒多少条消息提炼一次 |
| `MEMORY_EXTRACT_DAILY_LIMIT` | 20 | 每天最多提炼次数 |
| `MEMORY_HIERARCHY_ENABLED` | false | 是否启用主题软分层 |
| `AGENT_MEMORY_TOPICS` | 3 | 每轮最多注入几个主题 |
| `MEMORY_TOPICS_MAX` | 50 | 每个 scope 的主题上限 |
| `EMBEDDING_ENABLED` | false | 是否启用向量检索 |
| `EMBEDDING_MODEL` | BAAI/bge-m3 | OpenAI 兼容 embedding 模型名 |
| `WEB_FETCH_TIMEOUT` | 15 | 抓网页超时（秒） |
| `WEB_FETCH_MAX_BYTES` | 2000000 | 响应体上限 |
| `WEB_FETCH_MAX_CHARS` | 4000 | 喂给模型的正文上限 |
| `MARKET_PUSH_ENABLED` | false | 是否注册 9:30/15:00 市场简报任务 |
| `PERSONA_FEEDBACK_TTL_MINUTES` | 180 | 审核窗口超时 |
| `PERSONA_ANALYZE_DAILY_LIMIT` | 20 | 每天最多提炼次数 |
| `PERSONA_MAX_PENDING` | 20 | 待审核规则上限 |
| `LLM_API_KEY` | 空 | 未填或仍是占位符时自动跳过 LLM |
| `LLM_BASE_URL` | https://api.deepseek.com | 兼容 OpenAI 协议的服务 |
| `LLM_MODEL` | deepseek-chat | 模型名 |
| `LLM_TIMEOUT` | 30 | 单次请求超时（秒） |
| `LLM_MAX_TOKENS` | 800 | 单次回复上限 |
| `LLM_PRICE_INPUT_PER_M` | 2 | 输入单价（元/百万 token），仅用于估算 |
| `LLM_PRICE_OUTPUT_PER_M` | 8 | 输出单价（元/百万 token） |
| `DAILY_BUDGET_SOFT` | 10 | 日花费软上限：只告警 |
| `DAILY_BUDGET_HARD` | 30 | 日花费硬上限：超过后跳过 LLM 调用，走兜底文案 |
| `USAGE_LOG` | data/usage.jsonl | 用量明细文件 |
| `LOG_LEVEL` | INFO | 日志级别 |

### 用量与预算

每次 LLM 调用都会把 `prompt_tokens / completion_tokens / 估算费用` 追加到 `data/usage.jsonl`，`/api/v1/health` 的 `usage_today` 返回当日汇总：

```json
"usage_today": {
  "date": "2026-10-22", "calls": 12, "prompt_tokens": 48213,
  "completion_tokens": 3900, "cost_cny": 0.1276,
  "by_kind": {"chat": {"calls": 10, "cost_cny": 0.11}, "analysis": {"calls": 2, "cost_cny": 0.0176}},
  "soft_limit_cny": 10.0, "hard_limit_cny": 30.0, "level": "ok"
}
```

单价只是估算，实际以账单为准；把 `DAILY_BUDGET_SOFT/HARD` 设为 `0` 可关闭对应档位。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 状态：版本、LLM 是否配置、QQ 连接、调度任务 |
| GET | `/api/v1/skills` | 已注册技能 |
| GET | `/api/v1/activity` | 当日（或 `?date=` 指定）消息、工具、评价、花费 |
| POST | `/api/v1/chat` | `{"message": "..."}` → `{"reply": "...", "skill_used": "chat"}` |
| POST | `/api/v1/broadcast` | `{"message": "...", "groups": ["..."]}` 群发，需 QQ 已连接 |
| GET | `/api/v1/memory/topics?scope_type=user&scope_id=1` | 主题列表（名称、关键词、摘要、事实数） |
| GET | `/api/v1/memory/topics/{id}` | 主题详情 + 事实 |
| POST | `/api/v1/memory/extract?scope_type=user&scope_id=1` | 手动触发一次记忆提炼 |

设置了 `API_TOKEN` 后，`/chat`、`/broadcast`、`/activity` 以及记忆接口必须携带 `Authorization: Bearer <token>`；`/health` 与 `/skills` 保持公开只读。

## 人设机制

- `base`：固定设定，来自 `DEFAULT_BASE` 或 `data/persona.json`
- `evolved`：管理员确认后的规则，会拼进 system prompt
- `pending`：LLM 从对话中提炼出的候选规则

每天 20:00 若有 pending，会私聊管理员清单；回复 `保留 1 3` / `删除 2` / `全部保留` / `全部删除` / 纯序号即可处理。审核窗口默认 3 小时，超时后本条消息按普通对话处理并提示超时。

## 生产部署（systemd）

单机 Linux 部署，NapCat 与本服务同机，全程只走 `127.0.0.1`，**不需要开放任何入站端口**（SSH 除外）。

**1. 准备用户和目录**

```bash
sudo useradd -r -m -d /opt/yumababy -s /bin/bash yuma
sudo git clone git@github.com:Yuma-625/YumaBaby.git /opt/yumababy
cd /opt/yumababy
sudo -u yuma python3 -m venv .venv
sudo -u yuma .venv/bin/pip install -r requirements.txt
sudo -u yuma mkdir -p data
```

**2. 密钥放到 `/etc`，不放在项目里**

```bash
sudo install -d -m 750 /etc/yumababy
sudo install -m 600 -o root -g root /dev/null /etc/yumababy/env
sudo nano /etc/yumababy/env
```

内容示例（注意 `HOST=127.0.0.1`、`DEBUG=false`、`API_AUTH_REQUIRED=true`）：

```dotenv
DEBUG=false
HOST=127.0.0.1
PORT=8000
TIMEZONE=Asia/Shanghai
API_TOKEN=用 openssl rand -hex 32 生成
API_AUTH_REQUIRED=true
QQ_ENABLED=true
QQ_WS_URL=ws://127.0.0.1:3001
QQ_ACCESS_TOKEN=与 NapCat 侧一致
QQ_TARGET_GROUPS=你的群号
QQ_BOT_UID=机器人QQ
QQ_AUTO_REPLY_KEYWORDS=小鱼
QQ_PERSONA_ADMIN=管理员QQ
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-chat
```

**3. 安装并启动**

```bash
sudo cp deploy/systemd/yumababy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yumababy
systemctl status yumababy
journalctl -u yumababy -f
```

单元文件已内置 `Restart=always`、`ProtectSystem=strict`，仅允许写 `/opt/yumababy/data`（人设文件）。

**4. NapCat**

独立运行（Docker 或官方 Linux 包均可），监听 `127.0.0.1:3001`，并配置与 `QQ_ACCESS_TOKEN` 一致的 token。NapCat 不需要公网端口。

**5. 访问接口**

`HOST=127.0.0.1` 时外部不可达，需要时用 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:8000 yuma@服务器
curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v1/health
```

**密钥轮换**：改 `/etc/yumababy/env` → `sudo systemctl restart yumababy`。若 Key 可能已泄露，先在 DeepSeek 后台吊销再重启。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

覆盖：CQ 码清洗、自消息/重复消息过滤、鉴权头参数、技能分发、配置解析、人设增删与审核、API 冒烟、lifespan 起停。

## 提交护栏

`.env` 已被 `.gitignore` 排除，仓库里只保留 `.env.example`。为防误操作（例如 `git add -f .env`），`scripts/hooks/pre-commit` 会拦截：

- 任何 `.env` / `*.env` 环境变量文件
- 暂存内容中的 `sk-` 密钥、`AKIA` 访问密钥、`ghp_` token、PEM 私钥块

每个克隆启用一次：

```powershell
git config core.hooksPath scripts/hooks
```

钩子不随仓库自动生效，换机器后需要重新执行上面这条命令。

## 已知限制

- 股票 / 基金技能只返回「建设中」，未接 akshare；`CACHE_TTL`、`STOCK_DATA_SOURCE` 尚未被使用。
- 对话不保留上下文，每轮只带当前消息。
- 单人单进程设计，多实例会各自维护人设与调度。
- 私聊仅响应管理员，非管理员私聊直接忽略。

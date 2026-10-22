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
│  └─ builtin.py    时间 / 网页抓取（SSRF 防护）/ 记忆读写
├─ memory/
│  └─ store.py      SQLite：对话历史 + 按 scope 隔离的长期记忆
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
| [app/memory/store.py](app/memory/store.py) | SQLite：`messages` 存最近对话，`memories` 按 scope 存事实 |
| [app/agent/service.py](app/agent/service.py) | 取历史 + 检索记忆 + 跑循环 + 落库 |
| [app/agent/context.py](app/agent/context.py) | 人设 + 记忆拼装 system prompt |
| [app/tools/builtin.py](app/tools/builtin.py) | `get_time`、`web_fetch`、`remember`、`recall`、`forget` |

记忆的 scope 由代码注入，模型看不到也改不了：

| 场景 | scope | chat_id |
|---|---|---|
| 群聊 | `("group", 群号)` | `group:<群号>` |
| 私聊 | `("user", QQ号)` | `user:<QQ号>` |

`/health` 增加 `memory` 字段（是否启用、记忆条数）。`/chat` 在服务启动后同样走 AgentLoop。

网页抓取带 SSRF 防护：只允许 http/https，解析域名后拒绝私网、回环、链路本地和保留地址，重定向逐跳校验，响应体有大小上限。

连发合并（M1.5）：同一会话在 `DEBOUNCE_SECONDS` 窗口内的多条消息合并成一次调用。

- 缓冲按「群 + 人」隔离，群友之间的消息不会互相混入
- 第一条命中 @ / 关键词后，窗口内后续碎片消息**不再要求触发词**（「小鱼 在吗」→ 两条合成一条）
- 超过 `DEBOUNCE_MAX_CHARS` 或 `DEBOUNCE_MAX_MESSAGES` 立即处理，不等窗口
- `DEBOUNCE_SECONDS=0` 关闭该行为
- 适配器停止时取消所有待处理缓冲

关闭 Agent 回退旧行为：`AGENT_ENABLED=false`。

## 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEBUG` | false | 开启 uvicorn reload |
| `HOST` / `PORT` | 0.0.0.0 / 8000 | 监听地址 |
| `TIMEZONE` | Asia/Shanghai | 定时任务时区 |
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
| `DEBOUNCE_SECONDS` | 1.5 | 连发合并窗口；0 表示关闭（每条立即处理） |
| `DEBOUNCE_MAX_CHARS` | 400 | 合并文本超过这个长度立即处理 |
| `DEBOUNCE_MAX_MESSAGES` | 5 | 合并条数达到即立即处理 |
| `AGENT_ENABLED` | true | 是否用 AgentLoop 处理 QQ/HTTP 对话 |
| `AGENT_MAX_STEPS` | 6 | 单轮工具调用步数上限 |
| `AGENT_HISTORY_TURNS` | 8 | 带多少轮历史（1 轮 = 1 问 1 答） |
| `AGENT_MEMORY_LIMIT` | 5 | 每轮注入多少条记忆 |
| `MEMORY_DB` | data/memory.db | SQLite 路径 |
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
| POST | `/api/v1/chat` | `{"message": "..."}` → `{"reply": "...", "skill_used": "chat"}` |
| POST | `/api/v1/broadcast` | `{"message": "...", "groups": ["..."]}` 群发，需 QQ 已连接 |

设置了 `API_TOKEN` 后，`/chat` 与 `/broadcast` 必须携带 `Authorization: Bearer <token>`；`/health` 与 `/skills` 保持公开只读。

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

# YumaBaby 鱼宝宝

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
│  ├─ core.py       技能注册 + 关键词分发（可返回命中技能名）
│  └─ scheduler.py  定时任务（20:00 人设审核；市场简报受开关控制）
├─ adapters/
│  ├─ base.py       适配器抽象基类
│  └─ qq.py         NapCat 适配器：重连、去重、CQ 码清洗、自消息过滤
├─ skills/
│  ├─ base.py       BaseSkill(name / description / keywords / match / execute)
│  ├─ stock/        股票行情（占位，暂缓）
│  ├─ fund/         基金净值（占位，暂缓）
│  └─ chat/         LLM 闲聊 + 帮助
└─ utils/
   ├─ llm.py        AsyncOpenAI 封装（超时 / 重试 / 未配置时跳过）
   ├─ persona.py    人设三态：base / evolved / pending，原子落盘
   └─ helpers.py    日志配置
tests/              unittest（无需 pytest）
data/persona.json   运行时生成，已被 .gitignore 忽略
```

## 消息处理链路

```
NapCat → QQAdapter
  ├ 过滤 message_sent / 自己发的消息 / 重复 message_id
  ├ 群消息：目标群白名单 → 关键词或 @机器人 → AgentCore
  ├ 私聊：仅 QQ_PERSONA_ADMIN；审核窗口内按审核意见处理，否则走对话
  └ AgentCore：stock → fund → chat，按顺序取第一个 match() 命中的技能

ChatSkill → persona.get_system_prompt() → LLM
          → 后台按触发词/频次判断是否提炼新人设 → pending
```

## 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEBUG` | false | 开启 uvicorn reload |
| `HOST` / `PORT` | 0.0.0.0 / 8000 | 监听地址 |
| `TIMEZONE` | Asia/Shanghai | 定时任务时区 |
| `QQ_ENABLED` | false | 是否启动 QQ 适配器 |
| `QQ_WS_URL` | ws://127.0.0.1:6700 | NapCat 正向 WS 地址 |
| `QQ_ACCESS_TOKEN` | 空 | NapCat 的 access token，填入后走 `Authorization: Bearer` |
| `QQ_TARGET_GROUPS` | 空 | 群白名单，逗号分隔；为空则不处理群消息 |
| `QQ_BOT_UID` | 空 | 机器人 QQ，用于识别 `[CQ:at,...]` |
| `QQ_AUTO_REPLY_KEYWORDS` | 空 | 命中即回复的关键词，逗号分隔 |
| `QQ_PERSONA_ADMIN` | 空 | 管理员 QQ；为空则私聊与审核流程关闭 |
| `MARKET_PUSH_ENABLED` | false | 是否注册 9:30/15:00 市场简报任务 |
| `PERSONA_FEEDBACK_TTL_MINUTES` | 180 | 审核窗口超时 |
| `PERSONA_ANALYZE_DAILY_LIMIT` | 20 | 每天最多提炼次数 |
| `PERSONA_MAX_PENDING` | 20 | 待审核规则上限 |
| `LLM_API_KEY` | 空 | 未填或仍是占位符时自动跳过 LLM |
| `LLM_BASE_URL` | https://api.deepseek.com | 兼容 OpenAI 协议的服务 |
| `LLM_MODEL` | deepseek-chat | 模型名 |
| `LLM_TIMEOUT` | 30 | 单次请求超时（秒） |
| `LLM_MAX_TOKENS` | 800 | 单次回复上限 |
| `LOG_LEVEL` | INFO | 日志级别 |

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 状态：版本、LLM 是否配置、QQ 连接、调度任务 |
| GET | `/api/v1/skills` | 已注册技能 |
| POST | `/api/v1/chat` | `{"message": "..."}` → `{"reply": "...", "skill_used": "chat"}` |
| POST | `/api/v1/broadcast` | `{"message": "...", "groups": ["..."]}` 群发，需 QQ 已连接 |

## 人设机制

- `base`：固定设定，来自 `DEFAULT_BASE` 或 `data/persona.json`
- `evolved`：管理员确认后的规则，会拼进 system prompt
- `pending`：LLM 从对话中提炼出的候选规则

每天 20:00 若有 pending，会私聊管理员清单；回复 `保留 1 3` / `删除 2` / `全部保留` / `全部删除` / 纯序号即可处理。审核窗口默认 3 小时，超时后本条消息按普通对话处理并提示超时。

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

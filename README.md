# my-agent · 多模型任务台

一个可本地 / 云端混合部署的 **多模型 Agent 平台**：在同一个界面里并行驱动多个大模型完成同一任务，并给它们配上工具调用、长期记忆、记忆宫殿、计费与可观测性。

前端是一个单页「多模型任务台」：输入一次，多个模型并排跑，大屏对比结果；也可以让 2~4 个模型先各自作答，再由其中一个做裁判融合出最终答案。

---

## 功能特性

| 能力 | 说明 |
|---|---|
| 多模型并行 | `/chat`、`/chat/stream` 一次只跑一个模型；`/chat/fuse/stream` 让 2~4 个模型同时作答，由指定的裁判模型汇总 |
| 模型即插即用 | 内置本地模型（Ollama，OpenAI 兼容）与云端模型（DeepSeek 等）；运行时可注册任意 OpenAI 兼容接口，并标记是否支持视觉 |
| 约束词 / 项目契约 | 每个模型可单独追加「约束词」（业务口径），另有全局项目契约注入 system prompt，保证多模型协同同一项目时接口对齐 |
| 工具调用 | 天气、当前时间、计算器、Tavily 网页搜索、文件读写与追加、命令执行；带 **事务快照与回滚**（写前快照，可整体回滚） |
| 长期记忆 | SQLite 会话持久化 + 平台对话卡片 + 向量历史检索（`history_vector`） |
| 记忆宫殿 | 按提问语义把对话卡片挂成父子树：短句挂上一张卡，语义相似度 ≥ 阈值的挂到更早的卡，否则新开话题；嵌入模型离线运行，向量带内存缓存 |
| 平台计费 | 签发/停用平台 API Key（`sk-` 前缀），按 token 余额扣费、充值、查询用量流水 |
| 上游余额查询 | 按已注册模型的 Key 查询 DeepSeek / SiliconFlow 账户余额（不支持的 provider 明确返回不支持，不做探测） |
| 多智能体 | LangGraph supervisor 编排 + 知识库问答 agent |
| 可观测性 | 请求/工具调用统计与运行报告 |
| 部署 | 单文件 Dockerfile（python:3.11-slim），也可直接 `python api.py` |

## 架构

```
浏览器（static/index.html 单页任务台）
        │
        ▼
api.py  FastAPI：对话 / 流式 / 融合、模型管理、计费、记忆、宫殿树
        │
        ├── agent_with_memory.py   Agent 主循环（多轮工具调用 + 记忆注入）
        │        ├── model_router.py     模型注册表、client 选择、契约与约束词
        │        ├── tools.py            工具集 + 事务快照/回滚
        │        └── memory.py           SQLite 会话与对话卡片
        ├── history_vector.py    向量历史检索
        ├── palace_tree.py       记忆宫殿：语义主题分叉（MiniLM 嵌入）
        ├── billing.py           平台 Key、余额、充值、流水
        ├── balance_service.py   上游账户余额查询
        ├── knowledge_agent.py   知识库 agent（LangGraph）
        ├── multi_agent_supervisor.py 多智能体编排（LangGraph）
        └── observability.py     日志与统计
```

## 目录结构

```
my-agent/
├── api.py                    FastAPI 入口与全部 HTTP 接口
├── agent_with_memory.py      Agent 主循环（默认模型 deepseek-chat）
├── model_router.py           多模型路由：本地 Ollama + OpenAI 兼容 API、运行时注册、契约/约束词
├── memory.py                 会话与平台卡片持久化（SQLite）
├── history_vector.py         历史对话向量化检索
├── palace_tree.py            记忆宫殿主题分叉
├── tools.py                  工具集 + 写前快照 / 回滚
├── billing.py                平台计费（Key、余额、充值、流水）
├── balance_service.py        上游余额查询
├── knowledge_agent.py        知识库问答 agent
├── multi_agent_supervisor.py 多智能体 supervisor
├── observability.py          可观测性
├── static/index.html         单页前端（多模型大屏对比、记忆宫殿视图）
├── requirements.txt
└── Dockerfile
```

## 快速开始

### 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export DEEPSEEK_API_KEY=sk-xxxx          # 至少配一个可用的文本模型
export TAVILY_API_KEY=tvly-xxxx          # 可选：联网搜索工具
export DATA_DIR=./data                   # 会话、卡片、计费数据目录
python api.py                            # 默认监听 8000
```

打开 <http://127.0.0.1:8000/> 即可使用。

### Docker

```bash
docker build -t my-agent .
docker run -d --name my-agent -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-xxxx \
  -e TAVILY_API_KEY=tvly-xxxx \
  -v "$PWD/data:/app/data" \
  my-agent
```

## 配置（环境变量）

| 变量 | 默认值 | 作用 |
|---|---|---|
| `PORT` | `8000` | HTTP 端口 |
| `DATA_DIR` | `./data` | 会话 / 卡片 / 数据目录 |
| `DEFAULT_MODEL` | `deepseek-chat` | 默认模型 |
| `DEEPSEEK_API_KEY` | — | DeepSeek 文本模型 |
| `OLLAMA_BASE_URL` | `http://192.168.166.128:11434/v1` | 本地 Ollama 的 OpenAI 兼容地址（换机器部署务必覆盖） |
| `TAVILY_API_KEY` | — | `web_search` 工具 |
| `ADMIN_TOKEN` | 空 | 设置后 `/billing` 管理接口需带 `X-Admin-Token` |
| `BILLING_DIR` | `./data/billing` | 计费数据目录 |
| `CONSTRAINT_DIR` / `CONSTRAINT_FILE` | — | 约束词存储位置 |
| `RUNTIME_MODELS_FILE` | — | 运行时注册模型的持久化文件 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` `/api` | 前端页面 / 健康信息 |
| GET | `/models` | 已注册模型列表 |
| POST | `/models/register` | 运行时注册 OpenAI 兼容模型（含视觉标记） |
| DELETE | `/models/{model_id}` | 删除运行时模型 |
| GET/POST | `/models/constraints` | 查看 / 设置约束词 |
| GET | `/models/balance`、`/models/balance/{model_id}` | 上游账户余额 |
| POST/GET | `/billing/keys` | 签发 / 列出平台 Key |
| POST | `/billing/recharge`、`/billing/keys/disable` | 充值 / 停用 Key |
| GET | `/billing/balance`、`/billing/logs` | 余额 / 用量流水 |
| POST | `/chat`、`/chat/stream` | 单模型对话（同步 / 流式） |
| POST | `/chat/fuse/stream` | 多模型融合：2~4 个模型 + 裁判模型（流式） |
| POST | `/reset` | 重置会话 |
| GET | `/stats`、POST `/report` | 统计 / 运行报告 |
| POST | `/knowledge` | 知识库问答 |
| POST/GET/DELETE | `/memory/chats`、`/memory/chats/{chat_id}` | 对话卡片增删查 |
| GET | `/memory/palace_tree` | 记忆宫殿主题树 |

## 设计要点

- **模型只认名字**：Agent 侧只传模型名，由 `model_router` 决定用哪个 client / base_url，因此本地与云端模型可以在同一套 Agent 逻辑里切换。
- **契约与约束词分离**：项目契约（接口对齐）对所有模型生效；约束词只影响业务口径，不动契约，避免多模型协作时各写各的。
- **记忆宫殿是纯后处理**：卡片先落库，再按「短句挂上句 / 语义相似挂旧卡 / 否则新开话题」建树；阈值默认 `0.70`、短句阈值 10 字，可随数据量调整。
- **写文件前先快照**：`tools.py` 的 `txn_*` 会在写入前保存快照，支持整任务回滚，便于放开 Agent 的文件写权限。
- **计费前置校验**：`/chat*` 先校验平台 Key 与余额，再调用模型，按上游返回的 usage 扣费并落流水。

## 说明与限制

- 本地模型默认地址指向一台内网 Ollama，部署到别处请设置 `OLLAMA_BASE_URL`。
- 余额查询只实现对 DeepSeek 与 SiliconFlow 的官方接口，其余 provider 返回「不支持」，不做猜测性探测。
- `data/`、`*.db`、`.env` 已在 `.gitignore` 中排除，运行时数据与密钥不会入库。

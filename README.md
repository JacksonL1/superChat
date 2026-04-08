# OpenClaw-PY / SuperChat

一个基于 **FastAPI + Async OpenAI 协议 + 多 Agent 会话编排** 的智能对话系统。

这个项目支持：
- 面向用户的 HTTP / SSE 对话接口；
- 基于 `session_id` 的会话隔离与持久化；
- 主 Agent + 子 Agent（planner / knowledge / executor）协作；
- Skill 目录自动发现、按需加载；
- 飞书机器人（Lark）长连接接入。

---

## 1. 核心能力

- **会话隔离与并发运行**：每个 session 对应一个独立 `AgentLoop` 任务，互不干扰。  
- **流式输出**：通过 `GET /stream/{session_id}` 订阅 SSE，实时接收执行进度与最终答案。  
- **同步接口**：`POST /chat/sync` 可直接等待最终回复，便于机器人类调用方使用。  
- **多 Agent 协作**：主 Agent 负责调度，子 Agent 分工执行（规划、知识检索、命令执行）。  
- **SQLite 持久化**：会话、消息历史、工作区文件（TODO/NOTES/ERRORS）和 agent 路由记录全部落库。  
- **工具安全控制**：`bash` 工具默认白名单命令 + 黑名单片段校验，并限制 shell 操作符。  
- **Skill 机制**：自动扫描 `skills/*/SKILL.md` 并注入模型上下文，支持读取 skill 详情和脚本路径。  
- **飞书 Bot 集成**：使用飞书卡片展示“处理中进度 + 最终回复”，减少长任务超时问题。

---

## 2. 系统架构（按代码实际实现）

```text
用户/客户端
   ├─ CLI (cli.py)
   ├─ HTTP API 调用方
   └─ 飞书机器人 (lark_bot/)
          ↓
    Gateway (gateway/main.py)
          ↓
  SessionManager (gateway/session_manager.py)
          ↓
    AgentLoop (agent/loop.py)
          ↓
 tools/executor + skills/loader + MessageBus
          ↓
       SQLite (store/*.py)
```

### 角色分工

- **main**：用户对话入口与任务编排。  
- **planner**：把复杂任务拆成结构化步骤。  
- **knowledge**：读取 skill / 文件 /工作区并提炼知识。  
- **executor**：执行 bash 与落地操作，返回真实结果。

> 其中 `main` 的工具权限被限制为“协调型工具”，执行类工具需委派给子 Agent，避免主循环混杂执行细节。

---

## 3. 目录结构

```text
.
├── cli.py                    # 命令行入口（chat/sessions/history/reset/serve）
├── config.py                 # 主服务配置（SGLang/Skills/SQLite/Bash 策略）
├── agent/
│   ├── loop.py               # Agent 主循环与 LLM 工具调用循环
│   ├── executor.py           # 工具执行入口（bash/read_file/workspace）
│   ├── prompts.py            # 各角色 system prompt
│   └── tools.py              # 工具 JSON Schema
├── gateway/
│   ├── main.py               # FastAPI 路由（/chat /chat/sync /stream ...）
│   └── session_manager.py    # session 生命周期与 SSE 广播管理
├── messaging/
│   ├── bus.py                # Agent 间消息总线
│   └── protocol.py           # 消息协议/标志位定义
├── store/
│   ├── db.py                 # SQLite 初始化、连接上下文、建表
│   ├── session_store.py      # 会话与消息历史持久化
│   └── workspace.py          # TODO/NOTES/SUMMARY/ERRORS 虚拟工作区
├── skills/
│   ├── loader.py             # 扫描和加载 SKILL.md
│   ├── memory.py             # Skill 成功命令记忆
│   └── */SKILL.md            # 各技能文档与 scripts
└── lark_bot/
    ├── bot.py                # 飞书事件处理 + 卡片更新
    ├── superchat_client.py   # 对 Gateway 的 SSE 调用封装
    └── config.py             # 飞书侧配置
```

---

## 4. 环境要求

- Python **3.10+**（建议 3.11）
- 可访问的 OpenAI 兼容接口（SGLang / 网关等）
- SQLite（Python 内置驱动即可）

安装依赖：

```bash
pip install -r requirements.txt
```

> 若仅运行飞书 Bot，需额外安装 `lark_bot/requirements.txt`。

---

## 5. 配置说明

项目使用 `pydantic-settings`，默认读取根目录 `.env`。

### 5.1 主服务配置（`config.py`）

```env
# ===== LLM 网关 =====
SGLANG_BASE_URL=http://localhost:8000/v1
SGLANG_MODEL=Qwen
SGLANG_API_KEY=
MODELSCOPE_API_TOKEN=
SGLANG_HEADERS={"Content-Type":"application/json"}

# ===== Skills =====
SKILLS_DIR=./skills

# ===== SQLite =====
DB_PATH=./data/superChat.db

# ===== Agent =====
MAX_TOOL_ROUNDS=15

# ===== Bash 工具策略 =====
BASH_ALLOWED_COMMANDS=python,python3,pip,pip3,uv,pytest,ls,pwd,cat,head,tail,sed,awk,rg,find,echo,git,cp,mv,mkdir,touch
BASH_BLOCKED_PATTERNS=rm -rf,shutdown,reboot,poweroff,:(){,mkfs,dd if=,/etc/passwd,chmod 777,> /dev/sda,curl | sh,wget | sh
BASH_ALLOW_SHELL_OPERATORS=false
BASH_WORKSPACE_ROOT=.
```

### 5.2 飞书 Bot 配置（`lark_bot/config.py`）

```env
LARK_APP_ID=
LARK_APP_SECRET=
SUPERCHAT_URL=http://localhost:8000
GROUP_AT_ONLY=true
REQUEST_TIMEOUT=50000
```

---

## 6. 启动方式

### 6.1 启动 Gateway

```bash
python cli.py serve --host 0.0.0.0 --port 8000
```

### 6.2 CLI 调试

```bash
# 发送消息（默认 session=main）
python cli.py chat "你好"

# 指定会话
python cli.py chat "帮我总结这个项目" --session demo

# 查看会话列表
python cli.py sessions

# 查看历史
python cli.py history demo

# 清空历史
python cli.py reset demo
```

### 6.3 启动飞书 Bot

```bash
cd lark_bot
python bot.py
```

---

## 7. API 一览

### 健康检查

```bash
curl http://localhost:8000/health
```

### 异步对话（推荐配合 SSE）

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"main","message":"你好"}'
```

### 同步对话（阻塞直到最终回复）

```bash
curl -X POST http://localhost:8000/chat/sync \
  -H "Content-Type: application/json" \
  -d '{"session_id":"main","message":"你好","timeout":120}'
```

### 订阅 SSE

```bash
curl -N http://localhost:8000/stream/main
```

### 会话与历史

```bash
curl http://localhost:8000/sessions
curl http://localhost:8000/sessions/main/history
curl -X POST http://localhost:8000/sessions/main/reset
```

---

## 8. 数据持久化设计

SQLite 主要表：
- `sessions`：会话元信息与状态；
- `messages`：完整对话消息（含 tool_calls）；
- `workspace`：每个 session 的 TODO / NOTES / SUMMARY / ERRORS；
- `agent_messages`：Agent 间消息审计记录；
- `skill_memory`：Skill 成功命令记忆。

`store/session_store.py` 使用 **每 session 单写连接 + 队列串行写入**，显著降低并发场景下 SQLite 锁冲突。

---

## 9. Skills 开发与接入

每个 skill 建议结构：

```text
skills/my-skill/
├── SKILL.md
└── scripts/
    └── run.py
```

说明：
1. `SKILL.md` 作为技能说明入口；
2. `skills/loader.py` 会自动扫描并把技能元信息注入上下文；
3. Agent 可通过 `load_skill`、`list_skill_files`、`read_file` 使用技能内容；
4. `skills/memory.py` 会记录成功命令，供后续同类任务复用。

---

## 10. 常见问题排查

- **Gateway 启动即报模型请求错误**：检查 `SGLANG_BASE_URL`、模型名与网关鉴权。  
- **没有实时输出**：确认先建立 SSE 订阅，再发送 `/chat`。  
- **bash 命令被拒绝**：检查是否命中白名单限制或黑名单片段。  
- **SQLite database is locked**：确认是否使用本项目提供的写入路径，避免外部并发直写。  
- **飞书群里机器人无响应**：检查 `GROUP_AT_ONLY=true` 时是否正确 @ 机器人。

---

## 11. 开发建议

- 新增工具时：同步更新 `agent/tools.py`（schema）与 `agent/executor.py`（实现）。
- 新增 Agent 角色时：同步更新 `agent/prompts.py` 与 `agent/loop.py` 的工具白名单。
- 优先通过 `gateway/session_manager.py` 管理 session 生命周期，避免绕过管理器直接实例化 loop。

---

## 12. License

MIT

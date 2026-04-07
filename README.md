# SuperChat

SGLang + ClawHub Skills，支持基于 session_id 隔离的智能对话系统。

## 功能特性

- **会话隔离**：基于 session_id 的独立 Agent 会话
- **工具调用**：支持执行各种技能脚本
- **Gateway 服务**：提供 HTTP API 和 SSE 流式输出
- **飞书机器人集成**：支持飞书消息卡片
- **技能库**：内置多种实用技能
- **持久化存储**：使用 SQLite 存储会话和消息历史

## 项目结构

```
SuperChat/
├── agent/           # Agent 执行引擎
├── gateway/         # Gateway 服务（HTTP API + SSE）
├── store/           # 数据存储（SQLite）
├── skills/          # 技能库
├── lark_bot/        # 飞书机器人
├── messaging/       # 消息传递
├── store/session_store.py  # 会话与消息持久化
├── config.py        # 配置管理
├── cli.py           # 命令行工具
└── .env             # 环境变量
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并填写配置：

```env
# SGLang
SGLANG_BASE_URL=http://localhost:8000/v1
SGLANG_MODEL=default
# 需要鉴权时填写（优先）
SGLANG_API_KEY=
# 兼容 ModelScope Token（SGLANG_API_KEY 留空时生效）
MODELSCOPE_API_TOKEN=
SGLANG_HEADERS={"Content-Type": "application/json"}

# Skills
SKILLS_DIR=./skills

# SQLite
DB_PATH=./data/openclaw.db
```

### 3. 启动服务

#### 启动 Gateway 服务

```bash
python cli.py serve
```

### 4. 使用 API

#### 健康检查

```bash
curl http://localhost:8000/health
```

#### 发送消息

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "main", "message": "你好"}'
```

### 5. 使用命令行工具

#### 发送消息

```bash
python cli.py chat "你好"
```

#### 列出所有会话

```bash
python cli.py sessions
```

#### 查看消息历史

```bash
python cli.py history [session_id]
```

#### 重置会话历史

```bash
python cli.py reset [session_id]
```

## 技能库

项目内置多种技能，位于 `skills/` 目录：

- **1password**：密码管理
- **agent-browser**：浏览器操作
- **coding-agent**：代码生成
- **elite-longterm-memory**：长期记忆
- **gifgrep**：GIF 搜索
- **healthcheck**：健康检查
- **mcporter**：Minecraft 相关
- **nano-pdf**：PDF 处理
- **openai-whisper**：语音转文字
- **playwright**：浏览器自动化
- **session-logs**：会话日志
- **sherpa-onnx-tts**：文本转语音
- **skill-vetter**：技能审查
- **summarize**：文本摘要
- **video-frames**：视频帧提取
- **weather**：天气查询

## 飞书机器人

项目集成了飞书机器人，位于 `lark_bot/` 目录。配置飞书机器人后，可以通过飞书消息与系统交互。

## 配置说明

### 主要配置项

- **SGLANG_BASE_URL**：SGLang API 基础 URL
- **SGLANG_MODEL**：使用的模型名称
- **SGLANG_API_KEY**：网关鉴权 Token（优先）
- **MODELSCOPE_API_TOKEN**：ModelScope Token（兼容字段）
- **SGLANG_HEADERS**：API 请求头（JSON 格式）
- **SKILLS_DIR**：技能目录路径
- **DB_PATH**：SQLite 数据库路径

### 环境变量

所有配置项都可以通过环境变量覆盖，格式为大写蛇形命名，例如 `SGLANG_BASE_URL`。

## 开发指南

### 目录结构说明

- **agent/**：Agent 执行引擎，处理工具调用和 LLM 交互
- **gateway/**：Gateway 服务，提供 HTTP API 和 SSE 流式输出
- **store/**：数据存储，使用 SQLite 持久化数据（会话与消息由 `store/session_store.py` 负责）
- **skills/**：技能库，包含各种可执行技能
- **lark_bot/**：飞书机器人，处理飞书消息交互
- **messaging/**：消息传递，实现 Agent 间通信

### 添加新技能

1. 在 `skills/` 目录下创建新技能目录
2. 创建 `SKILL.md` 文件，描述技能功能和使用方法
3. 添加 `scripts/` 目录，包含可执行脚本
4. 技能会自动被系统发现和加载

## 故障排查

### 常见问题

1. **ModuleNotFoundError**：缺少依赖包，运行 `pip install -r requirements.txt`
2. **SQLite 数据库错误**：检查 `data/` 目录权限
3. **SGLang API 错误**：检查 `SGLANG_BASE_URL` 和 `SGLANG_HEADERS` 配置
4. **Gateway 服务启动失败**：检查端口是否被占用

### 日志

- 系统日志输出到控制台
- 数据库操作日志可以通过设置 `logging` 级别查看

## 许可证

MIT

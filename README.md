# Coding Partner

飞书 Vibe Coding 机器人 — 通过手机飞书与 Claude Code 或 Codex 协作开发。

## 快速开始

```bash
# 克隆项目
git clone <repo-url> && cd coding-partner

# 一键安装 — 交互式引导配置 + 选择部署方式 (systemd / Docker)
./scripts/setup.sh
```

安装脚本会自动完成：
1. 创建 `.env` 并引导填写飞书凭证
2. 选择部署方式 — **systemd**（推荐 Linux 服务器）或 **Docker**
3. 检查依赖是否就绪
4. 构建并启动服务

### 前置条件

| 部署方式 | 需要 |
|----------|------|
| **systemd** | Python 3.11+, [uv](https://docs.astral.sh/uv/), Claude CLI 或 Codex CLI, Git, `script`（仅 Claude 流式模式需要） |
| **Docker** | Docker, Docker Compose |

两种方式都需要：飞书开放平台应用（WebSocket 长连接 + 机器人能力）

### 手动运行（开发调试）

```bash
cp .env.example .env  # 填入 FEISHU_APP_ID、FEISHU_APP_SECRET
uv run python -m coding_partner.main
```

## 使用方法

1. 在飞书中找到机器人，私聊发送 `/repo` 选择项目
2. 直接发送需求描述，机器人会自动创建 worktree + 开发群
3. 在开发群中继续对话

### 群聊命令

| 命令 | 说明 |
|------|------|
| `/new` | 重置当前 Agent 会话上下文 |
| `/cancel` | 终止当前正在执行的任务 |
| `/done` | 清理 worktree、删除开发群 |

### 私聊命令

| 命令 | 说明 |
|------|------|
| `/repo` | 选择/切换项目仓库 |
| `/cli` | 选择后续新会话使用 Claude 或 Codex |
| `/start` | 在当前分支直接创建开发群（不创建 worktree） |
| 直接发文字 | 自动创建 worktree + 开发群 + 首次 Agent 执行 |

## 配置项

所有配置通过环境变量或 `.env` 文件设置，详见 [.env.example](.env.example)。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FEISHU_APP_ID` | (必填) | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | (必填) | 飞书应用 App Secret |
| `BOT_OPEN_ID` | `""` | 机器人自身 open_id，用于过滤自己的消息 |
| `REPO_BASE_PATH` | (必填) | Git 仓库扫描根路径 |
| `DB_PATH` | `./data/coding_partner.db` | SQLite 数据库路径 |
| `GROUP_NAME_PREFIX` | `""` | 新建开发群名称前缀，例如 `[研发机器人]` |
| `AGENT_PROVIDER` | `claude` | 使用的 Agent：`claude` 或 `codex` |
| `CLAUDE_CLI` | `claude` | Claude CLI 命令路径 |
| `CODEX_CLI` | `codex` | Codex CLI 命令路径 |
| `CODEX_MODEL` | `""` | 可选的 Codex 模型覆盖 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `AGENT_TIMEOUT` | `""` | 通用 Agent 执行超时（秒，优先于 `CLAUDE_TIMEOUT`） |
| `CLAUDE_TIMEOUT` | `1800` | Claude 兼容超时配置（未设置 `AGENT_TIMEOUT` 时生效） |
| `BRANCH_NAME_MODEL` | `haiku` | 生成分支名使用的模型 |
| `STREAM_COOLDOWN` | `3.0` | 卡片更新冷却时间（秒） |
| `CARD_STREAMING_MAX_LEN` | `2000` | 流式卡片最大显示字符数 |
| `CARD_RESULT_MAX_LEN` | `3000` | 结果卡片最大显示字符数 |
| `TOOL_ACTIVITY_LIMIT` | `8` | 保留的最近工具活动条数 |
| `SEEN_MESSAGE_MAX_AGE` | `3600` | 消息去重记录保留时间（秒） |
| `CLEANUP_INTERVAL` | `600` | 去重记录清理间隔（秒） |
| `REPO_SCAN_MAX_DEPTH` | `5` | 仓库扫描最大目录深度 |

## 架构

```
飞书 WebSocket ──→ main.py (事件分发)
                     ├── handlers/dm.py    (私聊: /repo, /start, 需求)
                     └── handlers/group.py (群聊: 消息队列 → Agent)
                           └── services/agent_runner.py  (按 provider 分发到 Claude / Codex)
```

- 飞书 SDK 回调是同步的，通过 `run_coroutine_threadsafe` 桥接到 asyncio 事件循环
- 每个开发群有独立的消息队列，顺序执行 Agent 任务
- SQLite 存储用户上下文和群聊绑定关系

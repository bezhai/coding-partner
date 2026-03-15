# Coding Partner

飞书 Vibe Coding 机器人 — 通过手机飞书与 Claude Code 协作开发。

## 启动

```bash
uv run python -m coding_partner.main
```

## 架构

- WebSocket 长连接接收飞书事件
- 私聊：/repo 选择项目 → 发送需求 → 自动创建 worktree + 开发群
- 群聊：直接对话 Claude Code → /new 重置会话 → /done 归档清理

## 约定

- 用 `asyncio` 处理并发，飞书 SDK 回调是同步的，通过 `run_coroutine_threadsafe` 桥接
- SQLite 存储 user_context 和 chat_binding
- 每个群聊一个 asyncio.Lock 防止并发执行

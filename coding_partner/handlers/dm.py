"""Private chat (DM) message handler.

State machine:
  - /repo → scan repos → send select card
  - Card callback (select_repo) → save user context → confirm
  - Natural language requirement → create worktree + group + first Claude run
"""

import asyncio
import logging
import time
from pathlib import Path

from coding_partner import feishu_client, formatter, store
from coding_partner.config import settings
from coding_partner.services import claude_runner, group_manager, worktree
from coding_partner.services.claude_runner import (
    StreamDelta,
    StreamQuestion,
    StreamResult,
    StreamToolUse,
)

logger = logging.getLogger(__name__)

# Pending confirmations: user_open_id -> {requirement, repo_path, chat_id}
_pending_confirms: dict[str, dict] = {}


async def handle_dm_message(
    message_id: str,
    user_open_id: str,
    text: str,
    chat_id: str,
) -> None:
    """Handle a private chat text message."""
    text = text.strip()

    try:
        if text == "/repo":
            await _handle_repo_command(message_id, user_open_id, chat_id)
        elif text == "/start":
            await _handle_start(message_id, user_open_id, chat_id)
        else:
            await _handle_requirement(message_id, user_open_id, text, chat_id)
    except Exception:
        logger.exception("DM handler error")


async def _handle_repo_command(message_id: str, user_open_id: str, chat_id: str) -> None:
    """Scan repos and send a select card."""
    from coding_partner.services.repo_scanner import scan_repos

    repos = scan_repos(settings.repo_base)

    if not repos:
        feishu_client.reply_text(message_id, f"在 {settings.repo_base} 下未找到 git 仓库")
        return

    card = formatter.build_repo_select_card(repos)
    feishu_client.reply_card(message_id, card)


async def _handle_start(message_id: str, user_open_id: str, chat_id: str) -> None:
    """Start a dev group directly on the repo without worktree."""
    repo_path = await store.get_user_repo(user_open_id)

    if not repo_path:
        feishu_client.reply_text(message_id, "请先使用 /repo 选择项目")
        return

    repo_name = Path(repo_path).name
    feishu_client.reply_text(message_id, f"正在为 {repo_name} 创建开发群...")

    try:
        # Get current branch name
        proc = await asyncio.create_subprocess_exec(
            "git", "branch", "--show-current",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        branch = stdout.decode().strip() or "main"

        group_chat_id = group_manager.create_dev_group(user_open_id, branch, repo_name)
        if not group_chat_id:
            feishu_client.reply_text(message_id, "创建开发群失败")
            return

        await store.create_binding(
            chat_id=group_chat_id,
            worktree_path=repo_path,
            repo_path=repo_path,
            branch_name=branch,
            user_id=user_open_id,
        )

        setup_card = formatter.build_setup_card(repo_name, branch, group_chat_id)
        feishu_client.send_card(group_chat_id, setup_card)

        feishu_client.reply_text(
            message_id,
            f"已创建开发群 {repo_name} | {branch}\n直接在群里发消息即可",
        )
    except Exception as e:
        logger.exception("Failed to handle /start")
        feishu_client.reply_text(message_id, f"创建失败: {e}")


async def _handle_requirement(
    message_id: str,
    user_open_id: str,
    requirement: str,
    chat_id: str,
) -> None:
    """User sent a requirement → send confirmation card, wait for approval."""
    repo_path = await store.get_user_repo(user_open_id)

    if not repo_path:
        feishu_client.reply_text(message_id, "请先使用 /repo 选择项目")
        return

    repo_name = Path(repo_path).name

    # Stash pending requirement and send confirmation card
    _pending_confirms[user_open_id] = {
        "requirement": requirement,
        "repo_path": repo_path,
        "chat_id": chat_id,
    }
    card = formatter.build_confirm_card(repo_name, requirement)
    feishu_client.reply_card(message_id, card)


async def _execute_requirement(user_open_id: str, pending: dict) -> None:
    """Actually create worktree + dev group + first Claude run (after user confirmation)."""
    requirement = pending["requirement"]
    repo_path = pending["repo_path"]
    chat_id = pending["chat_id"]

    try:
        # 1. Create worktree with AI branch name
        wt_info = await worktree.create_worktree(repo_path, requirement)

        # 2. Create Feishu dev group
        repo_name = Path(repo_path).name
        group_chat_id = group_manager.create_dev_group(user_open_id, wt_info.branch_name, repo_name)
        if not group_chat_id:
            feishu_client.send_text(chat_id, "创建开发群失败")
            return

        # 3. Save binding
        await store.create_binding(
            chat_id=group_chat_id,
            worktree_path=wt_info.path,
            repo_path=repo_path,
            branch_name=wt_info.branch_name,
            user_id=user_open_id,
        )

        # 4. Send setup card to the dev group
        setup_card = formatter.build_setup_card(repo_name, wt_info.branch_name, group_chat_id)
        feishu_client.send_card(group_chat_id, setup_card)

        # 5. Confirm in DM
        feishu_client.send_text(
            chat_id,
            f"已创建开发群 🔧 {repo_name} | {wt_info.branch_name}\n请到群里继续对话",
        )

        # 6. Run first Claude execution in the dev group
        asyncio.create_task(_run_first_claude(group_chat_id, wt_info.path, requirement))

    except Exception as e:
        logger.exception("Failed to execute requirement")
        feishu_client.send_text(chat_id, f"创建开发环境失败: {e}")


async def _run_first_claude(chat_id: str, worktree_path: str, requirement: str) -> None:
    """Run the first Claude execution in the dev group with streaming."""
    try:
        # Send thinking card
        thinking_card = formatter.build_thinking_card(requirement)
        card_msg_id = feishu_client.send_card(chat_id, thinking_card)

        accumulated = ""
        tool_activities: list[str] = []
        last_update_time = time.monotonic()
        need_update = False

        async for event in claude_runner.run_stream(
            prompt=requirement, cwd=worktree_path
        ):
            if isinstance(event, StreamDelta):
                accumulated += event.text
                need_update = True

            elif isinstance(event, StreamToolUse):
                tool_activities.append(event.summary)
                if len(tool_activities) > settings.tool_activity_limit:
                    tool_activities = tool_activities[-settings.tool_activity_limit :]
                need_update = True

            elif isinstance(event, StreamQuestion):
                question_card = formatter.build_question_card(
                    event.question, event.options, chat_id
                )
                feishu_client.send_card(chat_id, question_card)

            elif isinstance(event, StreamResult):
                result = event.result

                if result.session_id:
                    await store.update_session_id(chat_id, result.session_id)

                result_card = formatter.build_result_card(
                    result.result,
                    cost=result.cost,
                    duration=result.duration,
                    is_error=result.is_error,
                )
                if card_msg_id:
                    feishu_client.update_card(card_msg_id, result_card)
                else:
                    feishu_client.send_card(chat_id, result_card)

            if need_update:
                now = time.monotonic()
                if now - last_update_time >= settings.stream_cooldown:
                    if card_msg_id:
                        streaming_card = formatter.build_streaming_card(
                            requirement, accumulated, tool_activities
                        )
                        feishu_client.update_card(card_msg_id, streaming_card)
                    last_update_time = now
                    need_update = False
    except Exception:
        logger.exception("First Claude run failed")
        feishu_client.send_text(chat_id, "首次 Claude 执行失败，请在群里重新发送需求")


async def handle_card_action(action: dict, user_open_id: str) -> dict | None:
    """Handle card action callback (repo selection, confirm requirement, answer question)."""
    value = action.get("value", {})
    act = value.get("action")

    if act == "select_repo":
        selected_option = action.get("option")
        if not selected_option:
            return None

        repo_path = selected_option
        await store.set_user_repo(user_open_id, repo_path)

        repo_name = Path(repo_path).name
        return {
            "toast": {
                "type": "success",
                "content": f"已选择项目: {repo_name}，现在可以直接发送需求",
            }
        }

    if act == "confirm_requirement":
        pending = _pending_confirms.pop(user_open_id, None)
        if not pending:
            return {"toast": {"type": "info", "content": "该确认已过期，请重新发送需求"}}

        confirmed = value.get("confirm") == "yes"
        if confirmed:
            asyncio.create_task(_execute_requirement(user_open_id, pending))
            return {"toast": {"type": "success", "content": "开始执行，正在创建开发环境..."}}
        else:
            return {"toast": {"type": "info", "content": "已取消"}}

    if act == "answer_question":
        answer = value.get("answer", "")
        target_chat_id = value.get("chat_id", "")
        if answer and target_chat_id:
            from coding_partner.handlers import group

            # Enqueue the answer as a new message to the dev group
            await store.enqueue_message(target_chat_id, "", answer)
            group.ensure_worker(target_chat_id)
            return {"toast": {"type": "success", "content": f"已回复: {answer}"}}

    if act == "approve_plan":
        target_chat_id = value.get("chat_id", "")
        approved = value.get("approve") == "yes"
        if target_chat_id:
            from coding_partner.handlers import group

            asyncio.create_task(group.handle_plan_approval(target_chat_id, approved))
            msg = "方案��批准，开始执行..." if approved else "已拒绝方案"
            return {"toast": {"type": "success" if approved else "info", "content": msg}}

    return None

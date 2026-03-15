"""Private chat (DM) message handler.

State machine:
  - /repo → scan repos → send select card
  - Card callback (select_repo) → save user context → confirm
  - Natural language requirement → create worktree + group + first Claude run
"""

import asyncio
import logging
from pathlib import Path

from coding_partner import feishu_client, formatter, store
from coding_partner.config import settings
from coding_partner.services import claude_runner, group_manager, worktree

logger = logging.getLogger(__name__)


async def handle_dm_message(
    message_id: str,
    user_open_id: str,
    text: str,
    chat_id: str,
) -> None:
    """Handle a private chat text message."""
    text = text.strip()

    if text == "/repo":
        await _handle_repo_command(message_id, user_open_id, chat_id)
    else:
        await _handle_requirement(message_id, user_open_id, text, chat_id)


async def _handle_repo_command(message_id: str, user_open_id: str, chat_id: str) -> None:
    """Scan repos and send a select card."""
    from coding_partner.services.repo_scanner import scan_repos

    repos = scan_repos(settings.repo_base)

    if not repos:
        feishu_client.reply_text(message_id, f"在 {settings.repo_base} 下未找到 git 仓库")
        return

    card = formatter.build_repo_select_card(repos)
    feishu_client.reply_card(message_id, card)


async def _handle_requirement(
    message_id: str,
    user_open_id: str,
    requirement: str,
    chat_id: str,
) -> None:
    """User sent a requirement → create worktree + dev group + first Claude run."""
    repo_path = await store.get_user_repo(user_open_id)

    if not repo_path:
        feishu_client.reply_text(message_id, "请先使用 /repo 选择项目")
        return

    # Send a "working on it" reply
    feishu_client.reply_text(message_id, "收到需求，正在创建开发环境...")

    try:
        # 1. Create worktree with AI branch name
        wt_info = await worktree.create_worktree(repo_path, requirement)

        # 2. Create Feishu dev group
        repo_name = Path(repo_path).name
        group_chat_id = group_manager.create_dev_group(user_open_id, wt_info.branch_name, repo_name)
        if not group_chat_id:
            feishu_client.reply_text(message_id, "创建开发群失败")
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
        feishu_client.reply_text(
            message_id,
            f"已创建开发群 🔧 {repo_name} | {wt_info.branch_name}\n请到群里继续对话",
        )

        # 6. Run first Claude execution in the dev group
        asyncio.create_task(_run_first_claude(group_chat_id, wt_info.path, requirement))

    except Exception as e:
        logger.exception("Failed to handle requirement")
        feishu_client.reply_text(message_id, f"创建开发环境失败: {e}")


async def _run_first_claude(chat_id: str, worktree_path: str, requirement: str) -> None:
    """Run the first Claude execution in the dev group."""
    # Send thinking card
    thinking_card = formatter.build_thinking_card(requirement)
    card_msg_id = feishu_client.send_card(chat_id, thinking_card)

    # Run Claude
    result = await claude_runner.run(prompt=requirement, cwd=worktree_path)

    # Update session
    if result.session_id:
        await store.update_session_id(chat_id, result.session_id)

    # Replace thinking card with result
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


async def handle_card_action(action: dict, user_open_id: str) -> dict | None:
    """Handle card action callback (repo selection)."""
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

    return None

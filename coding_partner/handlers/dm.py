"""Private chat (DM) message handler.

State machine:
  - /repo → scan repos → send select card
  - Card callback (select_repo) → save user context → confirm
  - Natural language requirement → create worktree + group + first agent run
"""

import asyncio
import logging
from pathlib import Path

from coding_partner import feishu_client, formatter, store
from coding_partner.config import settings
from coding_partner.services import group_manager, worktree
from coding_partner.services.agent_runner import provider_display_name

logger = logging.getLogger(__name__)

# Pending confirmations: user_open_id -> {requirement, repo_path, chat_id}
_pending_confirms: dict[str, dict] = {}


async def handle_dm_message(
    message_id: str,
    user_open_id: str,
    text: str | None,
    chat_id: str,
    image_key: str | None = None,
) -> None:
    """Handle a private chat message."""
    if text:
        text = text.strip()

    try:
        if image_key and not text:
            agent_name = provider_display_name(await store.get_user_agent_provider(user_open_id))
            await feishu_client.async_reply_text(
                message_id,
                f"图片消息请在开发群中发送，{agent_name} 可以直接查看图片并回应",
            )
            return

        if not text:
            return

        if text == "/repo":
            await _handle_repo_command(message_id, user_open_id, chat_id)
        elif text == "/cli":
            await _handle_cli_command(message_id, user_open_id)
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
        await feishu_client.async_reply_text(message_id, f"在 {settings.repo_base} 下未找到 git 仓库")
        return

    card = formatter.build_repo_select_card(repos)
    await feishu_client.async_reply_card(message_id, card)


async def _handle_cli_command(message_id: str, user_open_id: str) -> None:
    """Show agent selection card for future sessions."""
    current_provider = await store.get_user_agent_provider(user_open_id)
    card = formatter.build_cli_select_card(current_provider)
    await feishu_client.async_reply_card(message_id, card)


async def _handle_start(message_id: str, user_open_id: str, chat_id: str) -> None:
    """Start a dev group directly on the repo without worktree."""
    repo_path = await store.get_user_repo(user_open_id)

    if not repo_path:
        await feishu_client.async_reply_text(message_id, "请先使用 /repo 选择项目")
        return

    repo_name = Path(repo_path).name
    agent_provider = await store.get_user_agent_provider(user_open_id)
    agent_name = provider_display_name(agent_provider)
    await feishu_client.async_reply_text(message_id, f"正在为 {repo_name} 创建开发群...")

    try:
        # Get current branch name
        proc = await asyncio.create_subprocess_exec(
            "git", "branch", "--show-current",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        branch = stdout.decode().strip() or "main"

        group_chat_id = await asyncio.to_thread(
            group_manager.create_dev_group, user_open_id, branch, repo_name
        )
        if not group_chat_id:
            await feishu_client.async_reply_text(message_id, "创建开发群失败")
            return

        await store.create_binding(
            chat_id=group_chat_id,
            worktree_path=repo_path,
            repo_path=repo_path,
            branch_name=branch,
            user_id=user_open_id,
            agent_provider=agent_provider,
        )

        setup_card = formatter.build_setup_card(
            repo_name, branch, group_chat_id, agent_name=agent_name
        )
        await feishu_client.async_send_card(group_chat_id, setup_card)

        await feishu_client.async_reply_text(
            message_id,
            f"已创建开发群 {repo_name} | {branch}\n直接在群里发消息即可",
        )
    except Exception as e:
        logger.exception("Failed to handle /start")
        await feishu_client.async_reply_text(message_id, f"创建失败: {e}")


async def _handle_requirement(
    message_id: str,
    user_open_id: str,
    requirement: str,
    chat_id: str,
) -> None:
    """User sent a requirement → send confirmation card, wait for approval."""
    repo_path = await store.get_user_repo(user_open_id)

    if not repo_path:
        await feishu_client.async_reply_text(message_id, "请先使用 /repo 选择项目")
        return

    repo_name = Path(repo_path).name

    # Stash pending requirement and send confirmation card
    _pending_confirms[user_open_id] = {
        "requirement": requirement,
        "repo_path": repo_path,
        "chat_id": chat_id,
    }
    card = formatter.build_confirm_card(repo_name, requirement)
    await feishu_client.async_reply_card(message_id, card)


async def _execute_requirement(user_open_id: str, pending: dict) -> None:
    """Actually create worktree + dev group + first agent run (after user confirmation)."""
    requirement = pending["requirement"]
    repo_path = pending["repo_path"]
    chat_id = pending["chat_id"]

    try:
        # 1. Create worktree with AI branch name
        wt_info = await worktree.create_worktree(repo_path, requirement)

        # 2. Create Feishu dev group
        repo_name = Path(repo_path).name
        group_chat_id = await asyncio.to_thread(
            group_manager.create_dev_group, user_open_id, wt_info.branch_name, repo_name
        )
        if not group_chat_id:
            await feishu_client.async_send_text(chat_id, "创建开发群失败")
            return

        # 3. Save binding
        await store.create_binding(
            chat_id=group_chat_id,
            worktree_path=wt_info.path,
            repo_path=repo_path,
            branch_name=wt_info.branch_name,
            user_id=user_open_id,
            agent_provider=await store.get_user_agent_provider(user_open_id),
        )

        # 4. Send setup card to the dev group
        agent_provider = await store.get_user_agent_provider(user_open_id)
        agent_name = provider_display_name(agent_provider)
        setup_card = formatter.build_setup_card(
            repo_name,
            wt_info.branch_name,
            group_chat_id,
            agent_name=agent_name,
        )
        await feishu_client.async_send_card(group_chat_id, setup_card)

        # 5. Confirm in DM
        await feishu_client.async_send_text(
            chat_id,
            f"已创建开发群 🔧 {repo_name} | {wt_info.branch_name}\n请到群里继续对话",
        )

        # 6. Enqueue first agent execution through the queue (avoids concurrent
        #    claude -p if the user sends a message before this finishes)
        from coding_partner import worker_manager

        await store.enqueue_message(group_chat_id, "", requirement)
        await worker_manager.ensure_worker(group_chat_id)

    except Exception as e:
        logger.exception("Failed to execute requirement")
        await feishu_client.async_send_text(chat_id, f"创建开发环境失败: {e}")


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

    if act == "select_cli":
        selected_option = action.get("option")
        if selected_option not in {"claude", "codex"}:
            return None

        await store.set_user_agent_provider(user_open_id, selected_option)
        agent_name = provider_display_name(selected_option)
        return {
            "toast": {
                "type": "success",
                "content": f"已切换默认 Agent: {agent_name}",
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
        target_chat_id = value.get("chat_id", "")
        # Form submission (multi-question): collect all dropdown answers
        form_value = action.get("form_value")
        if form_value and target_chat_id:
            parts = []
            for key in sorted(form_value.keys()):
                if key.startswith("q_"):
                    parts.append(form_value[key])
            answer = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(parts))
        else:
            # Single-question button click
            answer = value.get("answer", "")
        if answer and target_chat_id:
            from coding_partner import worker_manager

            # Enqueue the answer as a new message to the dev group
            await store.enqueue_message(target_chat_id, "", answer)
            await worker_manager.ensure_worker(target_chat_id)
            display = answer if len(answer) <= 50 else answer[:50] + "..."
            return {"toast": {"type": "success", "content": f"已回复: {display}"}}

    if act == "approve_plan":
        target_chat_id = value.get("chat_id", "")
        approved = value.get("approve") == "yes"
        if target_chat_id:
            from coding_partner.handlers import group

            asyncio.create_task(group.handle_plan_approval(target_chat_id, approved))
            msg = "方案已批准，开始执行..." if approved else "已拒绝方案"
            return {"toast": {"type": "success" if approved else "info", "content": msg}}

    return None

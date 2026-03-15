"""Feishu interactive card builders."""

from __future__ import annotations

from dataclasses import dataclass

from coding_partner.config import settings


@dataclass
class RepoInfo:
    name: str
    path: str
    branch: str


def _md_element(content: str) -> dict:
    """Build a markdown element (replaces div+lark_md for richer rendering)."""
    return {"tag": "markdown", "content": content}


def build_repo_select_card(repos: list[RepoInfo]) -> dict:
    """Build a card with a dropdown to select a repo."""
    options = [
        {
            "text": {"tag": "plain_text", "content": f"{r.name} ({r.branch})"},
            "value": r.path,
        }
        for r in repos
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择项目"},
            "template": "blue",
        },
        "elements": [
            _md_element("请选择要进行开发的项目仓库："),
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {"tag": "plain_text", "content": "选择项目..."},
                        "options": options,
                        "value": {"action": "select_repo"},
                    }
                ],
            },
        ],
    }


def build_thinking_card(requirement: str) -> dict:
    """Build a placeholder card while Claude is working."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude 正在工作中..."},
            "template": "wathet",
        },
        "elements": [
            _md_element(f"**需求**: {requirement}\n\n⏳ 正在执行，请稍候..."),
        ],
    }


def build_streaming_card(
    requirement: str,
    partial_text: str,
    tool_activities: list[str] | None = None,
) -> dict:
    """Build a card showing streaming progress with tool activity."""
    parts: list[str] = [f"**需求**: {requirement}"]

    if tool_activities:
        activity_lines = "\n".join(tool_activities[-5:])
        parts.append(f"\n**最近操作**:\n{activity_lines}")

    if partial_text:
        display = partial_text
        if len(display) > settings.card_streaming_max_len:
            display = display[-settings.card_streaming_max_len :]
        parts.append(f"\n---\n\n{display}")

    parts.append("\n\n⏳ 执行中...")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude 正在工作中..."},
            "template": "wathet",
        },
        "elements": [_md_element("\n".join(parts))],
    }


def build_plan_approval_card(plan_text: str, chat_id: str) -> dict:
    """Build a card showing Claude's plan and asking for approval to execute."""
    display = plan_text
    if len(display) > settings.card_result_max_len:
        display = display[: settings.card_result_max_len] + "\n\n... (已截断)"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "方案确认"},
            "template": "orange",
        },
        "elements": [
            _md_element(display),
            {"tag": "hr"},
            _md_element("Claude 已完成分析，以上是执行方案。确认后将开始修改代码。"),
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准执行"},
                        "type": "primary",
                        "value": {
                            "action": "approve_plan",
                            "approve": "yes",
                            "chat_id": chat_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "value": {
                            "action": "approve_plan",
                            "approve": "no",
                            "chat_id": chat_id,
                        },
                    },
                ],
            },
        ],
    }


def build_result_card(
    result_text: str,
    *,
    cost: str = "",
    duration: str = "",
    is_error: bool = False,
) -> dict:
    """Build a result card after Claude finishes."""
    template = "red" if is_error else "green"
    title = "执行失败" if is_error else "执行完成"

    footer_parts = []
    if duration:
        footer_parts.append(f"⏱ {duration}")
    if cost:
        footer_parts.append(f"💰 {cost}")
    footer = " | ".join(footer_parts)

    # Truncate very long results for card display
    display_text = result_text
    if len(display_text) > settings.card_result_max_len:
        display_text = display_text[: settings.card_result_max_len] + "\n\n... (已截断)"

    elements: list[dict] = [_md_element(display_text)]

    if footer:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": footer}],
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements,
    }


def build_confirm_card(repo_name: str, requirement: str) -> dict:
    """Build a confirmation card before creating worktree + dev group."""
    # Truncate long requirement for display
    display_req = requirement if len(requirement) <= 200 else requirement[:200] + "..."
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "确认执行"},
            "template": "orange",
        },
        "elements": [
            _md_element(
                f"**项目**: {repo_name}\n"
                f"**需求**: {display_req}\n\n"
                "将创建新分支和开发群，确认开始？"
            ),
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认执行"},
                        "type": "primary",
                        "value": {"action": "confirm_requirement", "confirm": "yes"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "value": {"action": "confirm_requirement", "confirm": "no"},
                    },
                ],
            },
        ],
    }


def build_question_card(question: str, options: list[str], chat_id: str) -> dict:
    """Build an interactive card for Claude's AskUserQuestion."""
    buttons = []
    for i, opt in enumerate(options):
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": opt},
                "type": "primary" if i == 0 else "default",
                "value": {"action": "answer_question", "answer": opt, "chat_id": chat_id},
            }
        )

    elements: list[dict] = [_md_element(f"**Claude 在询问：**\n\n{question}")]
    if buttons:
        elements.append({"tag": "action", "actions": buttons})
    elements.append(
        _md_element("*点击选项或直接回复文字作答*")
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "需要你的输入"},
            "template": "yellow",
        },
        "elements": elements,
    }


def build_setup_card(repo_name: str, branch_name: str, chat_id: str) -> dict:
    """Build a card confirming worktree + group setup."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "开发环境已就绪"},
            "template": "green",
        },
        "elements": [
            _md_element(
                f"**项目**: {repo_name}\n"
                f"**分支**: `{branch_name}`\n\n"
                "已创建开发群，请在群里继续对话。\n"
                "群内直接发消息即可，无需 @机器人。\n\n"
                "**可用命令**:\n"
                "- `/new` — 重置 Claude 会话\n"
                "- `/cancel` — 终止当前任务\n"
                "- `/done` — 提交并归档\n"
                "- `/confirm` — 开启确认模式（修改前需审批）\n"
                "- `/auto` — 切回自动模式"
            ),
        ],
    }

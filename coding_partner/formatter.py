"""Feishu interactive card builders."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RepoInfo:
    name: str
    path: str
    branch: str


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
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "请选择要进行开发的项目仓库："},
            },
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
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**需求**: {requirement}\n\n⏳ 正在执行，请稍候...",
                },
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
    if len(display_text) > 3000:
        display_text = display_text[:3000] + "\n\n... (已截断)"

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": display_text},
        },
    ]

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


def build_setup_card(repo_name: str, branch_name: str, chat_id: str) -> dict:
    """Build a card confirming worktree + group setup."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "开发环境已就绪"},
            "template": "green",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**项目**: {repo_name}\n"
                        f"**分支**: `{branch_name}`\n\n"
                        "已创建开发群，请在群里继续对话。\n"
                        "群内直接发消息即可，无需 @机器人。\n\n"
                        "**可用命令**:\n"
                        "- `/new` — 重置 Claude 会话\n"
                        "- `/done` — 提交并归档"
                    ),
                },
            },
        ],
    }

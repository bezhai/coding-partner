"""Feishu interactive card builders."""

from __future__ import annotations

from dataclasses import dataclass

from coding_partner.config import settings
from coding_partner.services.agent_runner import provider_display_name


@dataclass
class RepoInfo:
    name: str
    path: str
    branch: str


def _md_element(content: str) -> dict:
    """Build a markdown element (replaces div+lark_md for richer rendering)."""
    return {"tag": "markdown", "content": content}


def _button_row(buttons: list[dict]) -> dict:
    """Wrap buttons in a column_set for horizontal layout (V2 replacement for action tag)."""
    columns = [
        {"tag": "column", "width": "auto", "elements": [btn]}
        for btn in buttons
    ]
    return {"tag": "column_set", "flex_mode": "none", "columns": columns}


def _card(header: dict, elements: list[dict]) -> dict:
    """Build a v2 card with header and body elements."""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": header,
        "body": {"elements": elements},
    }


def build_repo_select_card(repos: list[RepoInfo]) -> dict:
    """Build a card with a dropdown to select a repo."""
    options = [
        {
            "text": {"tag": "plain_text", "content": f"{r.name} ({r.branch})"},
            "value": r.path,
        }
        for r in repos
    ]

    return _card(
        {"title": {"tag": "plain_text", "content": "选择项目"}, "template": "blue"},
        [
            _md_element("请选择要进行开发的项目仓库："),
            {
                "tag": "select_static",
                "placeholder": {"tag": "plain_text", "content": "选择项目..."},
                "options": options,
                "value": {"action": "select_repo"},
            },
        ],
    )


def build_cli_select_card(current_provider: str) -> dict:
    """Build a card to select Claude or Codex for future sessions."""
    current_name = provider_display_name(current_provider)
    options = [
        {
            "text": {"tag": "plain_text", "content": "Claude"},
            "value": "claude",
        },
        {
            "text": {"tag": "plain_text", "content": "Codex"},
            "value": "codex",
        },
    ]

    return _card(
        {"title": {"tag": "plain_text", "content": "选择 Agent"}, "template": "blue"},
        [
            _md_element(
                f"当前默认 Agent: **{current_name}**\n\n"
                "选择后会用于你后续新建的开发群。已创建的开发群不会受影响。"
            ),
            {
                "tag": "select_static",
                "placeholder": {"tag": "plain_text", "content": "选择 Claude 或 Codex"},
                "options": options,
                "value": {"action": "select_cli"},
            },
        ],
    )


def build_thinking_card(requirement: str, agent_name: str | None = None) -> dict:
    """Build a placeholder card while the selected agent is working."""
    agent_name = agent_name or provider_display_name()
    return _card(
        {
            "title": {"tag": "plain_text", "content": f"{agent_name} 正在工作中..."},
            "template": "wathet",
        },
        [_md_element(f"**需求**: {requirement}\n\n⏳ 正在执行，请稍候...")],
    )


def build_streaming_card(
    requirement: str,
    partial_text: str,
    tool_activities: list[str] | None = None,
    agent_name: str | None = None,
) -> dict:
    """Build a card showing streaming progress with tool activity."""
    agent_name = agent_name or provider_display_name()
    parts: list[str] = [f"**需求**: {requirement}"]

    if tool_activities:
        activity_lines = "\n".join(tool_activities[-5:])
        parts.append(f"\n**最近操作**:\n```\n{activity_lines}\n```")

    if partial_text:
        display = partial_text
        if len(display) > settings.card_streaming_max_len:
            display = display[-settings.card_streaming_max_len :]
        parts.append(f"\n---\n\n{display}\n")

    parts.append("\n\n⏳ 执行中...")

    return _card(
        {
            "title": {"tag": "plain_text", "content": f"{agent_name} 正在工作中..."},
            "template": "wathet",
        },
        [_md_element("\n".join(parts))],
    )


def build_plan_approval_card(plan_text: str, chat_id: str, agent_name: str | None = None) -> dict:
    """Build a card showing the agent's plan and asking for approval to execute."""
    agent_name = agent_name or provider_display_name()
    display = plan_text
    if len(display) > settings.card_result_max_len:
        display = display[: settings.card_result_max_len] + "\n\n... (已截断)"

    return _card(
        {"title": {"tag": "plain_text", "content": "方案确认"}, "template": "orange"},
        [
            _md_element(display),
            {"tag": "hr"},
            _md_element(f"{agent_name} 已完成分析，以上是执行方案。确认后将开始修改代码。"),
            _button_row([
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
            ]),
        ],
    )


def build_result_card(
    result_text: str,
    *,
    cost: str = "",
    duration: str = "",
    is_error: bool = False,
) -> dict:
    """Build a result card after the agent finishes."""
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
        elements.append(_md_element(footer))

    return _card(
        {"title": {"tag": "plain_text", "content": title}, "template": template},
        elements,
    )


def build_confirm_card(repo_name: str, requirement: str) -> dict:
    """Build a confirmation card before creating worktree + dev group."""
    # Truncate long requirement for display
    display_req = requirement if len(requirement) <= 200 else requirement[:200] + "..."
    return _card(
        {"title": {"tag": "plain_text", "content": "确认执行"}, "template": "orange"},
        [
            _md_element(
                f"**项目**: {repo_name}\n"
                f"**需求**: {display_req}\n\n"
                "将创建新分支和开发群，确认开始？"
            ),
            _button_row([
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
            ]),
        ],
    )


def build_question_card(
    question: str,
    options: list[str],
    chat_id: str,
    agent_name: str | None = None,
    all_questions: list[dict] | None = None,
) -> dict:
    """Build an interactive card for agent question(s).

    Single question: button per option.
    Multiple questions (all_questions): form with a dropdown per question + submit.
    """
    agent_name = agent_name or provider_display_name()

    if all_questions and len(all_questions) > 1:
        return _build_multi_question_card(all_questions, chat_id, agent_name)

    # --- single question: buttons ---
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

    elements: list[dict] = [_md_element(f"**{agent_name} 在询问：**\n\n{question}")]
    if buttons:
        elements.append(_button_row(buttons))
    elements.append(
        _md_element("*点击选项或直接回复文字作答*")
    )

    return _card(
        {"title": {"tag": "plain_text", "content": "需要你的输入"}, "template": "yellow"},
        elements,
    )


def _build_multi_question_card(
    all_questions: list[dict],
    chat_id: str,
    agent_name: str,
) -> dict:
    """Build a form card with a dropdown per question and a single submit button."""
    form_elements: list[dict] = []

    for i, q in enumerate(all_questions):
        select_options = [
            {"text": {"tag": "plain_text", "content": opt}, "value": opt}
            for opt in q["options"]
        ]
        form_elements.append(
            _md_element(f"**{i + 1}. {q['question']}**")
        )
        form_elements.append(
            {
                "tag": "select_static",
                "name": f"q_{i}",
                "placeholder": {"tag": "plain_text", "content": "请选择..."},
                "options": select_options,
            }
        )

    form_elements.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "提交"},
            "type": "primary",
            "action_type": "form_submit",
            "name": "submit_answers",
            "value": {"action": "answer_question", "chat_id": chat_id},
        }
    )

    return _card(
        {"title": {"tag": "plain_text", "content": "需要你的输入"}, "template": "yellow"},
        [
            _md_element(f"**{agent_name} 有以下问题需要你回答：**"),
            {"tag": "form", "name": "question_form", "elements": form_elements},
            _md_element("*选择后点击提交，或直接回复文字作答*"),
        ],
    )


def build_setup_card(
    repo_name: str,
    branch_name: str,
    chat_id: str,
    agent_name: str | None = None,
) -> dict:
    """Build a card confirming worktree + group setup."""
    agent_name = agent_name or provider_display_name()
    return _card(
        {"title": {"tag": "plain_text", "content": "开发环境已就绪"}, "template": "green"},
        [
            _md_element(
                f"**项目**: {repo_name}\n"
                f"**分支**: `{branch_name}`\n\n"
                f"**Agent**: {agent_name}\n\n"
                "已创建开发群，请在群里继续对话。\n"
                "群内直接发消息即可，无需 @机器人。\n\n"
                "**可用命令**:\n"
                f"- `/new` — 重置 {agent_name} 会话\n"
                "- `/cancel` — 终止当前任务\n"
                "- `/done` — 提交并归档\n"
                "- `/confirm` — 开启确认模式（修改前需审批）\n"
                "- `/auto` — 切回自动模式\n\n"
                "**私聊命令**:\n"
                "- `/cli` — 选择后续新会话使用 Claude 或 Codex"
            ),
        ],
    )

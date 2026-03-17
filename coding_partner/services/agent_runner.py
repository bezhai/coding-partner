"""Provider selection layer for supported coding agents."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

from coding_partner.config import settings


@dataclass
class AgentResult:
    result: str = ""
    session_id: str | None = None
    cost: str = ""
    duration: str = ""
    is_error: bool = False


@dataclass
class StreamDelta:
    """Incremental text from the agent."""

    text: str


@dataclass
class StreamToolUse:
    """A tool call or command made by the agent."""

    name: str
    summary: str


@dataclass
class StreamQuestion:
    """One or more questions the agent wants the user to answer."""

    question: str
    options: list[str]
    # For multi-question: list of {"question": str, "options": [str]}
    all_questions: list[dict] | None = None


@dataclass
class StreamResult:
    """Final result from the stream."""

    result: AgentResult


def provider_display_name(provider: str | None = None) -> str:
    provider = (provider or settings.normalized_agent_provider).strip().lower()
    return "Codex" if provider == "codex" else "Claude"


def write_tools_for_provider(provider: str | None = None) -> list[str]:
    provider = (provider or settings.normalized_agent_provider).strip().lower()
    if provider == "claude":
        return ["Bash", "Write", "Edit", "NotebookEdit"]
    return []


async def run(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
    provider: str | None = None,
) -> AgentResult:
    provider = (provider or settings.normalized_agent_provider).strip().lower()
    if provider == "codex":
        from coding_partner.services import codex_runner

        return await codex_runner.run(prompt=prompt, cwd=cwd, session_id=session_id)

    from coding_partner.services import claude_runner

    return await claude_runner.run(prompt=prompt, cwd=cwd, session_id=session_id)


async def run_stream(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
    proc_callback: Callable | None = None,
    disallowed_tools: list[str] | None = None,
    image_paths: list[str] | None = None,
    provider: str | None = None,
) -> AsyncGenerator[StreamDelta | StreamToolUse | StreamQuestion | StreamResult, None]:
    provider = (provider or settings.normalized_agent_provider).strip().lower()
    if provider == "codex":
        from coding_partner.services import codex_runner

        async for event in codex_runner.run_stream(
            prompt=prompt,
            cwd=cwd,
            session_id=session_id,
            proc_callback=proc_callback,
            disallowed_tools=disallowed_tools,
            image_paths=image_paths,
        ):
            yield event
        return

    from coding_partner.services import claude_runner

    async for event in claude_runner.run_stream(
        prompt=prompt,
        cwd=cwd,
        session_id=session_id,
        proc_callback=proc_callback,
        disallowed_tools=disallowed_tools,
        image_paths=image_paths,
    ):
        yield event

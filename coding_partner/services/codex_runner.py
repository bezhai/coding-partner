"""Codex CLI subprocess runner."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable

from coding_partner.config import settings
from coding_partner.services.agent_runner import (
    AgentResult,
    StreamDelta,
    StreamResult,
    StreamToolUse,
)

logger = logging.getLogger(__name__)


def _build_prompt(prompt: str, image_paths: list[str] | None, plan_only: bool) -> str:
    parts: list[str] = []
    if image_paths:
        joined = "\n".join(f"- {path}" for path in image_paths)
        parts.append(f"用户发送了图片，请结合这些图片文件处理任务：\n{joined}")
    if plan_only:
        parts.append(
            "当前处于确认模式。现在只允许分析并输出执行方案，不要修改文件，不要运行会产生改动的命令。"
        )
    if prompt:
        parts.append(prompt)
    return "\n\n".join(parts)


def _build_exec_cmd(
    prompt: str,
    session_id: str | None,
    image_paths: list[str] | None,
    disallowed_tools: list[str] | None,
) -> list[str]:
    effective_prompt = _build_prompt(
        prompt=prompt,
        image_paths=image_paths,
        plan_only=bool(disallowed_tools),
    )

    if session_id:
        cmd = [
            settings.codex_cli,
            "exec",
            "resume",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if settings.codex_model:
            cmd.extend(["-m", settings.codex_model])
        if image_paths:
            for path in image_paths:
                cmd.extend(["-i", path])
        cmd.extend([session_id, effective_prompt])
        return cmd

    cmd = [
        settings.codex_cli,
        "exec",
        "--json",
        "--color",
        "never",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if settings.codex_model:
        cmd.extend(["-m", settings.codex_model])
    if image_paths:
        for path in image_paths:
            cmd.extend(["-i", path])
    cmd.append(effective_prompt)
    return cmd


def _summarize_command(command: str) -> str:
    compact = " ".join(command.split())
    return f"⚡ `{compact[:80]}`" if compact else "⚡ 执行命令"


async def run(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
) -> AgentResult:
    final: AgentResult | None = None
    async for event in run_stream(prompt=prompt, cwd=cwd, session_id=session_id):
        if isinstance(event, StreamResult):
            final = event.result
    return final or AgentResult(result="Codex 未返回结果", is_error=True)


async def run_stream(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
    proc_callback: Callable[[asyncio.subprocess.Process], None] | None = None,
    disallowed_tools: list[str] | None = None,
    image_paths: list[str] | None = None,
) -> AsyncGenerator[StreamDelta | StreamToolUse | StreamResult, None]:
    """Run Codex in JSONL mode and map its events to the shared stream model."""
    cmd = _build_exec_cmd(prompt, session_id, image_paths, disallowed_tools)
    logger.info("Running Codex stream in %s (session: %s)", cwd, session_id)
    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,  # 10MB line buffer for large JSON lines
        )
    except Exception as e:
        yield StreamResult(result=AgentResult(result=f"启动 Codex 失败: {e}", is_error=True))
        return

    if proc_callback:
        proc_callback(proc)

    assert proc.stdout is not None
    assert proc.stderr is not None

    latest_session_id = session_id
    messages: list[str] = []
    last_message = ""
    tool_events: set[str] = set()

    try:
        while True:
            raw_line = await proc.stdout.readline()
            if not raw_line:
                break

            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")
            if event_type == "thread.started":
                latest_session_id = event.get("thread_id", latest_session_id)
                continue

            if event_type != "item.completed":
                continue

            item = event.get("item", {})
            item_type = item.get("type", "")

            if item_type == "agent_message":
                text = item.get("text", "").strip()
                if text:
                    messages.append(text)
                    last_message = text
                    yield StreamDelta(text=text)
                continue

            if item_type == "command_execution":
                item_id = item.get("id", "")
                if item_id in tool_events:
                    continue
                tool_events.add(item_id)
                command = item.get("command", "")
                yield StreamToolUse(name="command_execution", summary=_summarize_command(command))

        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.effective_agent_timeout
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        yield StreamResult(
            result=AgentResult(
                result=f"Codex 执行超时（{settings.effective_agent_timeout}s 限制）",
                session_id=latest_session_id,
                duration=f"{settings.effective_agent_timeout}s",
                is_error=True,
            )
        )
        return

    elapsed = time.monotonic() - start
    duration_str = f"{elapsed:.1f}s"
    stderr_text = stderr.decode(errors="replace").strip()

    if proc.returncode != 0:
        error_text = stderr_text or last_message or "Codex 执行失败"
        yield StreamResult(
            result=AgentResult(
                result=f"Codex 退出码 {proc.returncode}:\n{error_text}",
                session_id=latest_session_id,
                duration=duration_str,
                is_error=True,
            )
        )
        return

    result_text = last_message or ("\n\n".join(messages).strip()) or "(无输出)"
    yield StreamResult(
        result=AgentResult(
            result=result_text,
            session_id=latest_session_id,
            duration=duration_str,
        )
    )

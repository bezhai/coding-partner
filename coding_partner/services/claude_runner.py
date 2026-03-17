"""Claude Code CLI subprocess runner."""

import asyncio
import json
import logging
import shlex
import time
from collections.abc import AsyncGenerator, Callable

from coding_partner.config import settings
from coding_partner.services.agent_runner import (
    AgentResult,
    StreamDelta,
    StreamQuestion,
    StreamResult,
    StreamToolUse,
)

logger = logging.getLogger(__name__)


def _summarize_tool_use(name: str, inp: dict) -> str:
    """Create a short human-readable summary of a tool call."""
    if name == "Read":
        path = inp.get("file_path", "")
        return f"📂 读取 {path.split('/')[-1]}" if path else "📂 读取文件"
    if name == "Edit":
        path = inp.get("file_path", "")
        return f"✏️ 编辑 {path.split('/')[-1]}" if path else "✏️ 编辑文件"
    if name == "Write":
        path = inp.get("file_path", "")
        return f"📝 写入 {path.split('/')[-1]}" if path else "📝 写入文件"
    if name == "Bash":
        cmd = inp.get("command", "")
        return f"⚡ `{cmd[:100]}`" if cmd else "⚡ 执行命令"
    if name in ("Glob", "Grep"):
        pattern = inp.get("pattern", "")
        return f"🔍 搜索 {pattern[:40]}" if pattern else f"🔍 {name}"
    if name == "LSP":
        op = inp.get("operation", "")
        return f"🧠 LSP {op}"
    if name in ("Agent", "Task"):
        desc = inp.get("description", inp.get("prompt", ""))
        return f"🤖 {desc[:50]}" if desc else f"🤖 {name}"
    return f"🔧 {name}"


async def run(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
) -> AgentResult:
    """Run claude --print and return structured result."""
    cmd = [
        settings.claude_cli,
        "--print",
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
    ]

    if session_id:
        cmd.extend(["--resume", session_id])

    cmd.extend(["--", prompt])

    logger.info("Running Claude in %s (session: %s)", cwd, session_id)
    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.claude_timeout
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        timeout_min = settings.claude_timeout // 60
        return AgentResult(
            result=f"Claude 执行超时（{timeout_min} 分钟限制）",
            is_error=True,
            duration=f"{settings.claude_timeout}s",
        )
    except Exception as e:
        return AgentResult(result=f"启动 Claude 失败: {e}", is_error=True)

    elapsed = time.monotonic() - start
    duration_str = f"{elapsed:.1f}s"

    if proc.returncode != 0:
        error_text = stderr.decode(errors="replace").strip()
        return AgentResult(
            result=f"Claude 退出码 {proc.returncode}:\n{error_text}",
            is_error=True,
            duration=duration_str,
        )

    # Parse JSON output
    raw = stdout.decode(errors="replace").strip()
    try:
        data = json.loads(raw)

        # claude --print --output-format json returns a JSON array of blocks
        # or a single object depending on version
        if isinstance(data, list):
            # Extract text from assistant blocks
            texts = []
            new_session_id = session_id
            cost_usd = None
            for item in data:
                if isinstance(item, dict):
                    if item.get("type") == "result":
                        new_session_id = item.get("session_id", session_id)
                        cost_usd = item.get("cost_usd")
                        result_text = item.get("result", "")
                        if result_text:
                            texts.append(result_text)
                    elif item.get("type") == "assistant":
                        content = item.get("content", [])
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                texts.append(block.get("text", ""))
            result_text = "\n".join(texts) if texts else raw
            cost_str = f"${cost_usd:.4f}" if cost_usd else ""
        else:
            result_text = data.get("result", raw)
            new_session_id = data.get("session_id", session_id)
            cost_usd = data.get("cost_usd")
            cost_str = f"${cost_usd:.4f}" if cost_usd else ""

        return AgentResult(
            result=result_text,
            session_id=new_session_id,
            cost=cost_str,
            duration=duration_str,
        )
    except (json.JSONDecodeError, AttributeError):
        # Fallback: treat entire output as result
        return AgentResult(
            result=raw,
            session_id=session_id,
            duration=duration_str,
        )


WRITE_TOOLS = ["Bash", "Write", "Edit", "NotebookEdit"]


def _build_prompt_with_images(prompt: str, image_paths: list[str] | None) -> str:
    """Prepend image references to prompt so Claude reads them with the Read tool."""
    if not image_paths:
        return prompt
    refs = "\n".join(
        f"[用户发送了图片，请先用 Read 工具查看: {p}]" for p in image_paths
    )
    if prompt:
        return f"{refs}\n\n{prompt}"
    return refs


async def run_stream(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
    proc_callback: Callable[[asyncio.subprocess.Process], None] | None = None,
    disallowed_tools: list[str] | None = None,
    image_paths: list[str] | None = None,
) -> AsyncGenerator[StreamDelta | StreamToolUse | StreamQuestion | StreamResult, None]:
    """Run claude with streaming output, yielding deltas and a final result.

    Uses --output-format stream-json --verbose to get NDJSON events.
    If proc_callback is given, it is called with the subprocess once created
    (useful for allowing external cancellation).
    If disallowed_tools is given, those tools are blocked via --disallowedTools.
    If image_paths is given, the prompt is augmented with instructions to read the images.
    """
    effective_prompt = _build_prompt_with_images(prompt, image_paths)

    claude_cmd = [
        settings.claude_cli,
        "--print",
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--verbose",
    ]

    if disallowed_tools:
        claude_cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])

    if session_id:
        claude_cmd.extend(["--resume", session_id])

    claude_cmd.extend(["--", effective_prompt])

    # Wrap in `script` to allocate a PTY — forces the binary to line-buffer stdout
    inner = " ".join(shlex.quote(c) for c in claude_cmd)
    cmd = ["script", "-qefc", inner, "/dev/null"]

    logger.info("Running Claude stream in %s (session: %s)", cwd, session_id)
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
        yield StreamResult(result=AgentResult(result=f"启动 Claude 失败: {e}", is_error=True))
        return

    if proc_callback:
        proc_callback(proc)

    assert proc.stdout is not None

    new_session_id = session_id
    cost_usd = None
    result_text = ""

    idle_timeout = settings.stream_idle_timeout
    timed_out = False

    try:
        logger.info("Stream: entering readline loop (idle_timeout=%ds)", idle_timeout)
        while True:
            try:
                raw_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=idle_timeout
                )
            except TimeoutError:
                logger.warning(
                    "Stream: no output for %ds, killing process", idle_timeout
                )
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                break

            if not raw_line:
                logger.info("Stream: EOF reached")
                break  # EOF

            line = raw_line.decode(errors="replace").strip().strip("\r")
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            # ---- system: init / lifecycle ----------------------------------
            if event_type == "system":
                subtype = event.get("subtype", "")
                logger.info("Stream event: system/%s", subtype)

            # ---- assistant: content blocks ---------------------------------
            elif event_type == "assistant":
                logger.info("Stream event: assistant")
                msg = event.get("message", {})
                content = msg.get("content", [])
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text = block.get("text", "")
                        if text:
                            yield StreamDelta(text=text)
                    elif block_type == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        if name == "AskUserQuestion":
                            questions = inp.get("questions", [])
                            all_qs: list[dict] = []
                            for q in questions:
                                qt = q.get("question", "")
                                opts = [
                                    o.get("label", "")
                                    for o in q.get("options", [])
                                ]
                                if qt:
                                    all_qs.append({"question": qt, "options": opts})
                            if len(all_qs) == 1:
                                yield StreamQuestion(
                                    question=all_qs[0]["question"],
                                    options=all_qs[0]["options"],
                                )
                            elif len(all_qs) > 1:
                                combined = "\n".join(
                                    f"{i + 1}. {q['question']}"
                                    for i, q in enumerate(all_qs)
                                )
                                yield StreamQuestion(
                                    question=combined,
                                    options=[],
                                    all_questions=all_qs,
                                )
                        else:
                            yield StreamToolUse(
                                name=name,
                                summary=_summarize_tool_use(name, inp),
                            )

            # ---- rate_limit_event: rate limit info -------------------------
            elif event_type == "rate_limit_event":
                info = event.get("rate_limit_info", {})
                status = info.get("status", "")
                logger.info("Stream event: rate_limit_event status=%s", status)

            # ---- result: final result --------------------------------------
            elif event_type == "result":
                subtype = event.get("subtype", "")
                logger.info("Stream event: result/%s", subtype)
                new_session_id = event.get("session_id", session_id)
                cost_usd = event.get("total_cost_usd")
                result_text = event.get("result", "")
                break  # result is the final event — stop reading immediately

            # ---- unknown: log for debugging --------------------------------
            else:
                logger.warning(
                    "Stream event: unhandled type=%s keys=%s",
                    event_type,
                    list(event.keys()),
                )

        # Wait for process to finish
        await asyncio.wait_for(proc.wait(), timeout=30)

    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    elapsed = time.monotonic() - start
    duration_str = f"{elapsed:.1f}s"
    cost_str = f"${cost_usd:.4f}" if cost_usd else ""

    if timed_out:
        timeout_min = idle_timeout // 60
        final_text = f"执行超时（{timeout_min} 分钟无输出），已终止"
        is_error = True
    else:
        final_text = result_text or "(无输出)"
        is_error = proc.returncode is not None and proc.returncode != 0

    yield StreamResult(
        result=AgentResult(
            result=final_text,
            session_id=new_session_id,
            cost=cost_str,
            duration=duration_str,
            is_error=is_error,
        )
    )

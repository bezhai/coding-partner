"""Claude Code CLI subprocess runner."""

import asyncio
import json
import logging
import shlex
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

from coding_partner.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    result: str = ""
    session_id: str | None = None
    cost: str = ""
    duration: str = ""
    is_error: bool = False


@dataclass
class StreamDelta:
    """Incremental text from assistant message."""

    text: str


@dataclass
class StreamToolUse:
    """A tool call made by Claude."""

    name: str
    summary: str  # human-readable one-liner


@dataclass
class StreamQuestion:
    """Claude is asking the user a question via AskUserQuestion."""

    question: str
    options: list[str]


@dataclass
class StreamResult:
    """Final result from the stream."""

    result: ClaudeResult


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
        return f"⚡ `{cmd[:60]}`" if cmd else "⚡ 执行命令"
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
) -> ClaudeResult:
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
        return ClaudeResult(
            result=f"Claude 执行超时（{timeout_min} 分钟限制）",
            is_error=True,
            duration=f"{settings.claude_timeout}s",
        )
    except Exception as e:
        return ClaudeResult(result=f"启动 Claude 失败: {e}", is_error=True)

    elapsed = time.monotonic() - start
    duration_str = f"{elapsed:.1f}s"

    if proc.returncode != 0:
        error_text = stderr.decode(errors="replace").strip()
        return ClaudeResult(
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

        return ClaudeResult(
            result=result_text,
            session_id=new_session_id,
            cost=cost_str,
            duration=duration_str,
        )
    except (json.JSONDecodeError, AttributeError):
        # Fallback: treat entire output as result
        return ClaudeResult(
            result=raw,
            session_id=session_id,
            duration=duration_str,
        )


WRITE_TOOLS = ["Bash", "Write", "Edit", "NotebookEdit"]


async def run_stream(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
    proc_callback: Callable[[asyncio.subprocess.Process], None] | None = None,
    disallowed_tools: list[str] | None = None,
) -> AsyncGenerator[StreamDelta | StreamToolUse | StreamQuestion | StreamResult, None]:
    """Run claude with streaming output, yielding deltas and a final result.

    Uses --output-format stream-json --verbose to get NDJSON events.
    If proc_callback is given, it is called with the subprocess once created
    (useful for allowing external cancellation).
    If disallowed_tools is given, those tools are blocked via --disallowedTools.
    """
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

    claude_cmd.extend(["--", prompt])

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
        yield StreamResult(result=ClaudeResult(result=f"启动 Claude 失败: {e}", is_error=True))
        return

    if proc_callback:
        proc_callback(proc)

    assert proc.stdout is not None

    new_session_id = session_id
    cost_usd = None
    result_text = ""

    try:
        logger.info("Stream: entering readline loop")
        while True:
            raw_line = await proc.stdout.readline()
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
            logger.info("Stream event: type=%s", event_type)

            # stream-json emits full "assistant" messages (one per turn)
            # Content blocks include text and tool_use
            if event_type == "assistant":
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
                            for q in questions:
                                question_text = q.get("question", "")
                                options = [
                                    o.get("label", "")
                                    for o in q.get("options", [])
                                ]
                                if question_text:
                                    yield StreamQuestion(
                                        question=question_text,
                                        options=options,
                                    )
                        else:
                            yield StreamToolUse(
                                name=name,
                                summary=_summarize_tool_use(name, inp),
                            )

            elif event_type == "result":
                new_session_id = event.get("session_id", session_id)
                cost_usd = event.get("total_cost_usd")
                result_text = event.get("result", "")

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
    final_text = result_text or "(无输出)"

    is_error = proc.returncode is not None and proc.returncode != 0

    yield StreamResult(
        result=ClaudeResult(
            result=final_text,
            session_id=new_session_id,
            cost=cost_str,
            duration=duration_str,
            is_error=is_error,
        )
    )

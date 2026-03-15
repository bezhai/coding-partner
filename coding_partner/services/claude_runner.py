"""Claude Code CLI subprocess runner."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from coding_partner.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 300  # 5 minutes


@dataclass
class ClaudeResult:
    result: str = ""
    session_id: str | None = None
    cost: str = ""
    duration: str = ""
    is_error: bool = False


async def run(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
) -> ClaudeResult:
    """Run claude --print and return structured result."""
    cmd = [
        settings.claude_cli,
        "--print",
        "--output-format",
        "json",
        "--verbose",
    ]

    if session_id:
        cmd.extend(["--session-id", session_id])

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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SECONDS)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return ClaudeResult(
            result="Claude 执行超时（5 分钟限制）",
            is_error=True,
            duration=f"{TIMEOUT_SECONDS}s",
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
    except json.JSONDecodeError:
        # Fallback: treat entire output as result
        return ClaudeResult(
            result=raw,
            session_id=session_id,
            duration=duration_str,
        )

"""Git worktree management with AI branch naming."""

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from coding_partner.config import settings

logger = logging.getLogger(__name__)


@dataclass
class WorktreeInfo:
    path: str
    branch_name: str


async def generate_branch_name(requirement: str) -> str:
    """Use Claude to generate a branch name from a requirement."""
    prompt = (
        "Generate a git branch name for this requirement. "
        "Format: type/kebab-case-name (e.g., feat/add-captcha, fix/login-timeout). "
        "Types: feat, fix, refactor, chore, docs. "
        "Reply with ONLY the branch name, nothing else.\n\n"
        f"Requirement: {requirement}"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            settings.claude_cli,
            "-p",
            prompt,
            "--model",
            settings.branch_name_model,
            "--output-format",
            "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        name = stdout.decode().strip().strip("`").strip('"').strip("'")

        # Validate branch name format
        if re.match(r"^(feat|fix|refactor|chore|docs)/[a-z0-9][a-z0-9-]*$", name):
            return name

        logger.warning("AI generated invalid branch name: %s, using fallback", name)
    except Exception as e:
        logger.warning("branch name generation failed: %s", e)

    # Fallback: sanitize requirement into a branch name
    slug = re.sub(r"[^a-z0-9]+", "-", requirement.lower()[:40]).strip("-")
    return f"feat/{slug or 'dev'}"


async def create_worktree(repo_path: str, requirement: str) -> WorktreeInfo:
    """Create a worktree: AI branch name -> git worktree add -> return path."""
    branch_name = await generate_branch_name(requirement)
    repo = Path(repo_path)

    # Worktree base directory: sibling to repo, named <repo>-worktrees
    worktree_base = repo.parent / f"{repo.name}-worktrees"
    worktree_base.mkdir(parents=True, exist_ok=True)

    # Worktree path: base / branch_name (replace / with -)
    wt_dirname = branch_name.replace("/", "-")
    wt_path = worktree_base / wt_dirname

    proc = await asyncio.create_subprocess_exec(
        "git",
        "worktree",
        "add",
        str(wt_path),
        "-b",
        branch_name,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

    if proc.returncode != 0:
        error = stderr.decode().strip()
        raise RuntimeError(f"git worktree add failed: {error}")

    logger.info("Created worktree: %s (branch: %s)", wt_path, branch_name)
    return WorktreeInfo(path=str(wt_path), branch_name=branch_name)


async def cleanup_worktree(
    worktree_path: str, repo_path: str, branch_name: str | None = None
) -> None:
    """Remove a worktree and force-delete the branch."""
    wt = Path(worktree_path)

    # Try to discover branch from worktree if not provided
    if not branch_name and wt.exists():
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                cwd=worktree_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            branch_name = stdout.decode().strip() or None
        except Exception:
            pass

    # Remove worktree (even if path is gone, prune stale entries)
    if wt.exists():
        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "remove",
            worktree_path,
            "--force",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)

    # Prune stale worktree references
    proc = await asyncio.create_subprocess_exec(
        "git",
        "worktree",
        "prune",
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)

    # Force-delete branch
    if branch_name and branch_name not in ("main", "master", "HEAD"):
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "-D",
            branch_name,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            logger.warning("branch -D %s failed: %s", branch_name, stderr.decode().strip())

    logger.info("Cleaned up worktree: %s (branch: %s)", worktree_path, branch_name)

"""Scan for git repositories under a base path."""

import logging
import subprocess
from pathlib import Path

from coding_partner.formatter import RepoInfo

logger = logging.getLogger(__name__)

SKIP_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
}


def scan_repos(base_path: str | Path, max_depth: int = 5) -> list[RepoInfo]:
    """Recursively scan base_path for git repos, return (name, path, branch) list."""
    base = Path(base_path).expanduser().resolve()
    if not base.is_dir():
        logger.warning("repo base path does not exist: %s", base)
        return []

    repos: list[RepoInfo] = []
    _scan(base, base, max_depth, 0, repos)

    # Sort by name
    repos.sort(key=lambda r: r.name)
    return repos


def _scan(base: Path, current: Path, max_depth: int, depth: int, repos: list[RepoInfo]) -> None:
    if depth > max_depth:
        return

    try:
        entries = sorted(current.iterdir())
    except PermissionError:
        return

    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if entry.name in SKIP_DIRS:
            continue
        if entry.name.endswith("-worktrees"):
            continue

        git_dir = entry / ".git"
        if git_dir.exists():
            branch = _get_branch(entry)
            name = str(entry.relative_to(base))
            repos.append(RepoInfo(name=name, path=str(entry), branch=branch))
            # Don't recurse into git repos
            continue

        _scan(base, entry, max_depth, depth + 1, repos)


def _get_branch(repo_path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Feishu credentials ---
    feishu_app_id: str
    feishu_app_secret: str
    bot_open_id: str = ""

    # --- Paths ---
    repo_base_path: str  # required — no sensible default, users must configure

    @field_validator("repo_base_path")
    @classmethod
    def _validate_repo_base_path(cls, v: str) -> str:
        if not v:
            raise ValueError("REPO_BASE_PATH is required — set it in .env or environment")
        p = Path(v).expanduser()
        if not p.is_dir():
            raise ValueError(f"REPO_BASE_PATH does not exist: {p}")
        return v

    db_path: str = "./data/coding_partner.db"
    group_name_prefix: str = ""
    agent_provider: str = "claude"
    claude_cli: str = "claude"
    codex_cli: str = "codex"
    codex_model: str = ""
    log_level: str = "INFO"

    # --- Agent execution ---
    claude_timeout: int = 1800  # seconds, kept for backward compatibility
    agent_timeout: int | None = None
    stream_idle_timeout: int = 600  # seconds, kill stream if no output for this long
    branch_name_model: str = "haiku"  # model for generating branch names
    # Permission mode: "auto" = all tools auto-approved;
    # "confirm" = read-only first pass, then Feishu card for approval before write pass
    permission_mode: str = "auto"

    # --- Streaming UI ---
    stream_cooldown: float = 3.0  # seconds between card updates
    card_streaming_max_len: int = 2000  # max chars shown in streaming card
    card_result_max_len: int = 3000  # max chars shown in result card
    tool_activity_limit: int = 8  # max tool activity entries to keep

    # --- Housekeeping ---
    seen_message_max_age: int = 3600  # seconds before seen_messages are cleaned
    cleanup_interval: int = 600  # seconds between cleanup runs
    repo_scan_max_depth: int = 5  # max directory depth for repo scanning

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def repo_base(self) -> Path:
        return Path(self.repo_base_path).expanduser()

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def normalized_group_name_prefix(self) -> str:
        prefix = self.group_name_prefix.strip()
        return f"{prefix} " if prefix else ""

    @property
    def normalized_agent_provider(self) -> str:
        provider = self.agent_provider.strip().lower()
        if provider not in {"claude", "codex"}:
            raise ValueError(f"Unsupported AGENT_PROVIDER: {self.agent_provider}")
        return provider

    @property
    def agent_display_name(self) -> str:
        return "Codex" if self.normalized_agent_provider == "codex" else "Claude"

    @property
    def configured_agent_cli(self) -> str:
        if self.normalized_agent_provider == "codex":
            return self.codex_cli
        return self.claude_cli

    @property
    def effective_agent_timeout(self) -> int:
        return self.agent_timeout or self.claude_timeout


settings = Settings()  # type: ignore[call-arg]

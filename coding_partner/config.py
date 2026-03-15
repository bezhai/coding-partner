from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Feishu credentials ---
    feishu_app_id: str
    feishu_app_secret: str
    bot_open_id: str = ""

    # --- Paths ---
    repo_base_path: str  # required — no sensible default, users must configure
    db_path: str = "./data/coding_partner.db"
    claude_cli: str = "claude"
    log_level: str = "INFO"

    # --- Claude execution ---
    claude_timeout: int = 1800  # seconds, safety net for Claude subprocess
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


settings = Settings()  # type: ignore[call-arg]

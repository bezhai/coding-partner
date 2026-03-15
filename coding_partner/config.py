from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    feishu_app_id: str
    feishu_app_secret: str
    bot_open_id: str = ""

    repo_base_path: str = "~/code"
    db_path: str = "./data/coding_partner.db"
    claude_cli: str = "claude"
    log_level: str = "INFO"

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

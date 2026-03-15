"""CLI entry point for coding-partner: run / setup / check."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# XDG-style default paths
CONFIG_DIR = Path.home() / ".config" / "coding-partner"
DATA_DIR = Path.home() / ".local" / "share" / "coding-partner"

_ENV_REQUIRED_KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")

# Inline systemd service template (no project directory needed after install)
_SYSTEMD_TEMPLATE = """\
[Unit]
Description=Coding Partner - Feishu Vibe Coding Bot
After=network.target

[Service]
Type=simple
User={user}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
EnvironmentFile={env_file}
Environment=DB_PATH={data_dir}/coding_partner.db

[Install]
WantedBy=multi-user.target
"""


# ── Env file helpers ────────────────────────────────────


def _load_env_file(path: Path) -> None:
    """Parse KEY=VALUE lines from *path* and inject into os.environ (without overwriting)."""
    if not path.is_file():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            # Don't overwrite existing env vars (explicit env > file)
            if key not in os.environ:
                os.environ[key] = value


def _resolve_env_file(explicit: str | None) -> Path | None:
    """Return the env file path by priority: explicit arg > CWD > config dir."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        print(f"Error: env file not found: {explicit}", file=sys.stderr)
        raise SystemExit(1)

    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        return cwd_env

    config_env = CONFIG_DIR / ".env"
    if config_env.is_file():
        return config_env

    return None


# ── Subcommands ─────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> None:
    """Load env file then start the bot."""
    env_file = _resolve_env_file(getattr(args, "env_file", None))
    if env_file:
        _load_env_file(env_file)
        print(f"Loaded env from {env_file}")

    # Import *after* env injection so Settings() picks up the values
    from coding_partner.main import run

    run()


def cmd_check(_args: argparse.Namespace) -> None:
    """Check that required tools and config are present."""
    ok = True

    # Tools
    tools = [
        ("claude", "Claude Code CLI"),
        ("git", "Git version control"),
        ("script", "PTY allocation (bsdutils)"),
    ]
    for cmd, desc in tools:
        if shutil.which(cmd):
            print(f"  [OK]   {cmd} — {desc}")
        else:
            print(f"  [MISS] {cmd} — {desc}")
            ok = False

    # Config files
    print()
    for label, path in [
        ("Config dir", CONFIG_DIR),
        ("Config .env", CONFIG_DIR / ".env"),
        ("Data dir", DATA_DIR),
        ("CWD .env", Path.cwd() / ".env"),
    ]:
        exists = path.exists()
        tag = "[OK]" if exists else "[MISS]"
        print(f"  {tag:6s} {label}: {path}")
        # Not marking missing config as failure — CWD .env may suffice

    if not ok:
        print("\nSome required tools are missing.")
        raise SystemExit(1)

    print("\nAll checks passed.")


def cmd_setup(_args: argparse.Namespace) -> None:
    """Interactive wizard: create config, data dirs, optionally install systemd service."""
    print()
    print("╔══════════════════════════════════════════╗")
    print("║   Coding Partner — CLI Setup Wizard      ║")
    print("║   飞书 Vibe Coding 机器人                ║")
    print("╚══════════════════════════════════════════╝")
    print()

    # 1. Create directories
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  Config dir: {CONFIG_DIR}")
    print(f"  Data dir:   {DATA_DIR}")
    print()

    # 2. Create / update .env
    env_file = CONFIG_DIR / ".env"
    existing_values: dict[str, str] = {}
    if env_file.is_file():
        # Read existing values
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                existing_values[k.strip()] = v.strip()

    need_input = any(not existing_values.get(k) for k in _ENV_REQUIRED_KEYS)

    if need_input:
        print("Configure Feishu credentials:")
        print()
        app_id = input(f"  FEISHU_APP_ID [{existing_values.get('FEISHU_APP_ID', '')}]: ").strip()
        app_secret = input(
            f"  FEISHU_APP_SECRET [{existing_values.get('FEISHU_APP_SECRET', '')}]: "
        ).strip()
        bot_open_id = input(f"  BOT_OPEN_ID [{existing_values.get('BOT_OPEN_ID', '')}]: ").strip()
        repo_base = input(
            f"  REPO_BASE_PATH (required) [{existing_values.get('REPO_BASE_PATH', '')}]: "
        ).strip()
        print()

        if app_id:
            existing_values["FEISHU_APP_ID"] = app_id
        if app_secret:
            existing_values["FEISHU_APP_SECRET"] = app_secret
        if bot_open_id:
            existing_values["BOT_OPEN_ID"] = bot_open_id
        if repo_base:
            existing_values["REPO_BASE_PATH"] = repo_base
        existing_values.setdefault("DB_PATH", str(DATA_DIR / "coding_partner.db"))
        existing_values.setdefault("LOG_LEVEL", "INFO")

        # Write .env
        with open(env_file, "w") as f:
            for k, v in existing_values.items():
                f.write(f"{k}={v}\n")
        print(f"  Config saved to {env_file}")
    else:
        print(f"  Config already exists: {env_file}")

    print()

    # 3. Check dependencies
    print("Checking dependencies...")
    missing = []
    for cmd, desc in [
        ("claude", "Claude Code CLI"),
        ("git", "Git"),
        ("script", "PTY allocation"),
    ]:
        if shutil.which(cmd):
            print(f"  [OK]   {cmd}")
        else:
            print(f"  [MISS] {cmd} — {desc}")
            missing.append(cmd)

    if missing:
        print(f"\n  Missing: {', '.join(missing)}. Please install them before running.")
        print()

    # 4. Generate systemd service
    print()
    answer = input("Generate systemd service file? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        cp_bin = shutil.which("coding-partner")
        if not cp_bin:
            # Fallback: try to locate via sys.executable
            cp_bin = f"{sys.executable} -m coding_partner.cli"
            print(f"  Note: coding-partner not found in PATH, using: {cp_bin}")

        user = os.environ.get("SUDO_USER", os.environ.get("USER", "root"))
        service_content = _SYSTEMD_TEMPLATE.format(
            user=user,
            exec_start=f"{cp_bin} run",
            env_file=env_file,
            data_dir=DATA_DIR,
        )

        service_dir = CONFIG_DIR
        service_path = service_dir / "coding-partner.service"
        with open(service_path, "w") as f:
            f.write(service_content)
        print(f"  Service file: {service_path}")
        print()

        install = input("Install and start the systemd service now? [y/N] ").strip().lower()
        if install in ("y", "yes"):
            target = Path("/etc/systemd/system/coding-partner.service")
            try:
                subprocess.run(["sudo", "cp", str(service_path), str(target)], check=True)
                subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
                subprocess.run(["sudo", "systemctl", "enable", "coding-partner"], check=True)
                subprocess.run(["sudo", "systemctl", "start", "coding-partner"], check=True)
                print()
                print("  Service started!")
                print("  Status:  sudo systemctl status coding-partner")
                print("  Logs:    sudo journalctl -u coding-partner -f")
                print("  Stop:    sudo systemctl stop coding-partner")
            except subprocess.CalledProcessError:
                print("  Failed to install service. You can install manually:")
                print(f"    sudo cp {service_path} /etc/systemd/system/")
                print("    sudo systemctl daemon-reload")
                print("    sudo systemctl enable --now coding-partner")
        else:
            print("  To install manually:")
            print(f"    sudo cp {service_path} /etc/systemd/system/")
            print("    sudo systemctl daemon-reload")
            print("    sudo systemctl enable --now coding-partner")

    print()
    print("Setup complete!")
    print("  Run manually: coding-partner run")
    print("  Check status: coding-partner check")
    print()


# ── Main ────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="coding-partner",
        description="Coding Partner — Feishu vibe coding bot powered by Claude Code",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Start the bot")
    p_run.add_argument(
        "--env-file",
        help="Path to .env file (default: CWD/.env > ~/.config/coding-partner/.env)",
    )

    # setup
    sub.add_parser("setup", help="Interactive setup wizard")

    # check
    sub.add_parser("check", help="Check dependencies and config")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "check":
        cmd_check(args)
    else:
        parser.print_help()
        raise SystemExit(1)


if __name__ == "__main__":
    main()

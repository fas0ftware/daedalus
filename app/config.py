"""Settings from the environment (.env). Fails early with a clear message."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    model: str = "claude-haiku-5-5"
    effort: str = "medium"
    max_steps: int = 8              # tool calls per request
    sandbox_image: str = "daedalus-sandbox"
    sandbox_timeout: int = 20       # seconds per code run
    workspace: str = "workspace"
    requests_per_hour: int = 30


def _int(environ, name: str, default: int, low: int, high: int) -> int:
    raw = environ.get(name, "").strip() or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def load_settings(environ=os.environ) -> Settings:
    missing = [k for k in ("TELEGRAM_TOKEN", "ALLOWED_USER_ID", "ANTHROPIC_API_KEY") if not environ.get(k)]
    if missing:
        raise ValueError("Missing in .env: " + ", ".join(missing))
    try:
        ids = frozenset(int(x) for x in environ["ALLOWED_USER_ID"].replace(" ", "").split(",") if x)
    except ValueError:
        raise ValueError("ALLOWED_USER_ID must be a number (or comma-separated numbers)") from None
    if not ids:
        raise ValueError("ALLOWED_USER_ID must list at least one Telegram user id")
    effort = environ.get("CLAUDE_EFFORT", "medium")
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("CLAUDE_EFFORT must be low, medium, high, xhigh or max")
    return Settings(
        telegram_token=environ["TELEGRAM_TOKEN"],
        allowed_user_ids=ids,
        model=environ.get("CLAUDE_MODEL", "claude-haiku-5-5"),
        effort=effort,
        sandbox_image=environ.get("DAEDALUS_SANDBOX_IMAGE", "daedalus-sandbox"),
        sandbox_timeout=_int(environ, "DAEDALUS_SANDBOX_TIMEOUT", 20, 1, 600),
        workspace=environ.get("DAEDALUS_WORKSPACE", "workspace"),
        requests_per_hour=_int(environ, "DAEDALUS_REQUESTS_PER_HOUR", 30, 1, 10_000),
    )

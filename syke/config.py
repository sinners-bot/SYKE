from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    discord_token: str
    dev_guild_id: int | None
    ai_provider: str  # "openai" | "anthropic" | "none"
    openai_api_key: str
    openai_model: str
    anthropic_api_key: str
    anthropic_model: str
    db_path: str
    scan_limit: int
    min_messages: int
    cache_minutes: int
    default_timezone: str
    default_prefix: str
    owner_ids: frozenset[int] = frozenset()

DEFAULT_OWNER_IDS = "1342786189576634398"


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(part) for part in raw.replace(" ", "").split(",") if part.isdigit())


def load_settings() -> Settings:
    load_dotenv()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    provider = os.getenv("SYKE_AI_PROVIDER", "auto").strip().lower() or "auto"
    if provider == "auto":
        provider = "openai" if openai_key else "anthropic" if anthropic_key else "none"
    if provider == "openai" and not openai_key or provider == "anthropic" and not anthropic_key:
        provider = "none"

    dev_guild = os.getenv("SYKE_DEV_GUILD_ID", "").strip()
    return Settings(
        discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
        dev_guild_id=int(dev_guild) if dev_guild else None,
        ai_provider=provider,
        openai_api_key=openai_key,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        anthropic_api_key=anthropic_key,
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest").strip(),
        db_path=os.getenv("SYKE_DB_PATH", "syke.db").strip(),
        scan_limit=_int("SYKE_SCAN_LIMIT", 3000),
        min_messages=_int("SYKE_MIN_MESSAGES", 15),
        cache_minutes=_int("SYKE_CACHE_MINUTES", 15),
        default_timezone=os.getenv("SYKE_DEFAULT_TIMEZONE", "UTC").strip() or "UTC",
        default_prefix=os.getenv("SYKE_DEFAULT_PREFIX", "!").strip() or "!",
        owner_ids=_ids(os.getenv("SYKE_OWNER_IDS", DEFAULT_OWNER_IDS)),
    )

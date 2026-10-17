import base64
import binascii
import os
from dataclasses import dataclass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class DiscordConfig:
    token: str
    cat_gen_api_key: str
    guild_id: int | None = None

    @classmethod
    def from_env(cls) -> "DiscordConfig":
        guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
        if guild and (not guild.isdecimal() or int(guild) <= 0):
            raise SystemExit("DISCORD_GUILD_ID must be a positive server ID")
        return cls(
            token=_require("DISCORD_TOKEN"),
            cat_gen_api_key=_require("CAT_GEN_API_KEY"),
            guild_id=int(guild) if guild else None,
        )


def normalize_group_id(group_id: str) -> str:
    """Accept either the REST API form ("group.<base64>") or signal-cli's internal id.

    Returns the internal id, which is what incoming messages carry.
    """
    group_id = group_id.strip()
    if group_id.startswith("group."):
        try:
            return base64.b64decode(group_id.removeprefix("group.")).decode()
        except (binascii.Error, UnicodeDecodeError) as e:
            raise SystemExit(f"Invalid group id {group_id!r}: {e}") from e
    return group_id


@dataclass(frozen=True)
class Config:
    signal_api_url: str
    signal_number: str

    llm_base_url: str
    llm_model: str
    llm_api_key: str | None
    llm_timeout: float
    llm_max_input_chars: int

    db_path: str
    default_count: int
    max_count: int
    history_limit: int
    allowed_groups: frozenset[str]

    cat_gen_api_key: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        allowed = os.environ.get("ALLOWED_GROUPS", "")
        return cls(
            signal_api_url=os.environ.get("SIGNAL_API_URL", "http://localhost:8080").rstrip("/"),
            signal_number=_require("SIGNAL_NUMBER"),
            llm_base_url=_require("LLM_BASE_URL").rstrip("/"),
            llm_model=_require("LLM_MODEL"),
            llm_api_key=os.environ.get("LLM_API_KEY") or None,
            llm_timeout=_float("LLM_TIMEOUT", 300.0),
            llm_max_input_chars=_int("LLM_MAX_INPUT_CHARS", 60_000),
            db_path=os.environ.get("DB_PATH", "messages.db"),
            default_count=_int("TLDR_DEFAULT_COUNT", 50),
            max_count=_int("TLDR_MAX_COUNT", 500),
            history_limit=_int("HISTORY_LIMIT", 5000),
            allowed_groups=frozenset(
                normalize_group_id(g) for g in allowed.split(",") if g.strip()
            ),
            cat_gen_api_key=os.environ.get("CAT_GEN_API_KEY") or None,
        )

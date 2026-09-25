"""Settings loaded from environment variables (and a local .env file, if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl

from dotenv import load_dotenv

# Sites of the HeadHunter group that this bot knows how to poll. Every one of
# them is served by the same API at api.hh.ru; the `host` query parameter picks
# the site, and the default region is that site's country.
KNOWN_SOURCES = {
    "hh.ru": {"default_area": ""},  # empty = no region filter
    "rabota.by": {"default_area": "16"},  # 16 = Belarus
}


class ConfigError(RuntimeError):
    pass


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class SearchFilters:
    """hh.ru-style structured search parameters, applied before any LLM scoring."""

    text: str
    area: str
    experience: str
    professional_roles: list[str]
    extra_params: list[tuple[str, str]] = field(default_factory=list)

    def to_params(self) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = []
        if self.text:
            params.append(("text", self.text))
        if self.area:
            params.append(("area", self.area))
        if self.experience:
            params.append(("experience", self.experience))
        for role in self.professional_roles:
            params.append(("professional_role", role))
        params.extend(self.extra_params)
        return params


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: int | None
    anthropic_api_key: str
    claude_model: str
    claude_effort: str
    hh_access_token: str
    hh_user_agent: str
    sources: list[str]
    filters: dict[str, SearchFilters]
    poll_interval_seconds: int
    per_page: int
    first_run_batch: int
    threshold: int
    heartbeat_hour: int
    timezone: str
    data_dir: Path
    resume_file: Path
    resume_text: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "job_alert.sqlite3"


def load_config() -> Config:
    load_dotenv()

    telegram_bot_token = _get("TELEGRAM_BOT_TOKEN")
    anthropic_api_key = _get("ANTHROPIC_API_KEY")
    hh_user_agent = _get("HH_USER_AGENT")
    missing = [
        name
        for name, value in [
            ("TELEGRAM_BOT_TOKEN", telegram_bot_token),
            ("ANTHROPIC_API_KEY", anthropic_api_key),
            ("HH_USER_AGENT", hh_user_agent),
        ]
        if not value
    ]
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))

    chat_id_raw = _get("TELEGRAM_CHAT_ID")
    telegram_chat_id = int(chat_id_raw) if chat_id_raw else None

    sources = _split(_get("SOURCES", "hh.ru,rabota.by"))
    unknown = [s for s in sources if s not in KNOWN_SOURCES]
    if unknown:
        raise ConfigError(
            f"Unknown SOURCES entries {unknown}; supported: {', '.join(KNOWN_SOURCES)}"
        )
    if not sources:
        raise ConfigError("SOURCES must list at least one source")

    text = _get("SEARCH_TEXT", "python")
    experience = _get("SEARCH_EXPERIENCE")
    roles = _split(_get("SEARCH_PROFESSIONAL_ROLES", "96"))  # 96 = programmer/developer
    extra = parse_qsl(_get("SEARCH_EXTRA_PARAMS"), keep_blank_values=False)

    filters: dict[str, SearchFilters] = {}
    for source in sources:
        env_name = "AREA_" + source.upper().replace(".", "_")  # AREA_HH_RU, AREA_RABOTA_BY
        area = os.environ.get(env_name)
        filters[source] = SearchFilters(
            text=text,
            area=(area.strip() if area is not None else KNOWN_SOURCES[source]["default_area"]),
            experience=experience,
            professional_roles=roles,
            extra_params=extra,
        )

    # Railway exposes the mount path of an attached volume; prefer an explicit
    # DATA_DIR, then the volume, then the working directory.
    data_dir = Path(_get("DATA_DIR") or _get("RAILWAY_VOLUME_MOUNT_PATH") or "./data")
    data_dir.mkdir(parents=True, exist_ok=True)

    threshold = _get_int("SCORE_THRESHOLD", 60)
    if not 0 <= threshold <= 100:
        raise ConfigError("SCORE_THRESHOLD must be between 0 and 100")

    heartbeat_hour = _get_int("HEARTBEAT_HOUR", 9)
    if not 0 <= heartbeat_hour <= 23:
        raise ConfigError("HEARTBEAT_HOUR must be between 0 and 23")

    return Config(
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        anthropic_api_key=anthropic_api_key,
        claude_model=_get("CLAUDE_MODEL", "claude-opus-5"),
        claude_effort=_get("CLAUDE_EFFORT", "low"),
        hh_access_token=_get("HH_ACCESS_TOKEN"),
        hh_user_agent=hh_user_agent,
        sources=sources,
        filters=filters,
        poll_interval_seconds=max(60, _get_int("POLL_INTERVAL_SECONDS", 300)),
        per_page=min(100, max(1, _get_int("SEARCH_PER_PAGE", 50))),
        first_run_batch=max(0, _get_int("FIRST_RUN_BATCH", 10)),
        threshold=threshold,
        heartbeat_hour=heartbeat_hour,
        timezone=_get("TIMEZONE", "Europe/Minsk"),
        data_dir=data_dir,
        resume_file=Path(_get("RESUME_FILE", "resume.txt")),
        resume_text=_get("RESUME_TEXT"),
    )

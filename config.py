"""Central configuration for ReportFix AI.

All secrets come from environment variables (.env). Nothing secret is hard-coded
and nothing here is ever rendered in the UI except non-secret flags.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

try:  # python-dotenv is optional at runtime; requirements.txt installs it.
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:  # pragma: no cover
    pass


def _flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def ai_api_key() -> str:
    return os.getenv("AI_API_KEY", "").strip()


def ai_model() -> str:
    return os.getenv("AI_MODEL", "").strip()


def ai_base_url() -> str:
    return os.getenv("AI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")


def ai_embed_model() -> str:
    return os.getenv("AI_EMBED_MODEL", "").strip()


AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "20"))
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "2"))

CAMPUS_NAME = os.getenv("CAMPUS_NAME", "Demonstration Military College").strip()
CAMPUS_WEBSITE = os.getenv("CAMPUS_WEBSITE", "").strip()

# Judge Mode is OFF unless explicitly enabled in the local .env file.
JUDGE_MODE = _flag("JUDGE_MODE", False)

DB_PATH = os.getenv("REPORTFIX_DB", str(BASE_DIR / "data" / "reportfix.db"))

# Calendar days for trust recovery are counted in campus local time.
CAMPUS_UTC_OFFSET_HOURS = float(os.getenv("CAMPUS_UTC_OFFSET_HOURS", "5"))
CAMPUS_TZ = timezone(timedelta(hours=CAMPUS_UTC_OFFSET_HOURS))

MAX_COMPLAINT_CHARS = 2000

# SLA acknowledgment targets in minutes (configurable demo targets, not official procedure).
SLA_MINUTES = {
    "Critical": int(os.getenv("SLA_CRITICAL_MIN", "15")),
    "High": int(os.getenv("SLA_HIGH_MIN", "120")),
    "Medium": int(os.getenv("SLA_MEDIUM_MIN", "1440")),
    "Low": int(os.getenv("SLA_LOW_MIN", "4320")),
}


def ai_configured() -> bool:
    return bool(ai_api_key() and ai_model())


def local_today(now_utc: datetime | None = None):
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(CAMPUS_TZ).date()

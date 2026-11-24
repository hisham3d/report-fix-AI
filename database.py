"""SQLite persistence layer. All queries elsewhere use parameterized SQL."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

# --------------------------------------------------------------------------
# Clock (overridable in tests so trust recovery / SLA can be exercised)
# --------------------------------------------------------------------------
_clock_offset = timedelta(0)


def utcnow() -> datetime:
    return datetime.now(timezone.utc) + _clock_offset


def set_clock_offset(delta: timedelta) -> None:
    global _clock_offset
    _clock_offset = delta


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cms_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('student','staff','admin')),
    department_code TEXT REFERENCES departments(code),
    display_name TEXT,
    is_conf_handler INTEGER NOT NULL DEFAULT 0,
    is_conf_officer INTEGER NOT NULL DEFAULT 0,
    trust_score INTEGER NOT NULL DEFAULT 100 CHECK (trust_score BETWEEN 0 AND 100),
    last_recovery_date TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    department_code TEXT NOT NULL REFERENCES departments(code),
    kind TEXT NOT NULL DEFAULT 'facility',
    verification_source TEXT NOT NULL DEFAULT 'Admin entry',
    verification_status TEXT NOT NULL DEFAULT 'Verified'
        CHECK (verification_status IN ('Verified','Suggested','Rejected')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS location_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id TEXT NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    alias TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (location_id, alias)
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT UNIQUE,
    department_code TEXT NOT NULL REFERENCES departments(code),
    location_id TEXT NOT NULL REFERENCES locations(id),
    category TEXT NOT NULL,
    normalized_problem TEXT NOT NULL,
    title TEXT NOT NULL,
    base_priority TEXT NOT NULL,
    priority_score INTEGER NOT NULL,
    priority TEXT NOT NULL,
    is_safety_hazard INTEGER NOT NULL DEFAULT 0,
    hazard_type TEXT,
    status TEXT NOT NULL,
    routing_reason TEXT,
    location_reason TEXT,
    priority_reason TEXT,
    escalation_reason TEXT,
    recurrence_of INTEGER REFERENCES issues(id),
    recurrence_count INTEGER NOT NULL DEFAULT 0,
    embedding TEXT,
    analysis_mode TEXT,
    reopen_count INTEGER NOT NULL DEFAULT 0,
    needs_admin_review INTEGER NOT NULL DEFAULT 0,
    admin_review_reason TEXT,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at TEXT,
    closed_at TEXT,
    last_update_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_dept ON issues(department_code, status);
CREATE INDEX IF NOT EXISTS idx_issues_loc_cat ON issues(location_id, category, status);

CREATE TABLE IF NOT EXISTS report_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    issue_id INTEGER REFERENCES issues(id),
    text TEXT NOT NULL,
    analysis_json TEXT,
    outcome TEXT NOT NULL,
    clarification_question TEXT,
    clarification_count INTEGER NOT NULL DEFAULT 0,
    suggested_locations TEXT,
    is_hazard INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_user ON report_submissions(user_id, created_at);

CREATE TABLE IF NOT EXISTS issue_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    via TEXT NOT NULL CHECK (via IN ('report','me_too')),
    trust_snapshot INTEGER NOT NULL,
    suspicious INTEGER NOT NULL DEFAULT 0,
    joined_at TEXT NOT NULL,
    UNIQUE (issue_id, user_id)
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT,
    actor_role TEXT,
    actor_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolution_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    round_no INTEGER NOT NULL,
    resolution_note TEXT NOT NULL,
    resolved_by INTEGER REFERENCES users(id),
    eligible_user_ids TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    outcome TEXT,
    outcome_reason TEXT,
    UNIQUE (issue_id, round_no)
);

CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES resolution_rounds(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    vote TEXT NOT NULL CHECK (vote IN ('confirm','dispute')),
    reason TEXT,
    trust_snapshot INTEGER NOT NULL,
    weight REAL NOT NULL,
    suspicious INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (round_id, user_id)
);

CREATE TABLE IF NOT EXISTS trust_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    previous_score INTEGER NOT NULL,
    new_score INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    issue_id INTEGER,
    vote_id INTEGER,
    verification_source TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Confidential data: department-visible report content holds NO reporter link.
CREATE TABLE IF NOT EXISTS confidential_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_id TEXT NOT NULL UNIQUE,
    department_code TEXT REFERENCES departments(code),
    location_text TEXT,
    category TEXT NOT NULL,
    sanitized_text TEXT NOT NULL,
    priority TEXT NOT NULL,
    is_safety_hazard INTEGER NOT NULL DEFAULT 0,
    queue TEXT NOT NULL CHECK (queue IN ('department','admin_review','independent')),
    status TEXT NOT NULL,
    routing_reason TEXT,
    confidentiality_reason TEXT,
    location_needs_clarification INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confidential_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES confidential_reports(id) ON DELETE CASCADE,
    author_role TEXT NOT NULL CHECK (author_role IN ('reporter','handler','system')),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Isolated reporter association. Only privacy_engine reads this table, and only
-- for the authenticated reporter's own view.
CREATE TABLE IF NOT EXISTS confidential_associations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL UNIQUE REFERENCES confidential_reports(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS recurrence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    new_issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    previous_issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    occurrence_no INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (new_issue_id, previous_issue_id)
);

CREATE TABLE IF NOT EXISTS suspicious_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    issue_id INTEGER,
    round_id INTEGER,
    user_ids TEXT,
    details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','cleared','confirmed_abuse')),
    review_reason TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    actor_role TEXT,
    action TEXT NOT NULL,
    target TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);
"""

DATA_TABLES_TO_CLEAR = (
    "votes", "resolution_rounds", "status_history", "issue_participants", "recurrence_links",
    "report_submissions", "notifications", "confidential_updates", "confidential_associations",
    "confidential_reports", "suspicious_activity", "trust_history", "issues",
)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    from constants import DEPARTMENTS

    with conn:
        for code, name in DEPARTMENTS.items():
            conn.execute("INSERT OR IGNORE INTO departments(code, name) VALUES (?, ?)", (code, name))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('schema_version', '1')")
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('ai_disabled', '0')")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit transaction; nested calls join the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def audit(conn, actor: dict | None, action: str, target: str = "", reason: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_logs(actor_id, actor_role, action, target, reason, created_at) VALUES (?,?,?,?,?,?)",
        (actor["id"] if actor else None, actor["role"] if actor else "system", action, target, reason, now_iso()),
    )


def row_to_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn, sql: str, params: tuple = ()) -> dict | None:
    return row_to_dict(conn.execute(sql, params).fetchone())


def clear_complaint_data(conn) -> None:
    with transaction(conn):
        for t in DATA_TABLES_TO_CLEAR:
            conn.execute(f"DELETE FROM {t}")  # table names are constants, not user input
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('issues','confidential_reports')")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)

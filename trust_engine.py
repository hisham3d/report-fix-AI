"""Student trust (credit) score: bounded 0..100, idempotent events, daily recovery."""
from __future__ import annotations

from datetime import date

import config
import database as db

REWARD = 10
PENALTY = -10
DAILY_RECOVERY = 5


def clamp(v: int) -> int:
    return max(0, min(100, int(v)))


def apply_change(conn, user_id: int, delta: int, reason: str, event_key: str, source: str,
                 issue_id: int | None = None, vote_id: int | None = None) -> dict | None:
    """Apply a trust change exactly once per event_key. Returns the history row or None if duplicate."""
    with db.transaction(conn):
        if db.one(conn, "SELECT id FROM trust_history WHERE event_key = ?", (event_key,)):
            return None
        user = db.one(conn, "SELECT id, role, trust_score FROM users WHERE id = ?", (user_id,))
        if not user or user["role"] != "student":
            return None
        prev = user["trust_score"]
        new = clamp(prev + delta)
        conn.execute("UPDATE users SET trust_score = ? WHERE id = ?", (new, user_id))
        conn.execute(
            """INSERT INTO trust_history(user_id, previous_score, new_score, delta, reason, event_key, issue_id, vote_id,
                   verification_source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, prev, new, new - prev, reason, event_key, issue_id, vote_id, source, db.now_iso()),
        )
        if new != prev:
            conn.execute("INSERT INTO notifications(user_id, message, created_at) VALUES (?,?,?)",
                         (user_id, f"Your credit score changed {prev} → {new}: {reason}", db.now_iso()))
    return {"previous": prev, "new": new, "delta": new - prev}


def apply_daily_recovery(conn, user_id: int, today: date | None = None) -> int:
    """+5 per elapsed calendar day since last recovery; works after the app was closed."""
    today = today or config.local_today(db.utcnow())
    with db.transaction(conn):
        user = db.one(conn, "SELECT id, role, trust_score, last_recovery_date FROM users WHERE id = ?", (user_id,))
        if not user or user["role"] != "student":
            return 0
        last = date.fromisoformat(user["last_recovery_date"]) if user["last_recovery_date"] else today
        days = (today - last).days
        if days <= 0:
            return 0
        prev = user["trust_score"]
        new = clamp(prev + DAILY_RECOVERY * days)
        conn.execute("UPDATE users SET trust_score = ?, last_recovery_date = ? WHERE id = ?",
                     (new, today.isoformat(), user_id))
        if new != prev:
            conn.execute(
                """INSERT INTO trust_history(user_id, previous_score, new_score, delta, reason, event_key,
                       verification_source, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (user_id, prev, new, new - prev, f"Daily recovery ({days} day(s) × +{DAILY_RECOVERY})",
                 f"recovery:{user_id}:{today.isoformat()}", "System daily recovery", db.now_iso()),
            )
    return new - prev


def history(conn, user_id: int) -> list[dict]:
    return db.rows(conn, """SELECT created_at, previous_score, new_score, delta, reason, issue_id, vote_id,
                                   verification_source FROM trust_history WHERE user_id = ? ORDER BY id DESC""", (user_id,))


# Convenience wrappers for the rule table
def reward_verified_report(conn, user_id, issue_id, source):
    return apply_change(conn, user_id, REWARD, f"Verified genuine report (issue #{issue_id})",
                        f"genuine_report:{issue_id}:{user_id}", source, issue_id=issue_id)


def reward_accurate_vote(conn, user_id, vote_id, issue_id, source):
    return apply_change(conn, user_id, REWARD, f"Independently verified accurate vote (issue #{issue_id})",
                        f"accurate_vote:{vote_id}", source, issue_id=issue_id, vote_id=vote_id)


def penalize_false_report(conn, user_id, issue_id, source):
    return apply_change(conn, user_id, PENALTY, f"Proven deliberately false report (issue #{issue_id})",
                        f"false_report:{issue_id}:{user_id}", source, issue_id=issue_id)


def penalize_false_vote(conn, user_id, vote_id, issue_id, source):
    return apply_change(conn, user_id, PENALTY, f"Proven deliberately false vote (issue #{issue_id})",
                        f"false_vote:{vote_id}", source, issue_id=issue_id, vote_id=vote_id)


def penalize_abuse(conn, user_id, flag_id, source):
    return apply_change(conn, user_id, PENALTY, f"Confirmed spam/abuse (review flag #{flag_id})",
                        f"abuse:{flag_id}:{user_id}", source)

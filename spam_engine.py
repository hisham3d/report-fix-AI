"""Spam / brigading detection. Flags go to admin review; nobody is punished automatically."""
from __future__ import annotations

import json
from datetime import timedelta

import auth
import database as db
import trust_engine

NEW_ACCOUNT_HOURS = 24
CLUSTER_WINDOW_MIN = 30
CLUSTER_MIN = 3
RATE_LIMIT_COUNT = 8
RATE_LIMIT_WINDOW_MIN = 10


def is_new_account(user: dict, at=None) -> bool:
    at = at or db.utcnow()
    created = db.parse_ts(user["created_at"])
    return (at - created) < timedelta(hours=NEW_ACCOUNT_HOURS)


def rate_limited(conn, user_id: int) -> bool:
    since = (db.utcnow() - timedelta(minutes=RATE_LIMIT_WINDOW_MIN)).isoformat(timespec="seconds")
    n = db.one(conn, "SELECT COUNT(*) c FROM report_submissions WHERE user_id = ? AND created_at >= ?",
               (user_id, since))["c"]
    return n >= RATE_LIMIT_COUNT


def _open_flag(conn, kind, issue_id=None, round_id=None, user_ids=None, details=""):
    existing = db.one(conn, """SELECT id, user_ids FROM suspicious_activity WHERE kind = ? AND status = 'open'
                               AND IFNULL(issue_id, -1) = IFNULL(?, -1) AND IFNULL(round_id, -1) = IFNULL(?, -1)""",
                      (kind, issue_id, round_id))
    ids = sorted(set(user_ids or []))
    if existing:
        merged = sorted(set(json.loads(existing["user_ids"] or "[]")) | set(ids))
        conn.execute("UPDATE suspicious_activity SET user_ids = ?, details = ? WHERE id = ?",
                     (json.dumps(merged), details, existing["id"]))
        return existing["id"]
    cur = conn.execute("""INSERT INTO suspicious_activity(kind, issue_id, round_id, user_ids, details, created_at)
                          VALUES (?,?,?,?,?,?)""", (kind, issue_id, round_id, json.dumps(ids), details, db.now_iso()))
    return cur.lastrowid


def check_repeat_submission(conn, user_id: int, issue_id: int) -> bool:
    since = (db.utcnow() - timedelta(hours=24)).isoformat(timespec="seconds")
    n = db.one(conn, """SELECT COUNT(*) c FROM report_submissions WHERE user_id = ? AND issue_id = ?
                        AND outcome = 'already_reported' AND created_at >= ?""", (user_id, issue_id, since))["c"]
    if n >= 2:
        _open_flag(conn, "repeated_submission", issue_id=issue_id, user_ids=[user_id],
                   details=f"Same account re-submitted an issue it already joined {n + 1} times in 24h.")
        return True
    return False


def record_injection_attempt(conn, user_id: int) -> bool:
    since = (db.utcnow() - timedelta(hours=24)).isoformat(timespec="seconds")
    n = db.one(conn, """SELECT COUNT(*) c FROM report_submissions WHERE user_id = ? AND created_at >= ?
                        AND analysis_json LIKE '%"possible_prompt_injection": true%'""", (user_id, since))["c"]
    if n >= 3:
        _open_flag(conn, "repeated_instruction_override", user_ids=[user_id],
                   details=f"{n} reports containing instruction-override attempts in 24h (needs human review, no automatic penalty).")
        return True
    return False


def check_join_cluster(conn, issue_id: int) -> list[int]:
    since = (db.utcnow() - timedelta(minutes=CLUSTER_WINDOW_MIN)).isoformat(timespec="seconds")
    parts = db.rows(conn, """SELECT p.id, p.user_id, p.joined_at, u.created_at FROM issue_participants p
                             JOIN users u ON u.id = p.user_id WHERE p.issue_id = ? AND p.joined_at >= ?""",
                    (issue_id, since))
    new_ones = [p for p in parts if (db.parse_ts(p["joined_at"]) - db.parse_ts(p["created_at"])) < timedelta(hours=NEW_ACCOUNT_HOURS)]
    if len(new_ones) >= CLUSTER_MIN:
        ids = [p["user_id"] for p in new_ones]
        conn.execute(f"UPDATE issue_participants SET suspicious = 1 WHERE issue_id = ? AND user_id IN ({','.join('?' * len(ids))})",
                     (issue_id, *ids))
        _open_flag(conn, "new_account_cluster", issue_id=issue_id, user_ids=ids,
                   details=f"{len(ids)} accounts younger than {NEW_ACCOUNT_HOURS}h joined this issue within "
                           f"{CLUSTER_WINDOW_MIN} minutes. Excluded from crowd escalation until reviewed.")
        return ids
    return []


def check_vote_cluster(conn, round_id: int, issue_id: int) -> list[int]:
    since = (db.utcnow() - timedelta(minutes=CLUSTER_WINDOW_MIN)).isoformat(timespec="seconds")
    votes = db.rows(conn, """SELECT v.id, v.user_id, v.vote, v.created_at, u.created_at AS ucreated FROM votes v
                             JOIN users u ON u.id = v.user_id WHERE v.round_id = ? AND v.created_at >= ?""",
                    (round_id, since))
    flagged = []
    for choice in ("confirm", "dispute"):
        new_votes = [v for v in votes if v["vote"] == choice and
                     (db.parse_ts(v["created_at"]) - db.parse_ts(v["ucreated"])) < timedelta(hours=NEW_ACCOUNT_HOURS)]
        if len(new_votes) >= CLUSTER_MIN:
            ids = [v["user_id"] for v in new_votes]
            conn.execute(f"UPDATE votes SET suspicious = 1 WHERE round_id = ? AND user_id IN ({','.join('?' * len(ids))})",
                         (round_id, *ids))
            _open_flag(conn, "coordinated_votes", issue_id=issue_id, round_id=round_id, user_ids=ids,
                       details=f"{len(ids)} brand-new accounts cast '{choice}' votes within {CLUSTER_WINDOW_MIN} minutes. "
                               "Excluded from automatic outcome until reviewed.")
            flagged += ids
    return flagged


def open_flags(conn) -> list[dict]:
    return db.rows(conn, "SELECT * FROM suspicious_activity ORDER BY status = 'open' DESC, id DESC")


def review_flag(conn, admin: dict, flag_id: int, decision: str, reason: str) -> dict:
    auth.require_role(conn, admin, "admin")
    if decision not in ("cleared", "confirmed_abuse"):
        raise ValueError("Invalid decision.")
    if not reason.strip():
        raise ValueError("A reason is required.")
    flag = db.one(conn, "SELECT * FROM suspicious_activity WHERE id = ?", (flag_id,))
    if not flag or flag["status"] != "open":
        raise ValueError("Flag not found or already reviewed.")
    ids = json.loads(flag["user_ids"] or "[]")
    with db.transaction(conn):
        conn.execute("UPDATE suspicious_activity SET status = ?, review_reason = ?, reviewed_at = ? WHERE id = ?",
                     (decision, reason, db.now_iso(), flag_id))
        if decision == "cleared" and ids:
            ph = ",".join("?" * len(ids))
            if flag["issue_id"]:
                conn.execute(f"UPDATE issue_participants SET suspicious = 0 WHERE issue_id = ? AND user_id IN ({ph})",
                             (flag["issue_id"], *ids))
            if flag["round_id"]:
                conn.execute(f"UPDATE votes SET suspicious = 0 WHERE round_id = ? AND user_id IN ({ph})",
                             (flag["round_id"], *ids))
        db.audit(conn, admin, f"spam_review:{decision}", f"flag:{flag_id}", reason)
    if decision == "confirmed_abuse":
        for uid in ids:
            trust_engine.penalize_abuse(conn, uid, flag_id, f"Admin review ({admin['cms_id']})")
    return flag

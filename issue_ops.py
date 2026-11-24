"""Low-level issue state helpers shared by services (status history, priority recompute)."""
from __future__ import annotations

import database as db
import notifications
import priority_engine
import trust_engine
from constants import S_CLOSED


def get_issue(conn, issue_id: int) -> dict | None:
    issue = db.one(conn, """SELECT i.*, l.name AS location_name FROM issues i JOIN locations l ON l.id = i.location_id
                            WHERE i.id = ?""", (issue_id,))
    if issue:
        issue["affected_count"] = db.one(conn, "SELECT COUNT(*) c FROM issue_participants WHERE issue_id = ?",
                                         (issue_id,))["c"]
    return issue


def set_status(conn, issue_id: int, new_status: str, note: str = "", actor: dict | None = None) -> None:
    issue = db.one(conn, "SELECT status FROM issues WHERE id = ?", (issue_id,))
    now = db.now_iso()
    extra = ""
    if new_status == S_CLOSED:
        extra = ", closed_at = :now"
    conn.execute(f"UPDATE issues SET status = :s, last_update_at = :now{extra} WHERE id = :id",
                 {"s": new_status, "now": now, "id": issue_id})
    conn.execute("""INSERT INTO status_history(issue_id, from_status, to_status, note, actor_role, actor_id, created_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (issue_id, issue["status"] if issue else None, new_status, note,
                  actor["role"] if actor else "system", actor["id"] if actor else None, now))


def add_history_note(conn, issue_id: int, note: str, actor: dict) -> None:
    issue = db.one(conn, "SELECT status FROM issues WHERE id = ?", (issue_id,))
    now = db.now_iso()
    conn.execute("""INSERT INTO status_history(issue_id, from_status, to_status, note, actor_role, actor_id, created_at)
                    VALUES (?,?,?,?,?,?,?)""", (issue_id, issue["status"], issue["status"], note, actor["role"], actor["id"], now))
    conn.execute("UPDATE issues SET last_update_at = ? WHERE id = ?", (now, issue_id))


def recompute_priority(conn, issue_id: int) -> tuple[str, str]:
    """Recalculate the deterministic score. Returns (old_priority, new_priority)."""
    issue = db.one(conn, "SELECT * FROM issues WHERE id = ?", (issue_id,))
    parts = db.rows(conn, "SELECT trust_snapshot, suspicious FROM issue_participants WHERE issue_id = ?", (issue_id,))
    weights = [priority_engine.trust_weight(p["trust_snapshot"]) for p in parts if not p["suspicious"]]
    score, label, expl = priority_engine.compute_priority(issue["base_priority"], bool(issue["is_safety_hazard"]),
                                                          weights, issue["recurrence_count"], len(parts))
    conn.execute("UPDATE issues SET priority_score = ?, priority = ?, escalation_reason = ? WHERE id = ?",
                 (score, label, expl, issue_id))
    if label != issue["priority"]:
        conn.execute("""INSERT INTO status_history(issue_id, from_status, to_status, note, actor_role, created_at)
                        VALUES (?,?,?,?,?,?)""",
                     (issue_id, issue["status"], issue["status"], f"Priority changed {issue['priority']} → {label}. {expl}",
                      "system", db.now_iso()))
    return issue["priority"], label


def close_verified(conn, issue_id: int, note: str, source: str, actor: dict | None = None) -> None:
    """Close an issue as verified and reward the genuine reporters once."""
    set_status(conn, issue_id, S_CLOSED, note, actor)
    notifications.notify_participants(conn, issue_id, f"Issue #{issue_id} was verified as fixed and closed.")
    reporters = db.rows(conn, "SELECT user_id FROM issue_participants WHERE issue_id = ? AND via = 'report' AND suspicious = 0",
                        (issue_id,))
    for r in reporters:
        trust_engine.reward_verified_report(conn, r["user_id"], issue_id, source)

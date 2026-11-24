"""In-app notifications. Never used for confidential reports (that would link reporter and report)."""
from __future__ import annotations

import database as db


def notify(conn, user_id: int, message: str) -> None:
    conn.execute("INSERT INTO notifications(user_id, message, created_at) VALUES (?,?,?)",
                 (user_id, message, db.now_iso()))


def notify_participants(conn, issue_id: int, message: str, exclude: int | None = None) -> None:
    for p in db.rows(conn, "SELECT user_id FROM issue_participants WHERE issue_id = ?", (issue_id,)):
        if p["user_id"] != exclude:
            notify(conn, p["user_id"], message)


def list_for(conn, user_id: int, limit: int = 50) -> list[dict]:
    return db.rows(conn, "SELECT id, message, is_read, created_at FROM notifications WHERE user_id = ? "
                         "ORDER BY id DESC LIMIT ?", (user_id, limit))


def unread_count(conn, user_id: int) -> int:
    return db.one(conn, "SELECT COUNT(*) c FROM notifications WHERE user_id = ? AND is_read = 0", (user_id,))["c"]


def mark_all_read(conn, user_id: int) -> None:
    conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user_id,))

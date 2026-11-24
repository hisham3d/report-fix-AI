"""Confidential reporting, redaction and identity protection.

Design:
* confidential_reports holds sanitized content and NO reporter reference.
* confidential_associations links a report to its reporter; only the functions
  in this module read it, and only to serve the authenticated reporter's own view.
* Confidential reports never enter issues / public boards / duplicate grouping /
  notifications / exports / AI summaries.
"""
from __future__ import annotations

import re
import secrets

import auth
import database as db
from constants import S_CLOSED, S_IN_PROGRESS, S_REOPENED, S_RESOLVED, S_SUBMITTED

# ---------------------------------------------------------------- redaction
_REDACTIONS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[REDACTED EMAIL]"),
    (re.compile(r"(?:\+92|0092|0)?3\d{2}[\s-]?\d{7}\b"), "[REDACTED PHONE]"),
    (re.compile(r"\+?\d[\d\s-]{8,}\d"), "[REDACTED NUMBER]"),
    (re.compile(r"\b(?:cms|cms\s*id|cms\s*no|roll\s*no|reg(?:istration)?\s*no)\s*[:#-]?\s*[\w-]+", re.I), "[REDACTED ID]"),
    (re.compile(r"\b\d{5,}\b"), "[REDACTED ID]"),
    (re.compile(r"\b(my name is|i am called|mera naam|mera nam)\s+[A-Za-z][A-Za-z.]*(\s+[A-Z][a-z]+)?", re.I), r"\1 [REDACTED NAME]"),
    (re.compile(r"\b(this is|i am|i'm|main)\s+([A-Z][a-z]+)(\s+[A-Z][a-z]+)?\s+(from|of|se)\b"), r"\1 [REDACTED NAME] \4"),
]


def redact(text: str) -> str:
    out = text or ""
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out


# ------------------------------------------------- rule-based sensitivity check
_CONF_RULES = [
    ("harassment", r"\bharass\w*|\bharas\w*|\btang\s+(?:kar|kr|karte|krte|kiya|kia|ho)\w*|\bchher\w*|\bcheer\s*khani|inappropriate(?:ly)?\s+touch|sexual\w*|\bstalk\w*|peecha\s+kar|picha\s+kar"),
    ("ragging", r"\bragg\w*"),
    ("bullying", r"\bbull(?:y|ies|ied|ying)\b"),
    ("intimidation", r"\bintimidat\w*|\bdara\s+(?:rahe|rhe|raha|rha)\b|\bdhamka\w*"),
    ("discrimination", r"\bdiscriminat\w*|\bracis\w*|\bsectarian\b"),
    ("abuse", r"\babus\w*|\bgaali\w*|\bgaliy\w*|\bmaar\s*peet|\bbeat(?:ing|en)?\s+(?:me|us|him|her)|\bmaar(?:a|te|tay|ty)\b|\bblackmail\w*"),
    ("threats", r"\bthreat\w*|\bdhamki\w*|\bkill\s+(?:me|you|him|her)\b|\bjaan\s+se\s+maar"),
    ("retaliation", r"\bretaliat\w*|\bbadla\s+(?:le|lene)"),
]

_STAFF_TERMS = r"\b(teacher|instructor|lecturer|professor|faculty|staff\s*member|lab\s*(?:attendant|engineer|incharge)|supervisor|hod|sir|madam|ustad|ustaad|officer|warden)\b"


def detect_confidential(text: str) -> tuple[bool, str | None, str]:
    low = (text or "").lower()
    for cat, pat in _CONF_RULES:
        m = re.search(pat, low)
        if m:
            return True, cat, f"The report describes {cat} ('{m.group(0)}'), which must be handled confidentially."
    return False, None, ""


def detect_staff_misconduct(text: str) -> bool:
    return bool(re.search(_STAFF_TERMS, (text or "").lower()))


def new_tracking_id(conn) -> str:
    while True:
        tid = "RF-CONF-" + secrets.token_hex(3).upper()
        if not db.one(conn, "SELECT id FROM confidential_reports WHERE tracking_id = ?", (tid,)):
            return tid


def _handler_exists(conn, dept: str | None) -> bool:
    if not dept:
        return False
    return bool(db.one(conn, "SELECT id FROM users WHERE role='staff' AND is_conf_handler=1 AND active=1 "
                             "AND department_code=?", (dept,)))


def create_confidential_report(conn, user_id: int, text: str, department_code: str | None, location_text: str | None,
                               category: str, priority: str, is_hazard: bool, conf_reason: str,
                               location_needs_clarification: bool) -> dict:
    sanitized = redact(text)
    staff_misconduct = detect_staff_misconduct(text)
    if staff_misconduct:
        queue = "independent"
        routing = ("The report may concern staff conduct, so it was sent to the independent confidential review "
                   "route instead of the department.")
    elif _handler_exists(conn, department_code):
        queue = "department"
        routing = f"Sent only to the authorized confidential handler of {department_code}."
    else:
        queue = "admin_review"
        routing = ("No authorized confidential handler is registered for the relevant department"
                   + (f" ({department_code})" if department_code else " (department not identified)")
                   + ", so it was sent to the restricted confidential review queue.")
    now = db.now_iso()
    with db.transaction(conn):
        tid = new_tracking_id(conn)
        cur = conn.execute(
            """INSERT INTO confidential_reports(tracking_id, department_code, location_text, category, sanitized_text,
                   priority, is_safety_hazard, queue, status, routing_reason, confidentiality_reason,
                   location_needs_clarification, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, department_code, location_text, category, sanitized, priority, int(is_hazard), queue, S_SUBMITTED,
             routing, conf_reason, int(location_needs_clarification), now, now),
        )
        rid = cur.lastrowid
        conn.execute("INSERT INTO confidential_associations(report_id, user_id) VALUES (?, ?)", (rid, user_id))
        msg = "Report received and protected."
        if location_needs_clarification:
            msg += " Location was not specified; you may add details privately below."
        conn.execute("INSERT INTO confidential_updates(report_id, author_role, message, created_at) VALUES (?,?,?,?)",
                     (rid, "system", msg, now))
    return {"tracking_id": tid, "queue": queue, "routing_reason": routing, "report_id": rid}


# ------------------------------------------------------------- access paths
_HANDLER_COLUMNS = ("id, tracking_id, department_code, location_text, category, sanitized_text, priority, "
                    "is_safety_hazard, queue, status, routing_reason, confidentiality_reason, "
                    "location_needs_clarification, created_at, updated_at")


def handler_queue(conn, actor: dict) -> list[dict]:
    """Sanitized reports for an authorized handler. Never includes reporter data."""
    user = auth.fresh(conn, actor)
    if user["role"] == "staff" and user["is_conf_officer"]:
        return db.rows(conn, f"SELECT {_HANDLER_COLUMNS} FROM confidential_reports "
                             "WHERE queue IN ('admin_review','independent') ORDER BY is_safety_hazard DESC, created_at")
    if user["role"] == "staff" and user["is_conf_handler"]:
        return db.rows(conn, f"SELECT {_HANDLER_COLUMNS} FROM confidential_reports "
                             "WHERE queue='department' AND department_code=? ORDER BY is_safety_hazard DESC, created_at",
                       (user["department_code"],))
    raise auth.PermissionDenied("Only authorized confidential handlers can open the confidential queue.")


def _handler_can_access(user: dict, report: dict) -> bool:
    if user["role"] != "staff":
        return False
    if user["is_conf_officer"] and report["queue"] in ("admin_review", "independent"):
        return True
    return bool(user["is_conf_handler"] and report["queue"] == "department"
                and report["department_code"] == user["department_code"])


def get_report_for_handler(conn, actor: dict, report_id: int) -> dict:
    user = auth.fresh(conn, actor)
    report = db.one(conn, f"SELECT {_HANDLER_COLUMNS} FROM confidential_reports WHERE id = ?", (report_id,))
    if not report or not _handler_can_access(user, report):
        raise auth.PermissionDenied("Access denied to this confidential report.")
    report["updates"] = db.rows(conn, "SELECT author_role, message, created_at FROM confidential_updates "
                                      "WHERE report_id = ? ORDER BY id", (report_id,))
    report["reporter_display"] = "Anonymous Student"
    return report


def handler_update(conn, actor: dict, report_id: int, new_status: str | None, message: str) -> None:
    report = get_report_for_handler(conn, actor, report_id)
    allowed = {None, S_IN_PROGRESS, S_RESOLVED}
    if new_status not in allowed:
        raise ValueError("Handlers may set In Progress or Resolved; closure requires the reporter's private confirmation.")
    if not (message or "").strip():
        raise ValueError("A note is required.")
    if new_status == S_RESOLVED and report["status"] == S_CLOSED:
        raise ValueError("Report already closed.")
    now = db.now_iso()
    with db.transaction(conn):
        if new_status:
            conn.execute("UPDATE confidential_reports SET status=?, updated_at=? WHERE id=?", (new_status, now, report_id))
        conn.execute("INSERT INTO confidential_updates(report_id, author_role, message, created_at) VALUES (?,?,?,?)",
                     (report_id, "handler", redact(message.strip()), now))


def my_confidential_reports(conn, actor: dict) -> list[dict]:
    """The reporter's own private list (requires authenticated identity)."""
    user = auth.fresh(conn, actor)
    if user["role"] != "student":
        return []
    reports = db.rows(conn, """SELECT r.id, r.tracking_id, r.category, r.status, r.priority, r.department_code,
                                      r.location_text, r.queue, r.created_at, r.updated_at, r.sanitized_text
                               FROM confidential_reports r JOIN confidential_associations a ON a.report_id = r.id
                               WHERE a.user_id = ? ORDER BY r.created_at DESC""", (user["id"],))
    for r in reports:
        r["updates"] = db.rows(conn, "SELECT author_role, message, created_at FROM confidential_updates "
                                     "WHERE report_id = ? ORDER BY id", (r["id"],))
    return reports


def _assert_reporter(conn, user_id: int, report_id: int) -> None:
    if not db.one(conn, "SELECT id FROM confidential_associations WHERE report_id=? AND user_id=?", (report_id, user_id)):
        raise auth.PermissionDenied("This is not your confidential report.")


def reporter_add_note(conn, actor: dict, report_id: int, message: str) -> None:
    user = auth.fresh(conn, actor)
    _assert_reporter(conn, user["id"], report_id)
    if not (message or "").strip():
        raise ValueError("Message cannot be empty.")
    now = db.now_iso()
    with db.transaction(conn):
        conn.execute("INSERT INTO confidential_updates(report_id, author_role, message, created_at) VALUES (?,?,?,?)",
                     (report_id, "reporter", redact(message.strip()[:1500]), now))
        conn.execute("UPDATE confidential_reports SET updated_at=?, location_needs_clarification=0 WHERE id=?",
                     (now, report_id))


def reporter_confirm(conn, actor: dict, report_id: int, resolved: bool, reason: str = "") -> None:
    """Private confirmation instead of public voting."""
    user = auth.fresh(conn, actor)
    _assert_reporter(conn, user["id"], report_id)
    report = db.one(conn, "SELECT status FROM confidential_reports WHERE id=?", (report_id,))
    if report["status"] != S_RESOLVED:
        raise ValueError("The handler has not marked this report resolved yet.")
    if not resolved and not reason.strip():
        raise ValueError("Please give a short reason.")
    new_status = S_CLOSED if resolved else S_REOPENED
    now = db.now_iso()
    with db.transaction(conn):
        conn.execute("UPDATE confidential_reports SET status=?, updated_at=? WHERE id=?", (new_status, now, report_id))
        msg = "Reporter privately confirmed the resolution." if resolved else f"Reporter says not resolved: {redact(reason)}"
        conn.execute("INSERT INTO confidential_updates(report_id, author_role, message, created_at) VALUES (?,?,?,?)",
                     (report_id, "system", msg, now))


def aggregate_stats(conn) -> dict:
    """Privacy-safe aggregate counts for ordinary admin dashboards."""
    total = db.one(conn, "SELECT COUNT(*) c FROM confidential_reports")["c"]
    by_status = db.rows(conn, "SELECT status, COUNT(*) count FROM confidential_reports GROUP BY status")
    by_queue = db.rows(conn, "SELECT queue, COUNT(*) count FROM confidential_reports GROUP BY queue")
    return {"total": total, "by_status": by_status, "by_queue": by_queue}

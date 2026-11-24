"""Complaint orchestration: AI understanding -> Python decisions -> persistence.

The AI layer only *understands* text. Every operational decision (location verification,
routing, priority, confidentiality, duplicate merge, permissions) is made here in code.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

import ai_engine
import auth
import config
import database as db
import duplicate_engine
import issue_ops
import location_engine
import notifications
import priority_engine
import privacy_engine
import spam_engine
import trust_engine
import voting_engine
from constants import (ACTIVE_STATUSES, CATEGORIES, DEPARTMENTS, HAZARD_CATEGORY, PRIORITY_BASE_SCORE, S_ASSIGNED,
                       S_CLOSED, S_IN_PROGRESS, S_PENDING, S_REJECTED, S_REOPENED, S_RESOLVED, S_SUBMITTED, SAFETY_GUIDANCE)


class SubmissionError(Exception):
    pass


# =========================================================== analysis (no writes)
def analyze_report(conn, text: str, chosen_location_id: str | None = None, manual_confidential: bool = False,
                   force_fallback: bool = False) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise SubmissionError("Please describe the problem — the report cannot be empty.")
    notes = []
    if len(raw) > config.MAX_COMPLAINT_CHARS:
        raw = raw[: config.MAX_COMPLAINT_CHARS]
        notes.append(f"Report was very long and was trimmed to {config.MAX_COMPLAINT_CHARS} characters.")
    registry = location_engine.load_registry(conn)
    analysis = ai_engine.analyze_complaint(raw, [l["name"] for l in registry], conn=conn, force_fallback=force_fallback)
    mode = analysis["mode"]

    det_inj, inj_hits = ai_engine.detect_injection(raw)
    injection = det_inj or bool(analysis.get("possible_prompt_injection"))
    clean = ai_engine.strip_injection(priority_engine.strip_emphasis(raw))

    det_hazard, det_htype, det_hreason = priority_engine.detect_hazard(clean)
    det_conf, det_conf_cat, det_conf_reason = privacy_engine.detect_confidential(raw)
    ai_conf = bool(analysis.get("is_confidential"))
    confidential = det_conf or ai_conf or manual_confidential
    if det_conf:
        conf_reason = det_conf_reason
    elif ai_conf:
        conf_reason = f"The AI recognised a sensitive {analysis.get('confidentiality_category') or 'personal safety'} complaint."
    elif manual_confidential:
        conf_reason = "The student chose to submit this report confidentially."
    else:
        conf_reason = ""

    category = analysis.get("issue_category") or "Other"
    if category not in CATEGORIES:
        category = "Other"
    if mode != "ai" and det_hazard and det_htype:
        category = HAZARD_CATEGORY[det_htype]
    if confidential and (det_conf or ai_conf) and category in ("Other", "Security/Personal Safety") and not det_hazard:
        category = "Harassment/Misconduct"
    normalized_problem = (analysis.get("normalized_problem") or "").strip() or "Unspecified campus problem"

    ai_hazard = bool(analysis.get("is_safety_hazard"))
    base_priority, is_hazard, priority_reason = priority_engine.decide_base_priority(
        category, clean, analysis.get("suggested_priority"), analysis.get("priority_reason"), ai_hazard,
        det_hazard, det_hreason, injection, mode)
    hazard_type = det_htype if det_hazard else (analysis.get("hazard_type") if is_hazard else None)

    genuine = bool(analysis.get("has_genuine_campus_problem", True)) or is_hazard or confidential
    if injection and category == "Other" and not is_hazard and not confidential:
        genuine = False

    decision = {
        "text": raw, "clean_text": clean, "notes": notes, "analysis": analysis, "mode": mode,
        "ai_error": analysis.get("ai_error"), "injection": injection, "injection_hits": inj_hits,
        "is_hazard": is_hazard, "hazard_type": hazard_type, "confidential": confidential,
        "confidentiality_category": det_conf_cat or analysis.get("confidentiality_category"),
        "confidentiality_reason": conf_reason, "genuine": genuine, "category": category,
        "normalized_problem": normalized_problem, "base_priority": base_priority, "priority_reason": priority_reason,
        "safety_warning": SAFETY_GUIDANCE if is_hazard else None, "reject_reason": None,
        "needs_clarification": False, "clarification_question": None,
    }
    if not genuine:
        decision["reject_reason"] = ("Rejected: the report contains only instructions to the system and no genuine campus "
                                     "problem. Reports are treated as data, not commands." if injection else
                                     "No campus problem was identified. Please describe what is wrong and where.")
        decision["location"] = location_engine._result("missing", reason="Not evaluated.")
        decision["department"] = None
        decision["routing_reason"] = ""
        return decision

    # ---- location
    if chosen_location_id:
        loc = location_engine.get_location(conn, chosen_location_id)
        if not loc:
            raise SubmissionError("The selected location is not in the verified registry.")
        location = location_engine._result("verified", loc, reason=f"The student selected the verified location '{loc['name']}'.",
                                           confidence=1.0)
    else:
        location = location_engine.resolve(conn, clean, analysis.get("location"), category)
    decision["location"] = location

    # ---- routing
    if location["status"] == "verified":
        dept = location["department"]
        ai_dept = analysis.get("department")
        routing = f"'{location['location_name']}' is a verified location owned by {dept} ({DEPARTMENTS[dept]})."
        if ai_dept and ai_dept != dept:
            routing += f" The AI suggested {ai_dept}, but the registered owner of the location takes precedence."
        elif ai_dept == dept:
            routing += " The AI's reading of the complaint agrees."
        decision["department"], decision["routing_reason"] = dept, routing
    else:
        decision["department"] = analysis.get("department") if confidential else None
        decision["routing_reason"] = "Not routed until the location is verified."

    if not confidential:
        if location["status"] != "verified":
            decision["needs_clarification"] = True
            decision["clarification_question"] = location.get("question") or "Please specify the location."
        elif category == "Other" and not is_hazard:
            decision["needs_clarification"] = True
            decision["clarification_question"] = (analysis.get("clarification_question") if mode == "ai" and analysis.get("clarification_question")
                                                  else f"What exactly is the problem at {location['location_name']}? (e.g. AC, WiFi, projector, furniture)")
    return decision


def explain(decision: dict) -> list[tuple[str, str]]:
    """Short human-readable 'Why this decision?' bullets."""
    out = []
    loc = decision.get("location") or {}
    if decision.get("confidential"):
        out.append(("Confidentiality", decision["confidentiality_reason"] + " Reporter identity is hidden; it never appears on public boards."))
    if loc.get("reason"):
        out.append(("Location", loc["reason"]))
    if decision.get("routing_reason"):
        out.append(("Department", decision["routing_reason"]))
    out.append(("Priority", f"{decision['base_priority']}: {decision['priority_reason']}"))
    if decision.get("injection"):
        out.append(("Security", "The report contained instructions aimed at the system; they were ignored."))
    out.append(("Analysis mode", "AI model" if decision["mode"] == "ai" else
                "Fallback rule-based classifier (AI unavailable) — not equivalent to full AI understanding"))
    return out


# =========================================================== submission
def submit_report(conn, actor: dict, text: str, chosen_location_id: str | None = None,
                  pending_submission_id: int | None = None, clarification_text: str | None = None,
                  manual_confidential: bool = False, force_fallback: bool = False) -> dict:
    user = auth.require_role(conn, actor, "student")
    trust_engine.apply_daily_recovery(conn, user["id"])
    user = auth.fresh(conn, user)

    pending = None
    if pending_submission_id:
        pending = db.one(conn, "SELECT * FROM report_submissions WHERE id = ? AND user_id = ? AND outcome = 'pending_clarification'",
                         (pending_submission_id, user["id"]))
        if not pending:
            raise SubmissionError("Pending report not found.")
        extra = (clarification_text or "").strip()
        text = pending["text"] + ((" | Clarification: " + extra) if extra else "")

    decision = analyze_report(conn, text, chosen_location_id, manual_confidential, force_fallback)
    result = {"decision": decision, "explanation": explain(decision), "safety_warning": decision["safety_warning"]}

    if decision["reject_reason"]:
        with db.transaction(conn):
            if pending:
                conn.execute("DELETE FROM report_submissions WHERE id = ?", (pending["id"],))
            conn.execute("""INSERT INTO report_submissions(user_id, text, analysis_json, outcome, created_at)
                            VALUES (?,?,?,?,?)""", (user["id"], decision["text"], db.dumps(decision["analysis"]), "rejected", db.now_iso()))
            if decision["injection"]:
                spam_engine.record_injection_attempt(conn, user["id"])
        result.update(status="rejected", message=decision["reject_reason"])
        return result

    # ---------------- confidential path (never touches issues / public tables)
    if decision["confidential"]:
        loc = decision["location"]
        verified = loc["status"] == "verified"
        if pending:
            conn.execute("DELETE FROM report_submissions WHERE id = ?", (pending["id"],))
        conf = privacy_engine.create_confidential_report(
            conn, user["id"], decision["text"], decision["department"], loc["location_name"] if verified else None,
            decision["category"], decision["base_priority"], decision["is_hazard"], decision["confidentiality_reason"],
            not verified)
        result.update(status="confidential", tracking_id=conf["tracking_id"],
                      message="Your report has been filed confidentially. Your identity is hidden from staff, other students and public boards.",
                      receipt={"Report ID": conf["tracking_id"], "Type": "Confidential", "Handling": conf["routing_reason"],
                               "Priority": decision["base_priority"], "Status": S_SUBMITTED,
                               "Submitted": db.now_iso(), "Tracking": "Private Confidential Reports (your dashboard only)"})
        return result

    if not decision["is_hazard"] and spam_engine.rate_limited(conn, user["id"]):
        result.update(status="rate_limited", message="Too many reports in a short time. Please wait a few minutes. "
                                                     "(Safety hazards are never rate-limited.)")
        return result

    # ---------------- needs clarification
    if decision["needs_clarification"]:
        question = decision["clarification_question"]
        count = (pending["clarification_count"] + 1) if pending else 0
        if pending and count >= 1 and decision["location"]["status"] in ("missing", "unknown"):
            question = "We still could not verify the location. Please choose one of the verified locations from the list."
        sugg = decision["location"].get("suggestions", [])
        with db.transaction(conn):
            if pending:
                conn.execute("""UPDATE report_submissions SET text = ?, analysis_json = ?, clarification_question = ?,
                                clarification_count = ?, suggested_locations = ?, is_hazard = ? WHERE id = ?""",
                             (decision["text"], db.dumps(decision["analysis"]), question, count, db.dumps(sugg),
                              int(decision["is_hazard"]), pending["id"]))
                sub_id = pending["id"]
            else:
                cur = conn.execute("""INSERT INTO report_submissions(user_id, text, analysis_json, outcome, clarification_question,
                                      suggested_locations, is_hazard, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                                   (user["id"], decision["text"], db.dumps(decision["analysis"]), "pending_clarification",
                                    question, db.dumps(sugg), int(decision["is_hazard"]), db.now_iso()))
                sub_id = cur.lastrowid
        result.update(status="pending_clarification", submission_id=sub_id, question=question, suggestions=sugg,
                      message=f"{S_PENDING}: {question}")
        return result

    return _file_public_report(conn, user, decision, pending, result)


def _file_public_report(conn, user, decision, pending, result):
    loc = decision["location"]
    embedding = ai_engine.embed(f"{decision['normalized_problem']} at {loc['location_name']}")
    now = db.now_iso()
    with db.transaction(conn):
        dup, dup_reason = duplicate_engine.find_duplicate(conn, loc["location_id"], decision["category"],
                                                          decision["normalized_problem"], embedding, loc["location_name"])
        if pending:
            conn.execute("DELETE FROM report_submissions WHERE id = ?", (pending["id"],))
        if dup:
            already = db.one(conn, "SELECT id FROM issue_participants WHERE issue_id = ? AND user_id = ?", (dup["id"], user["id"]))
            outcome = "already_reported" if already else "merged"
            conn.execute("""INSERT INTO report_submissions(user_id, issue_id, text, analysis_json, outcome, is_hazard, created_at)
                            VALUES (?,?,?,?,?,?,?)""", (user["id"], dup["id"], decision["text"], db.dumps(decision["analysis"]),
                                                        outcome, int(decision["is_hazard"]), now))
            if already:
                spam_engine.check_repeat_submission(conn, user["id"], dup["id"])
                msg = "You have already reported or joined this issue. The affected student count was not changed."
            else:
                conn.execute("""INSERT INTO issue_participants(issue_id, user_id, via, trust_snapshot, joined_at)
                                VALUES (?,?,?,?,?)""", (dup["id"], user["id"], "report", user["trust_score"], now))
                if decision["is_hazard"] and not dup["is_safety_hazard"]:
                    conn.execute("UPDATE issues SET is_safety_hazard = 1, hazard_type = ?, base_priority = 'Critical', priority_reason = ? WHERE id = ?",
                                 (decision["hazard_type"], decision["priority_reason"], dup["id"]))
                spam_engine.check_join_cluster(conn, dup["id"])
                old, new = issue_ops.recompute_priority(conn, dup["id"])
                issue_ops.add_history_note(conn, dup["id"], "Another affected student reported the same problem (merged).", {"role": "system", "id": None})
                msg = "This problem has already been reported. You have been added to the affected students list."
            issue = issue_ops.get_issue(conn, dup["id"])
            result.update(status=outcome, issue_id=dup["id"], public_id=issue["public_id"], message=msg, merge_reason=dup_reason,
                          receipt=_receipt(issue))
            return result

        previous = duplicate_engine.find_previous_closed(conn, loc["location_id"], decision["category"])
        rec_count = len(previous)
        title = f"{decision['normalized_problem']} — {loc['location_name']}"
        cur = conn.execute(
            """INSERT INTO issues(department_code, location_id, category, normalized_problem, title, base_priority,
                   priority_score, priority, is_safety_hazard, hazard_type, status, routing_reason, location_reason,
                   priority_reason, recurrence_of, recurrence_count, embedding, analysis_mode, created_at, last_update_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decision["department"], loc["location_id"], decision["category"], decision["normalized_problem"], title,
             decision["base_priority"], PRIORITY_BASE_SCORE[decision["base_priority"]], decision["base_priority"],
             int(decision["is_hazard"]), decision["hazard_type"], S_SUBMITTED, decision["routing_reason"], loc["reason"],
             decision["priority_reason"], previous[-1]["id"] if previous else None, rec_count,
             json.dumps(embedding) if embedding else None, decision["mode"], now, now))
        iid = cur.lastrowid
        public_id = f"RF-{db.utcnow().year}-{iid:04d}"
        conn.execute("UPDATE issues SET public_id = ? WHERE id = ?", (public_id, iid))
        conn.execute("INSERT INTO status_history(issue_id, from_status, to_status, note, actor_role, created_at) VALUES (?,?,?,?,?,?)",
                     (iid, None, S_SUBMITTED, "Report submitted.", "student", now))
        for n, prev in enumerate(previous, start=1):
            conn.execute("INSERT INTO recurrence_links(new_issue_id, previous_issue_id, occurrence_no, created_at) VALUES (?,?,?,?)",
                         (iid, prev["id"], n, now))
        conn.execute("INSERT INTO issue_participants(issue_id, user_id, via, trust_snapshot, joined_at) VALUES (?,?,?,?,?)",
                     (iid, user["id"], "report", user["trust_score"], now))
        conn.execute("""INSERT INTO report_submissions(user_id, issue_id, text, analysis_json, outcome, is_hazard, created_at)
                        VALUES (?,?,?,?,?,?,?)""", (user["id"], iid, decision["text"], db.dumps(decision["analysis"]), "created",
                                                    int(decision["is_hazard"]), now))
        issue_ops.recompute_priority(conn, iid)
        issue_ops.set_status(conn, iid, S_ASSIGNED, f"Automatically routed to {decision['department']}. {decision['routing_reason']}")
        notifications.notify(conn, user["id"], f"Your report {public_id} was assigned to {decision['department']}.")
        issue = issue_ops.get_issue(conn, iid)
    msg = "Report submitted and routed."
    if rec_count:
        msg += " Recurring Issue — This problem has previously been resolved but has returned."
    result.update(status="created", issue_id=iid, public_id=public_id, message=msg, recurrence_count=rec_count,
                  merge_reason=dup_reason, receipt=_receipt(issue))
    return result


def _receipt(issue: dict) -> dict:
    return {"Report ID": issue["public_id"], "Department": issue["department_code"], "Location": issue["location_name"],
            "Problem": issue["normalized_problem"], "Priority": issue["priority"], "Status": issue["status"],
            "Affected students": issue["affected_count"], "Submitted": db.now_iso(), "Tracking": "Available in My Reports"}


def discard_pending(conn, actor, submission_id: int) -> None:
    user = auth.fresh(conn, actor)
    conn.execute("DELETE FROM report_submissions WHERE id = ? AND user_id = ? AND outcome = 'pending_clarification'",
                 (submission_id, user["id"]))


def my_pending(conn, actor) -> list[dict]:
    user = auth.fresh(conn, actor)
    out = db.rows(conn, """SELECT id, text, clarification_question, suggested_locations, is_hazard, created_at, clarification_count
                           FROM report_submissions WHERE user_id = ? AND outcome = 'pending_clarification' ORDER BY id DESC""",
                  (user["id"],))
    for o in out:
        o["suggested_locations"] = json.loads(o["suggested_locations"] or "[]")
    return out


# =========================================================== student actions
def join_issue(conn, actor: dict, issue_id: int) -> dict:
    user = auth.require_role(conn, actor, "student")
    trust_engine.apply_daily_recovery(conn, user["id"])
    user = auth.fresh(conn, user)
    issue = issue_ops.get_issue(conn, issue_id)
    if not issue:
        raise ValueError("Issue not found.")
    if issue["status"] not in ACTIVE_STATUSES:
        raise ValueError("This issue is closed; please submit a new report if the problem returned.")
    with db.transaction(conn):
        if db.one(conn, "SELECT id FROM issue_participants WHERE issue_id = ? AND user_id = ?", (issue_id, user["id"])):
            return {"status": "already", "message": "You are already in the affected students list for this issue."}
        conn.execute("INSERT INTO issue_participants(issue_id, user_id, via, trust_snapshot, joined_at) VALUES (?,?,?,?,?)",
                     (issue_id, user["id"], "me_too", user["trust_score"], db.now_iso()))
        flagged = spam_engine.check_join_cluster(conn, issue_id)
        old, new = issue_ops.recompute_priority(conn, issue_id)
    msg = "You were added to the affected students list."
    if issue["status"] == S_RESOLVED:
        msg += " (You joined after the current resolution attempt, so you can vote from the next round.)"
    if flagged:
        msg += " Note: unusual activity on this issue is under review."
    return {"status": "joined", "message": msg, "old_priority": old, "new_priority": new}


def student_issues(conn, actor) -> list[dict]:
    user = auth.fresh(conn, actor)
    return db.rows(conn, """SELECT i.id, i.public_id, i.title, i.department_code, l.name AS location, i.priority, i.status,
                                   p.via, i.last_update_at, i.recurrence_count,
                                   (SELECT COUNT(*) FROM issue_participants x WHERE x.issue_id = i.id) AS affected
                            FROM issues i JOIN issue_participants p ON p.issue_id = i.id JOIN locations l ON l.id = i.location_id
                            WHERE p.user_id = ? ORDER BY i.last_update_at DESC""", (user["id"],))


def public_issues(conn, include_closed: bool = False) -> list[dict]:
    """Public board: confidential reports live in a different table and can never appear here."""
    where = "" if include_closed else f"WHERE i.status IN ({','.join('?' * len(ACTIVE_STATUSES))})"
    params = () if include_closed else ACTIVE_STATUSES
    return db.rows(conn, f"""SELECT i.id, i.public_id, i.title, i.department_code, l.name AS location, i.category,
                                    i.priority, i.priority_score, i.status, i.recurrence_count, i.last_update_at,
                                    (SELECT COUNT(*) FROM issue_participants x WHERE x.issue_id = i.id) AS affected
                             FROM issues i JOIN locations l ON l.id = i.location_id {where}
                             ORDER BY i.priority_score DESC, i.created_at""", params)


def issue_detail(conn, actor: dict, issue_id: int) -> dict:
    issue = issue_ops.get_issue(conn, issue_id)
    if not issue:
        raise ValueError("Issue not found.")
    user = auth.assert_issue_access(conn, actor, issue)
    issue.pop("embedding", None)
    issue["history"] = db.rows(conn, "SELECT from_status, to_status, note, actor_role, created_at FROM status_history "
                                     "WHERE issue_id = ? ORDER BY id", (issue_id,))
    issue["rounds"] = voting_engine.rounds_summary(conn, issue_id)
    issue["recurrence"] = recurrence_info(conn, issue_id)
    issue["sla"] = sla_status(conn, issue)
    issue["credible_count"] = db.one(conn, "SELECT COUNT(*) c FROM issue_participants WHERE issue_id = ? AND suspicious = 0",
                                     (issue_id,))["c"]
    if user["role"] == "student":
        part = db.one(conn, "SELECT via, joined_at FROM issue_participants WHERE issue_id = ? AND user_id = ?", (issue_id, user["id"]))
        issue["my_participation"] = part
    issue["why"] = [
        ("Department", issue["routing_reason"] or ""), ("Location", issue["location_reason"] or ""),
        ("Priority", f"{issue['base_priority']} base: {issue['priority_reason'] or ''}"),
        ("Current priority", issue["escalation_reason"] or ""),
        ("Merged reports", f"{issue['affected_count']} unique affected student(s) grouped into this issue "
                           f"(same verified location + same problem)."),
    ]
    reopens = [r for r in issue["rounds"] if r["outcome"] == "reopened"]
    if reopens:
        issue["why"].append(("Reopened", reopens[-1]["outcome_reason"] or ""))
    return issue


def recurrence_info(conn, issue_id: int) -> dict:
    links = db.rows(conn, """SELECT r.occurrence_no, p.public_id, p.created_at, p.closed_at, p.reopen_count
                             FROM recurrence_links r JOIN issues p ON p.id = r.previous_issue_id
                             WHERE r.new_issue_id = ? ORDER BY r.occurrence_no""", (issue_id,))
    durations = []
    for l in links:
        if l["closed_at"]:
            durations.append((db.parse_ts(l["closed_at"]) - db.parse_ts(l["created_at"])).total_seconds() / 3600)
    return {"previous": links, "count": len(links),
            "avg_resolution_hours": round(sum(durations) / len(durations), 1) if durations else None,
            "previous_disputes": sum(l["reopen_count"] for l in links)}


# =========================================================== staff actions
def department_queue(conn, actor: dict, department: str | None = None) -> list[dict]:
    user = auth.require_role(conn, actor, "staff", "admin")
    if user["role"] == "staff":
        department = user["department_code"]
        if not department:
            raise auth.PermissionDenied("This account has no department queue.")
    rows_ = db.rows(conn, """SELECT i.id, i.public_id, i.title, l.name AS location, i.category, i.priority, i.priority_score,
                                    i.status, i.is_safety_hazard, i.recurrence_count, i.reopen_count, i.created_at,
                                    i.acknowledged_at, i.last_update_at, i.routing_reason, i.priority_reason, i.escalation_reason,
                                    i.department_code, i.needs_admin_review,
                                    (SELECT COUNT(*) FROM issue_participants x WHERE x.issue_id = i.id) AS affected
                             FROM issues i JOIN locations l ON l.id = i.location_id
                             WHERE (? IS NULL OR i.department_code = ?)
                             ORDER BY i.priority_score DESC, i.created_at ASC""", (department, department))
    for r in rows_:
        r["overdue"] = sla_status(conn, r)["overdue"]
    return rows_


def _staff_issue(conn, actor, issue_id):
    issue = issue_ops.get_issue(conn, issue_id)
    if not issue:
        raise ValueError("Issue not found.")
    user = auth.assert_issue_manage(conn, actor, issue)
    return user, issue


def start_work(conn, actor: dict, issue_id: int, note: str = "") -> None:
    user, issue = _staff_issue(conn, actor, issue_id)
    if issue["status"] not in (S_ASSIGNED, S_SUBMITTED, S_REOPENED):
        raise ValueError(f"Cannot start work on an issue in status '{issue['status']}'.")
    with db.transaction(conn):
        conn.execute("UPDATE issues SET acknowledged_at = ? WHERE id = ?", (db.now_iso(), issue_id))
        issue_ops.set_status(conn, issue_id, S_IN_PROGRESS, note.strip() or "Staff started work.", user)
        notifications.notify_participants(conn, issue_id, f"Staff started work on {issue['public_id']}.")


def add_progress(conn, actor: dict, issue_id: int, note: str) -> None:
    user, issue = _staff_issue(conn, actor, issue_id)
    if not note.strip():
        raise ValueError("Progress note cannot be empty.")
    with db.transaction(conn):
        if not issue["acknowledged_at"]:
            conn.execute("UPDATE issues SET acknowledged_at = ? WHERE id = ?", (db.now_iso(), issue_id))
        issue_ops.add_history_note(conn, issue_id, "Progress: " + note.strip(), user)
        notifications.notify_participants(conn, issue_id, f"Update on {issue['public_id']}: {note.strip()[:120]}")


def mark_resolved(conn, actor, issue_id, note):
    return voting_engine.mark_resolved(conn, actor, issue_id, note)


def reject_issue(conn, actor: dict, issue_id: int, reason: str) -> None:
    user, issue = _staff_issue(conn, actor, issue_id)
    if not reason.strip():
        raise ValueError("A rejection reason is required.")
    if issue["is_safety_hazard"] and user["role"] != "admin":
        raise auth.PermissionDenied("Safety-hazard reports cannot be rejected by department staff; escalate to admin.")
    if issue["status"] in (S_CLOSED, S_REJECTED):
        raise ValueError("Issue already finalised.")
    with db.transaction(conn):
        issue_ops.set_status(conn, issue_id, S_REJECTED, reason.strip(), user)
        if user["role"] == "admin":
            db.audit(conn, user, "reject_issue", f"issue:{issue_id}", reason)
        notifications.notify_participants(conn, issue_id, f"{issue['public_id']} was rejected: {reason.strip()[:150]}")
    # No automatic trust penalty: rejection is not proof of a deliberately false report.


# =========================================================== admin actions
def _admin(conn, actor, reason):
    user = auth.require_role(conn, actor, "admin")
    if not (reason or "").strip():
        raise ValueError("A reason is required for admin actions.")
    return user


def reassign(conn, actor, issue_id: int, department: str, reason: str) -> None:
    user = _admin(conn, actor, reason)
    if department not in DEPARTMENTS:
        raise ValueError("Unknown department.")
    issue = issue_ops.get_issue(conn, issue_id)
    with db.transaction(conn):
        conn.execute("UPDATE issues SET department_code = ?, routing_reason = ? WHERE id = ?",
                     (department, f"Reassigned by administration from {issue['department_code']} to {department}: {reason}", issue_id))
        issue_ops.add_history_note(conn, issue_id, f"Reassigned {issue['department_code']} → {department}: {reason}", user)
        db.audit(conn, user, "reassign_issue", f"issue:{issue_id} {issue['department_code']}->{department}", reason)


def confirm_false_report(conn, actor, issue_id: int, user_id: int, reason: str) -> dict | None:
    admin = _admin(conn, actor, reason)
    if not db.one(conn, "SELECT id FROM issue_participants WHERE issue_id = ? AND user_id = ? AND via = 'report'", (issue_id, user_id)):
        raise ValueError("That student did not submit a report on this issue.")
    with db.transaction(conn):
        db.audit(conn, admin, "confirm_false_report", f"issue:{issue_id} user:{user_id}", reason)
        res = trust_engine.penalize_false_report(conn, user_id, issue_id, f"Independent admin finding ({admin['cms_id']}): {reason}")
    return res


def confirm_false_vote(conn, actor, vote_id: int, reason: str) -> dict | None:
    admin = _admin(conn, actor, reason)
    vote = db.one(conn, "SELECT v.*, r.issue_id FROM votes v JOIN resolution_rounds r ON r.id = v.round_id WHERE v.id = ?", (vote_id,))
    if not vote:
        raise ValueError("Vote not found.")
    with db.transaction(conn):
        db.audit(conn, admin, "confirm_false_vote", f"vote:{vote_id}", reason)
        return trust_engine.penalize_false_vote(conn, vote["user_id"], vote_id, vote["issue_id"],
                                                f"Independent admin finding ({admin['cms_id']}): {reason}")


def set_user_flags(conn, actor, user_id: int, reason: str, is_conf_handler=None, active=None) -> None:
    admin = _admin(conn, actor, reason)
    target = db.one(conn, "SELECT id, role FROM users WHERE id = ?", (user_id,))
    if not target:
        raise ValueError("User not found.")
    with db.transaction(conn):
        if is_conf_handler is not None:
            if target["role"] != "staff":
                raise ValueError("Only staff can be confidential handlers.")
            conn.execute("UPDATE users SET is_conf_handler = ? WHERE id = ?", (int(is_conf_handler), user_id))
        if active is not None:
            if user_id == admin["id"]:
                raise ValueError("You cannot deactivate yourself.")
            conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))
        db.audit(conn, admin, "update_user", f"user:{user_id} conf_handler={is_conf_handler} active={active}", reason)


def list_users(conn, actor) -> list[dict]:
    auth.require_role(conn, actor, "admin")
    return db.rows(conn, """SELECT id, cms_id, role, department_code, is_conf_handler, is_conf_officer, trust_score, active, created_at
                            FROM users ORDER BY role, cms_id""")


def issue_participants_admin(conn, actor, issue_id: int) -> list[dict]:
    """Public (non-confidential) issue participants for trust review."""
    auth.require_role(conn, actor, "admin")
    return db.rows(conn, """SELECT p.user_id, u.cms_id, p.via, p.trust_snapshot, p.suspicious, p.joined_at
                            FROM issue_participants p JOIN users u ON u.id = p.user_id WHERE p.issue_id = ?""", (issue_id,))


def votes_admin(conn, actor, issue_id: int) -> list[dict]:
    auth.require_role(conn, actor, "admin")
    return db.rows(conn, """SELECT v.id, r.round_no, u.cms_id, v.vote, v.reason, v.weight, v.suspicious, v.created_at
                            FROM votes v JOIN resolution_rounds r ON r.id = v.round_id JOIN users u ON u.id = v.user_id
                            WHERE r.issue_id = ? ORDER BY v.id""", (issue_id,))


def audit_log(conn, actor, limit: int = 200) -> list[dict]:
    auth.require_role(conn, actor, "admin")
    return db.rows(conn, "SELECT created_at, actor_role, actor_id, action, target, reason FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))


def pending_clarifications_admin(conn, actor) -> list[dict]:
    auth.require_role(conn, actor, "admin")
    return db.rows(conn, """SELECT id, text, clarification_question, is_hazard, created_at FROM report_submissions
                            WHERE outcome = 'pending_clarification' ORDER BY is_hazard DESC, id DESC""")


def export_issues_csv(conn, actor) -> str:
    """Export of PUBLIC issues only — no reporter identities, no confidential data."""
    auth.require_role(conn, actor, "admin")
    data = public_issues(conn, include_closed=True)
    buf = io.StringIO()
    if data:
        w = csv.DictWriter(buf, fieldnames=list(data[0].keys()))
        w.writeheader()
        w.writerows(data)
    return buf.getvalue()


# =========================================================== SLA / analytics
def sla_status(conn, issue: dict) -> dict:
    target = config.SLA_MINUTES.get(issue["priority"], 1440)
    now = db.utcnow()
    created = db.parse_ts(issue["created_at"])
    ref = created
    if issue.get("status") == S_REOPENED:
        last = db.one(conn, "SELECT created_at FROM status_history WHERE issue_id = ? AND to_status = ? ORDER BY id DESC LIMIT 1",
                      (issue["id"], S_REOPENED))
        if last:
            ref = db.parse_ts(last["created_at"])
    ack = db.parse_ts(issue.get("acknowledged_at"))
    active = issue.get("status") in (S_SUBMITTED, S_ASSIGNED, S_REOPENED, S_IN_PROGRESS)
    overdue = bool(active and not ack and (now - ref) > timedelta(minutes=target))
    last_upd = db.parse_ts(issue.get("last_update_at"))
    stale = bool(issue.get("status") == S_IN_PROGRESS and last_upd and (now - last_upd) > timedelta(minutes=target * 3))
    return {"target_minutes": target, "minutes_since_submission": int((now - created).total_seconds() // 60),
            "minutes_since_ack": int((now - ack).total_seconds() // 60) if ack else None,
            "minutes_since_update": int((now - last_upd).total_seconds() // 60) if last_upd else None,
            "overdue": overdue or stale,
            "label": "Response overdue — Administrative review recommended." if (overdue or stale) else "Within target"}


def department_performance(conn) -> list[dict]:
    out = []
    for code in DEPARTMENTS:
        issues = db.rows(conn, "SELECT * FROM issues WHERE department_code = ?", (code,))
        total = len(issues)
        attempted = [i for i in issues if db.one(conn, "SELECT id FROM resolution_rounds WHERE issue_id = ?", (i["id"],))]
        verified = [i for i in issues if i["status"] == S_CLOSED]
        reopened = [i for i in issues if i["reopen_count"] > 0]
        acks = [(db.parse_ts(i["acknowledged_at"]) - db.parse_ts(i["created_at"])).total_seconds() / 3600
                for i in issues if i["acknowledged_at"]]
        res = [(db.parse_ts(i["closed_at"]) - db.parse_ts(i["created_at"])).total_seconds() / 3600 for i in verified if i["closed_at"]]
        overdue = sum(1 for i in issues if sla_status(conn, i)["overdue"])
        recurring = sum(1 for i in issues if i["recurrence_count"] > 0)
        v_rate = len(verified) / len(attempted) if attempted else None
        r_rate = len(reopened) / len(attempted) if attempted else None
        active = sum(1 for i in issues if i["status"] in ACTIVE_STATUSES)
        score = None
        if total:
            score = round(100 * (0.5 * (v_rate if v_rate is not None else 0.5) + 0.2 * (1 - (r_rate or 0))
                                 + 0.15 * (1 - (overdue / active if active else 0))
                                 + 0.15 * (1 - min(1.0, (sum(acks) / len(acks) / 24) if acks else 0.5))))
        out.append({"Department": code, "Issues": total, "Active": active, "Verified fixes": len(verified),
                    "Verified resolution rate": f"{v_rate:.0%}" if v_rate is not None else "—",
                    "Avg response (h)": round(sum(acks) / len(acks), 1) if acks else "—",
                    "Avg resolution (h)": round(sum(res) / len(res), 1) if res else "—",
                    "Reopened rate": f"{r_rate:.0%}" if r_rate is not None else "—",
                    "Overdue": overdue, "Recurring rate": f"{recurring / total:.0%}" if total else "—",
                    "Performance score": score if score is not None else "—"})
    return out


def admin_stats(conn, actor) -> dict:
    auth.require_role(conn, actor, "admin")
    q = lambda sql, p=(): db.one(conn, sql, p)["c"]
    ph = ",".join("?" * len(ACTIVE_STATUSES))
    closed = db.rows(conn, "SELECT created_at, closed_at FROM issues WHERE status = ? AND closed_at IS NOT NULL", (S_CLOSED,))
    avg_res = (sum((db.parse_ts(c["closed_at"]) - db.parse_ts(c["created_at"])).total_seconds() for c in closed) / len(closed) / 3600
               if closed else None)
    workload = db.rows(conn, f"SELECT department_code AS department, COUNT(*) AS active FROM issues WHERE status IN ({ph}) "
                             "GROUP BY department_code", ACTIVE_STATUSES)
    all_active = db.rows(conn, f"SELECT * FROM issues WHERE status IN ({ph})", ACTIVE_STATUSES)
    return {
        "total_public": q("SELECT COUNT(*) c FROM issues"),
        "total_submissions": q("SELECT COUNT(*) c FROM report_submissions"),
        "active": q(f"SELECT COUNT(*) c FROM issues WHERE status IN ({ph})", ACTIVE_STATUSES),
        "critical": q(f"SELECT COUNT(*) c FROM issues WHERE priority = 'Critical' AND status IN ({ph})", ACTIVE_STATUSES),
        "verified": q("SELECT COUNT(*) c FROM issues WHERE status = ?", (S_CLOSED,)),
        "reopened": q("SELECT COUNT(*) c FROM issues WHERE reopen_count > 0"),
        "awaiting_clarification": q("SELECT COUNT(*) c FROM report_submissions WHERE outcome = 'pending_clarification'"),
        "suspicious_open": q("SELECT COUNT(*) c FROM suspicious_activity WHERE status = 'open'"),
        "needs_review": q("SELECT COUNT(*) c FROM issues WHERE needs_admin_review = 1"),
        "avg_resolution_hours": round(avg_res, 1) if avg_res is not None else None,
        "workload": workload,
        "overdue": [dict(public_id=i["public_id"], title=i["title"], priority=i["priority"], department=i["department_code"])
                    for i in all_active if sla_status(conn, i)["overdue"]],
        "recurring": db.rows(conn, "SELECT public_id, title, recurrence_count, status FROM issues WHERE recurrence_count > 0 ORDER BY recurrence_count DESC"),
        "confidential": privacy_engine.aggregate_stats(conn),
    }


def trending(conn) -> dict:
    """Public trending board (confidential reports are structurally excluded)."""
    since = (db.utcnow() - timedelta(hours=48)).isoformat(timespec="seconds")
    items = []
    for i in public_issues(conn, include_closed=False):
        parts = db.rows(conn, "SELECT trust_snapshot, suspicious, joined_at FROM issue_participants WHERE issue_id = ?", (i["id"],))
        credible = [p for p in parts if not p["suspicious"]]
        weight = sum(priority_engine.trust_weight(p["trust_snapshot"]) for p in credible)
        recent = sum(1 for p in credible if p["joined_at"] >= since)
        status_factor = 1.0 if i["status"] != S_RESOLVED else 0.6
        score = (weight * 10 + recent * 5 + i["recurrence_count"] * 8 + i["priority_score"] * 0.3) * status_factor
        items.append({**i, "credible_students": len(credible), "recent_reports_48h": recent, "trend_score": round(score, 1)})
    return {
        "most_reported": sorted(items, key=lambda x: (-x["credible_students"], -x["trend_score"]))[:5],
        "recently_increasing": sorted([x for x in items if x["recent_reports_48h"] > 0], key=lambda x: -x["recent_reports_48h"])[:5],
        "recurring": sorted([x for x in items if x["recurrence_count"] > 0], key=lambda x: -x["recurrence_count"])[:5],
        "important_unresolved": sorted(items, key=lambda x: -x["trend_score"])[:5],
    }


def briefing_facts(conn) -> dict:
    ph = ",".join("?" * len(ACTIVE_STATUSES))
    active = db.rows(conn, f"""SELECT i.*, l.name AS location_name FROM issues i JOIN locations l ON l.id = i.location_id
                               WHERE i.status IN ({ph}) ORDER BY i.priority_score DESC""", ACTIVE_STATUSES)
    since = (db.utcnow() - timedelta(days=7)).isoformat(timespec="seconds")
    return {
        "active_issues": len(active),
        "critical_open": [f"{i['normalized_problem']} at {i['location_name']} ({i['public_id']})" for i in active if i["priority"] == "Critical"],
        "department_open": [{"department": d, "open": sum(1 for i in active if i["department_code"] == d)} for d in DEPARTMENTS],
        "recurring": [f"{i['normalized_problem']} at {i['location_name']} (recurred {i['recurrence_count']} time(s))"
                      for i in active if i["recurrence_count"] > 0],
        "reopened_count": sum(1 for i in active if i["status"] == S_REOPENED),
        "verified_recent": db.one(conn, "SELECT COUNT(*) c FROM issues WHERE status = ? AND closed_at >= ?", (S_CLOSED, since))["c"],
        "overdue_count": sum(1 for i in active if sla_status(conn, i)["overdue"]),
        "top_priorities": [f"{i['normalized_problem']} at {i['location_name']} ({i['priority']})" for i in active[:3]],
    }


def admin_briefing(conn, actor) -> tuple[str, str]:
    auth.require_role(conn, actor, "admin")
    return ai_engine.generate_briefing(briefing_facts(conn), conn)

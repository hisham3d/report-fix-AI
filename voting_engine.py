"""Community verification: trust-weighted voting on each resolution attempt."""
from __future__ import annotations

import json
import math

import auth
import database as db
import issue_ops
import notifications
import spam_engine
import trust_engine
from constants import S_ASSIGNED, S_IN_PROGRESS, S_REOPENED, S_RESOLVED
from priority_engine import trust_weight

REOPEN_MIN_DISPUTERS = 2
REOPEN_MIN_WEIGHT = 1.5
REOPEN_MIN_SHARE = 0.40
CONFIRM_MIN_SHARE_OF_CAST = 0.60
CONFIRM_MIN_SHARE_OF_ELIGIBLE = 0.50


class VoteError(Exception):
    pass


def current_round(conn, issue_id: int) -> dict | None:
    return db.one(conn, "SELECT * FROM resolution_rounds WHERE issue_id = ? ORDER BY round_no DESC LIMIT 1", (issue_id,))


def mark_resolved(conn, actor: dict, issue_id: int, note: str) -> dict:
    issue = issue_ops.get_issue(conn, issue_id)
    if not issue:
        raise ValueError("Issue not found.")
    user = auth.assert_issue_manage(conn, actor, issue)
    if user["role"] != "staff":
        raise auth.PermissionDenied("Only department staff can submit a resolution.")
    if not (note or "").strip() or len(note.strip()) < 5:
        raise ValueError("A resolution note (at least 5 characters) is required.")
    if issue["status"] not in (S_ASSIGNED, S_IN_PROGRESS, S_REOPENED):
        raise ValueError(f"Cannot resolve an issue in status '{issue['status']}'.")
    prev = current_round(conn, issue_id)
    round_no = (prev["round_no"] + 1) if prev else 1
    eligible = [p["user_id"] for p in db.rows(conn, "SELECT user_id FROM issue_participants WHERE issue_id = ?", (issue_id,))]
    now = db.now_iso()
    with db.transaction(conn):
        conn.execute("""INSERT INTO resolution_rounds(issue_id, round_no, resolution_note, resolved_by, eligible_user_ids, opened_at)
                        VALUES (?,?,?,?,?,?)""", (issue_id, round_no, note.strip(), user["id"], json.dumps(eligible), now))
        conn.execute("UPDATE issues SET resolved_at = ? WHERE id = ?", (now, issue_id))
        if not issue["acknowledged_at"]:
            conn.execute("UPDATE issues SET acknowledged_at = ? WHERE id = ?", (now, issue_id))
        issue_ops.set_status(conn, issue_id, S_RESOLVED, f"Resolution attempt #{round_no}: {note.strip()}", user)
        notifications.notify_participants(conn, issue_id,
                                          f"Issue {issue['public_id']} was marked resolved. Please verify: Confirm Fix or Still Not Fixed.")
    return {"round_no": round_no, "eligible": len(eligible)}


def cast_vote(conn, actor: dict, issue_id: int, vote: str, reason: str = "") -> dict:
    user = auth.fresh(conn, actor)
    if user["role"] != "student":
        raise VoteError("Only affected students can vote.")
    if vote not in ("confirm", "dispute"):
        raise VoteError("Invalid vote.")
    issue = db.one(conn, "SELECT id, status, public_id FROM issues WHERE id = ?", (issue_id,))
    if not issue or issue["status"] != S_RESOLVED:
        raise VoteError("This issue is not awaiting verification.")
    rnd = current_round(conn, issue_id)
    if not rnd or rnd["closed_at"]:
        raise VoteError("No open verification round.")
    if user["id"] not in json.loads(rnd["eligible_user_ids"]):
        raise VoteError("Vote rejected: only students who reported or joined this issue before the resolution may vote.")
    if vote == "dispute" and len((reason or "").strip()) < 5:
        raise VoteError("Please give a short reason (at least 5 characters) when disputing a resolution.")
    trust_engine.apply_daily_recovery(conn, user["id"])
    score = db.one(conn, "SELECT trust_score FROM users WHERE id = ?", (user["id"],))["trust_score"]
    # A participant already held for review (e.g. new-account cluster) casts a held vote.
    held = db.one(conn, "SELECT suspicious FROM issue_participants WHERE issue_id = ? AND user_id = ?", (issue_id, user["id"]))
    try:
        with db.transaction(conn):
            cur = conn.execute("""INSERT INTO votes(round_id, user_id, vote, reason, trust_snapshot, weight, suspicious, created_at)
                                  VALUES (?,?,?,?,?,?,?,?)""",
                               (rnd["id"], user["id"], vote, (reason or "").strip()[:500], score, trust_weight(score),
                                int(bool(held and held["suspicious"])), db.now_iso()))
            vote_id = cur.lastrowid
            flagged = spam_engine.check_vote_cluster(conn, rnd["id"], issue_id)
    except Exception as e:
        if "UNIQUE" in str(e):
            raise VoteError("Vote rejected: you have already voted in this resolution round.")
        raise
    outcome = evaluate_round(conn, issue_id)
    return {"vote_id": vote_id, "weight": trust_weight(score), "outcome": outcome, "flagged": bool(flagged)}


def round_tally(conn, rnd: dict) -> dict:
    eligible = json.loads(rnd["eligible_user_ids"])
    votes = db.rows(conn, "SELECT * FROM votes WHERE round_id = ?", (rnd["id"],))
    counted = [v for v in votes if not v["suspicious"]]
    conf = [v for v in counted if v["vote"] == "confirm"]
    disp = [v for v in counted if v["vote"] == "dispute"]
    elig_weight = 0.0
    if eligible:
        ph = ",".join("?" * len(eligible))
        held = {r["user_id"] for r in db.rows(conn, f"SELECT user_id FROM issue_participants WHERE issue_id = ? AND suspicious = 1 AND user_id IN ({ph})", (rnd["issue_id"], *eligible))}
        for u in db.rows(conn, f"SELECT id, trust_score FROM users WHERE id IN ({ph})", tuple(eligible)):
            if u["id"] not in held:
                elig_weight += trust_weight(u["trust_score"])
    return {
        "eligible_count": len(eligible), "eligible_weight": round(elig_weight, 2), "votes_cast": len(votes),
        "held_for_review": len(votes) - len(counted),
        "confirm_count": len(conf), "confirm_weight": round(sum(v["weight"] for v in conf), 2),
        "dispute_count": len(disp), "dispute_weight": round(sum(v["weight"] for v in disp), 2),
        "dispute_reasons": [v["reason"] for v in disp if v["reason"]],
    }


def evaluate_round(conn, issue_id: int) -> str:
    """Apply reopening / confirmation thresholds. Silence never counts as confirmation."""
    rnd = current_round(conn, issue_id)
    if not rnd or rnd["closed_at"]:
        return "no_open_round"
    t = round_tally(conn, rnd)
    cast_w = t["confirm_weight"] + t["dispute_weight"]
    n = t["eligible_count"]
    outcome, reason = "awaiting", ""
    if n <= 1 and t["dispute_count"] >= 1:
        outcome = "reopened"
        reason = "The only affected student disputed the resolution, so the issue was returned for further review."
    elif (t["dispute_count"] >= REOPEN_MIN_DISPUTERS and t["dispute_weight"] >= REOPEN_MIN_WEIGHT
          and cast_w > 0 and t["dispute_weight"] / cast_w >= REOPEN_MIN_SHARE):
        outcome = "reopened"
        reason = (f"{t['dispute_count']} affected students disputed the fix (dispute weight {t['dispute_weight']}, "
                  f"{t['dispute_weight'] / cast_w:.0%} of cast weight).")
    else:
        need_confirmers = min(2, n) if n else 1
        if (t["confirm_count"] >= need_confirmers and cast_w > 0
                and t["confirm_weight"] / cast_w >= CONFIRM_MIN_SHARE_OF_CAST
                and t["confirm_weight"] >= CONFIRM_MIN_SHARE_OF_ELIGIBLE * max(t["eligible_weight"], 0.25)):
            outcome = "verified"
            reason = (f"{t['confirm_count']} affected students confirmed the fix (confirm weight {t['confirm_weight']} "
                      f"of eligible weight {t['eligible_weight']}).")
    with db.transaction(conn):
        if outcome == "reopened":
            reasons = "; ".join(t["dispute_reasons"][:5])
            conn.execute("UPDATE resolution_rounds SET closed_at = ?, outcome = ?, outcome_reason = ? WHERE id = ?",
                         (db.now_iso(), "reopened", reason, rnd["id"]))
            conn.execute("UPDATE issues SET reopen_count = reopen_count + 1, resolved_at = NULL, acknowledged_at = NULL WHERE id = ?", (issue_id,))
            issue_ops.set_status(conn, issue_id, S_REOPENED, f"Automatically reopened: {reason} Student feedback: {reasons}")
            notifications.notify_participants(conn, issue_id, f"Issue #{issue_id} was reopened after students disputed the resolution.")
        elif outcome == "verified":
            conn.execute("UPDATE resolution_rounds SET closed_at = ?, outcome = ?, outcome_reason = ? WHERE id = ?",
                         (db.now_iso(), "verified", reason, rnd["id"]))
            issue_ops.close_verified(conn, issue_id, f"Community verified: {reason}", "Community verification vote")
        elif t["votes_cast"] >= n and n > 0:
            conn.execute("UPDATE issues SET needs_admin_review = 1, admin_review_reason = ? WHERE id = ?",
                         ("All eligible students voted but neither threshold was met (or votes are held for review).", issue_id))
    return outcome


def admin_independent_verification(conn, admin: dict, issue_id: int, fixed: bool, reason: str) -> str:
    """Independent verification by admin; rewards votes that match the verified outcome."""
    auth.require_role(conn, admin, "admin")
    if not reason.strip():
        raise ValueError("A reason is required.")
    issue = db.one(conn, "SELECT id, status FROM issues WHERE id = ?", (issue_id,))
    if not issue or issue["status"] != S_RESOLVED:
        raise ValueError("Independent verification applies to issues awaiting verification.")
    rnd = current_round(conn, issue_id)
    outcome = "verified" if fixed else "reopened"
    with db.transaction(conn):
        conn.execute("UPDATE resolution_rounds SET closed_at = ?, outcome = ?, outcome_reason = ? WHERE id = ?",
                     (db.now_iso(), outcome, f"Independent admin verification: {reason}", rnd["id"]))
        conn.execute("UPDATE issues SET needs_admin_review = 0, admin_review_reason = NULL WHERE id = ?", (issue_id,))
        db.audit(conn, admin, f"independent_verification:{outcome}", f"issue:{issue_id}", reason)
        if fixed:
            issue_ops.close_verified(conn, issue_id, f"Independently verified by administration: {reason}",
                                     f"Independent admin verification ({admin['cms_id']})", admin)
        else:
            conn.execute("UPDATE issues SET reopen_count = reopen_count + 1, resolved_at = NULL, acknowledged_at = NULL WHERE id = ?", (issue_id,))
            issue_ops.set_status(conn, issue_id, S_REOPENED, f"Reopened after independent verification: {reason}", admin)
            notifications.notify_participants(conn, issue_id, f"Issue #{issue_id} was reopened after independent verification.")
        accurate = "confirm" if fixed else "dispute"
        for v in db.rows(conn, "SELECT id, user_id FROM votes WHERE round_id = ? AND vote = ? AND suspicious = 0",
                         (rnd["id"], accurate)):
            trust_engine.reward_accurate_vote(conn, v["user_id"], v["id"], issue_id,
                                              f"Independent admin verification ({admin['cms_id']})")
    return outcome


def pending_votes_for(conn, user_id: int) -> list[dict]:
    out = []
    for issue in db.rows(conn, """SELECT i.id, i.public_id, i.title, i.status FROM issues i
                                  JOIN issue_participants p ON p.issue_id = i.id
                                  WHERE p.user_id = ? AND i.status = ?""", (user_id, S_RESOLVED)):
        rnd = current_round(conn, issue["id"])
        if not rnd or user_id not in json.loads(rnd["eligible_user_ids"]):
            continue
        voted = db.one(conn, "SELECT vote FROM votes WHERE round_id = ? AND user_id = ?", (rnd["id"], user_id))
        issue.update(round_no=rnd["round_no"], resolution_note=rnd["resolution_note"], already_voted=voted["vote"] if voted else None)
        out.append(issue)
    return out


def rounds_summary(conn, issue_id: int) -> list[dict]:
    out = []
    for rnd in db.rows(conn, "SELECT * FROM resolution_rounds WHERE issue_id = ? ORDER BY round_no", (issue_id,)):
        t = round_tally(conn, rnd)
        out.append({"round_no": rnd["round_no"], "resolution_note": rnd["resolution_note"], "opened_at": rnd["opened_at"],
                    "closed_at": rnd["closed_at"], "outcome": rnd["outcome"] or "awaiting", "outcome_reason": rnd["outcome_reason"],
                    **{k: v for k, v in t.items()}})
    return out


def required_confirmations(n: int) -> int:
    return max(min(2, n), math.ceil(n * CONFIRM_MIN_SHARE_OF_ELIGIBLE)) if n else 1

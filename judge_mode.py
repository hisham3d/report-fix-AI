"""Judge Mode: account switching, demo data, resets. Only active when JUDGE_MODE=true in .env."""
from __future__ import annotations

import secrets
from datetime import timedelta

import auth
import complaint_service as svc
import config
import database as db
import location_engine
import privacy_engine
import trust_engine
import voting_engine

STUDENT_PW = "Student@123"
STAFF_PW = "Staff@123"
CONF_PW = "Conf@123"
ADMIN_PW = "Admin@123"

DEMO_STUDENTS = [("300001", 100), ("300002", 100), ("300003", 85), ("300004", 70), ("300005", 55), ("300006", 30)]
STAFF = [("staff.ee", "EE"), ("staff.cse", "CSE"), ("staff.is", "IS"), ("staff.library", "Library"),
         ("staff.cafeteria", "Cafeteria"), ("staff.labs", "Labs")]
CONF_HANDLERS = [("conf.cse", "CSE"), ("conf.ee", "EE"), ("conf.is", "IS")]


class JudgeModeDisabled(Exception):
    pass


def require_enabled(enabled: bool | None = None):
    if not (config.JUDGE_MODE if enabled is None else enabled):
        raise JudgeModeDisabled("Judge Mode is disabled. Set JUDGE_MODE=true in the local .env file.")


def seed_accounts(conn) -> None:
    old = (db.utcnow() - timedelta(days=60)).isoformat(timespec="seconds")
    with db.transaction(conn):
        if not db.one(conn, "SELECT id FROM users WHERE cms_id = 'admin'"):
            auth._create_user(conn, "admin", ADMIN_PW, "admin", display_name="Campus Admin", created_at=old)
        for cms, dept in STAFF:
            if not db.one(conn, "SELECT id FROM users WHERE cms_id = ?", (cms,)):
                auth._create_user(conn, cms, STAFF_PW, "staff", dept, f"{dept} Staff", created_at=old)
        for cms, dept in CONF_HANDLERS:
            if not db.one(conn, "SELECT id FROM users WHERE cms_id = ?", (cms,)):
                auth._create_user(conn, cms, CONF_PW, "staff", dept, f"{dept} Confidential Handler", is_conf_handler=1, created_at=old)
        if not db.one(conn, "SELECT id FROM users WHERE cms_id = 'conf.officer'"):
            auth._create_user(conn, "conf.officer", CONF_PW, "staff", None, "Independent Confidential Review Officer",
                              is_conf_officer=1, created_at=old)
        for cms, _ in DEMO_STUDENTS:
            if not db.one(conn, "SELECT id FROM users WHERE cms_id = ?", (cms,)):
                auth._create_user(conn, cms, STUDENT_PW, "student", created_at=old)


def ensure_initialized(conn) -> None:
    db.init_db(conn)
    if not db.one(conn, "SELECT id FROM locations LIMIT 1"):
        location_engine.seed_locations(conn)
    if not db.one(conn, "SELECT id FROM users LIMIT 1"):
        seed_accounts(conn)
        load_demo_data(conn, _internal=True)


def _uid(conn, cms):
    return auth.load_user(conn, db.one(conn, "SELECT id FROM users WHERE cms_id = ?", (cms,))["id"])


def _reset_students(conn):
    today = config.local_today(db.utcnow()).isoformat()
    conn.execute("UPDATE users SET trust_score = 100, last_recovery_date = ? WHERE role = 'student'", (today,))


def reset_empty(conn, actor_label="judge") -> None:
    db.clear_complaint_data(conn)
    seed_accounts(conn)
    with db.transaction(conn):
        _reset_students(conn)
        db.audit(conn, None, "judge_reset_empty", actor_label, "Judge Mode reset to empty complaint database")


def load_demo_data(conn, _internal: bool = False) -> None:
    """Build the clearly-fictional demo dataset through the real service functions (fallback mode for determinism)."""
    db.clear_complaint_data(conn)
    seed_accounts(conn)
    with db.transaction(conn):
        _reset_students(conn)
    S = {cms: _uid(conn, cms) for cms, _ in DEMO_STUDENTS}
    st = {cms: _uid(conn, cms) for cms, _ in STAFF}
    admin = _uid(conn, "admin")
    sub = lambda u, t, **k: svc.submit_report(conn, u, t, force_fallback=True, **k)

    def at(**delta):
        db.set_clock_offset(-timedelta(**delta))

    try:
        # Starting scores (fictional) with narrated trust history
        at(days=30)
        for cms, score in DEMO_STUDENTS:
            if score < 100:
                u = S[cms]
                if cms == "300003":
                    trust_engine.apply_change(conn, u["id"], -10, "Confirmed spam/abuse (demo review flag)", "demo:abuse:300003", "Admin review (demo)")
                    trust_engine.apply_change(conn, u["id"], -10, "Proven deliberately false vote (demo)", "demo:falsevote:300003", "Independent admin verification (demo)")
                    trust_engine.apply_change(conn, u["id"], 5, "Daily recovery (1 day × +5)", "demo:recovery:300003", "System daily recovery")
                else:
                    trust_engine.apply_change(conn, u["id"], score - 100, "Demonstration starting score (fictional history)",
                                              f"demo:start:{cms}", "Demo data")
        S = {cms: _uid(conn, cms) for cms, _ in DEMO_STUDENTS}

        # 1) Recurring: Lab 4 AC fixed & verified 20 days ago, returns yesterday
        at(days=20)
        r = sub(S["300001"], "Lab 4 ka AC kharab hai.")
        lab4 = r["issue_id"]
        svc.join_issue(conn, S["300002"], lab4)
        at(days=19)
        svc.start_work(conn, st["staff.labs"], lab4, "Technician assigned.")
        at(days=18)
        voting_engine.mark_resolved(conn, st["staff.labs"], lab4, "Compressor capacitor replaced and gas refilled.")
        voting_engine.cast_vote(conn, S["300001"], lab4, "confirm")
        voting_engine.cast_vote(conn, S["300002"], lab4, "confirm")
        at(days=1)
        sub(S["300003"], "Lab 4 AC not cooling again, same as last time.")

        # 2) Reopened: Library AC disputed
        at(days=5)
        lib_ac = sub(S["300001"], "Library ka ac kam nhi kr rha.")["issue_id"]
        sub(S["300002"], "Library ki AC thandak nahi de rahi.")
        svc.join_issue(conn, S["300003"], lib_ac)
        at(days=4)
        svc.start_work(conn, st["staff.library"], lib_ac, "Checked unit.")
        voting_engine.mark_resolved(conn, st["staff.library"], lib_ac, "Filter cleaned.")
        at(days=3, hours=20)
        voting_engine.cast_vote(conn, S["300001"], lib_ac, "dispute", "Still blowing warm air in the afternoon.")
        voting_engine.cast_vote(conn, S["300002"], lib_ac, "dispute", "No cooling at all near the reading tables.")

        # 3) Awaiting verification: CSE projector
        at(days=3)
        proj = sub(S["300002"], "CSE lab ka projector dead hai.")["issue_id"]
        sub(S["300004"], "Projector in the CSE computer lab is not turning on.")
        at(days=2)
        svc.start_work(conn, st["staff.cse"], proj, "Checking lamp.")
        voting_engine.mark_resolved(conn, st["staff.cse"], proj, "Projector lamp replaced and HDMI cable changed.")

        # 4) In progress: Library internet
        at(days=1)
        net = sub(S["300004"], "Library mein internet nahi chal raha.")["issue_id"]
        at(hours=20)
        svc.start_work(conn, st["staff.library"], net, "Router being inspected.")
        svc.add_progress(conn, st["staff.library"], net, "Replacement router requested from IT store.")

        # 5) High: food hygiene
        at(hours=6)
        sub(S["300003"], "Cafeteria mein khane mein cockroach mila hai.")

        # 6) Semantic duplicate: Lab 3 AC x3
        at(hours=2)
        sub(S["300001"], "Lab 3 ka AC kharab hai.")
        sub(S["300002"], "Air conditioner in laboratory number 3 isn't working.")
        sub(S["300003"], "Lab three mein cooling nahi ho rahi.")

        # 7) Low priority with shouting + a repeated submission
        at(hours=1)
        sub(S["300006"], "URGENT!!! THE CAFETERIA CHAIR IS BROKEN!!!")
        sub(S["300006"], "URGENT!!! THE CAFETERIA CHAIR IS BROKEN!!!")
        sub(S["300003"], "Cafeteria ka khana thanda hai.")

        # 8) Critical safety (left unacknowledged -> SLA overdue demo)
        at(minutes=40)
        sub(S["300005"], "EE block mein wire se sparks nikal rahe hain.")

        # 9) Confidential reports
        at(minutes=30)
        sub(S["300004"], "Someone is continuously harassing me in the CSE department.")
        sub(S["300005"], "Senior students meri ragging kr rhy hain.")

        # 10) Pending clarification (unverified location)
        at(minutes=10)
        sub(S["300006"], "Lab 99 ka projector kharab hai.")
    finally:
        db.set_clock_offset(timedelta(0))
    with db.transaction(conn):
        today = config.local_today(db.utcnow()).isoformat()
        conn.execute("UPDATE users SET last_recovery_date = ? WHERE role = 'student'", (today,))
        db.audit(conn, None, "judge_load_demo", "demo", "Loaded fictional demonstration data")


def create_judge_student(conn) -> dict:
    cms = "JUDGE-" + secrets.token_hex(2).upper()
    while db.one(conn, "SELECT id FROM users WHERE cms_id = ?", (cms,)):
        cms = "JUDGE-" + secrets.token_hex(2).upper()
    pw = secrets.token_urlsafe(8)
    uid = auth.register_student(conn, cms, pw, pw)
    return {"user": auth.load_user(conn, uid), "password": pw}


def all_users(conn) -> list[dict]:
    return db.rows(conn, """SELECT id, cms_id, role, department_code, is_conf_handler, is_conf_officer, trust_score
                            FROM users WHERE active = 1 ORDER BY CASE role WHEN 'student' THEN 0 WHEN 'staff' THEN 1 ELSE 2 END, cms_id""")

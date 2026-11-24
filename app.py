"""ReportFix AI — Streamlit entry point.

Run:  python -m streamlit run app.py
"""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

import ai_engine
import auth
import complaint_service as svc
import config
import database as db
import duplicate_engine
import judge_mode
from dashboards import admin as admin_dash
from dashboards import department as dept_dash
from dashboards import student as student_dash
from dashboards.common import header, safe_call, show_decision

# Application logs never include complaint text (see ai_engine / privacy_engine).
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

st.set_page_config(page_title="ReportFix AI", page_icon="🛡️", layout="wide")


@st.cache_resource
def _initialize_database() -> bool:
    c = db.connect()
    judge_mode.ensure_initialized(c)
    c.close()
    return True


_initialize_database()
conn = db.connect()  # one lightweight SQLite connection per script run / session


def current_user():
    uid = st.session_state.get("user_id")
    if not uid:
        return None
    try:
        return auth.load_user(conn, uid)
    except auth.PermissionDenied:
        st.session_state.pop("user_id", None)
        return None


def sign_in(user: dict) -> None:
    for k in list(st.session_state.keys()):
        if k != "view":
            del st.session_state[k]
    st.session_state["user_id"] = user["id"]


# ------------------------------------------------------------------ sidebar
def sidebar(user):
    with st.sidebar:
        st.markdown("## 🛡️ ReportFix AI")
        st.caption(config.CAMPUS_NAME)
        ai_on = ai_engine.ai_enabled(conn)
        status = ai_engine.last_status()
        if ai_on and status.get("mode") != "fallback":
            st.success(f"AI mode: {config.ai_model()}")
        elif ai_on:
            st.warning(f"AI configured but last call failed → fallback active ({status.get('error')})")
        else:
            st.warning("Fallback mode active (AI not configured or disabled)")
        if user:
            role = user["role"].title()
            extra = f" · {user['department_code']}" if user.get("department_code") else ""
            st.markdown(f"**Signed in:** `{user['cms_id']}`  \n{role}{extra}")
            if user["role"] == "student":
                st.markdown(f"Credit score: **{user['trust_score']}**")
            if st.button("Log out", key="logout"):
                st.session_state.clear()
                st.rerun()
        if config.JUDGE_MODE:
            _judge_panel(user)


def _judge_panel(user):
    st.markdown("---")
    st.markdown("### ⚖️ Judge Mode")
    st.caption("Local demo controls (JUDGE_MODE=true). Never reveals confidential reporter identities.")
    users = judge_mode.all_users(conn)
    labels = {u["id"]: f"{u['cms_id']} · {u['role']}" + (f" · {u['department_code']}" if u["department_code"] else "")
              + (" · conf-handler" if u["is_conf_handler"] else "") + (" · conf-officer" if u["is_conf_officer"] else "")
              for u in users}
    pick = st.selectbox("Switch to user", list(labels), format_func=labels.get, key="judge_pick")
    if st.button("Switch (no password)", key="judge_switch"):
        judge_mode.require_enabled()
        sign_in(auth.load_user(conn, pick))
        st.rerun()
    if st.button("➕ Create new student", key="judge_new"):
        judge_mode.require_enabled()
        info = judge_mode.create_judge_student(conn)
        sign_in(info["user"])
        st.session_state["judge_msg"] = f"Created {info['user']['cms_id']} (trust 100, password {info['password']})"
        st.rerun()
    if st.session_state.get("judge_msg"):
        st.info(st.session_state["judge_msg"])
    st.caption("New accounts are < 24 h old: 3+ of them joining or voting together are flagged for review (anti-brigading).")
    confirm = st.checkbox("I confirm resetting data", key="judge_confirm")
    c1, c2 = st.columns(2)
    if c1.button("Reset → demo data", key="judge_demo", disabled=not confirm):
        judge_mode.require_enabled()
        judge_mode.load_demo_data(conn)
        st.session_state.clear()
        st.rerun()
    if c2.button("Reset → empty", key="judge_empty", disabled=not confirm):
        judge_mode.require_enabled()
        judge_mode.reset_empty(conn)
        st.session_state.clear()
        st.rerun()
    if st.button("🧪 Judge Testing Centre", key="judge_tc"):
        st.session_state["view"] = "testing"
        st.rerun()
    if st.session_state.get("view") == "testing" and st.button("⬅ Back to dashboard", key="judge_back"):
        st.session_state["view"] = "dashboard"
        st.rerun()


# --------------------------------------------------------------- login page
def login_page():
    header("Campus Complaint & Accountability Portal")
    left, right = st.columns(2)
    with left:
        st.subheader("Log in")
        with st.form("login"):
            cms = st.text_input("CMS ID / account ID")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Log in"):
                try:
                    sign_in(auth.login(conn, cms, pw))
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))
    with right:
        st.subheader("Student registration")
        with st.form("register"):
            cms = st.text_input("CMS ID")
            pw = st.text_input("Password", type="password")
            pw2 = st.text_input("Confirm password", type="password")
            st.caption("Only CMS ID and password are required. Every new account starts with a credit score of 100. "
                       "Staff and admin accounts are created by an authorized admin.")
            if st.form_submit_button("Create student account"):
                try:
                    uid = auth.register_student(conn, cms, pw, pw2)
                    st.success("Account created. You can now log in.")
                except auth.AuthError as e:
                    st.error(str(e))
    if config.JUDGE_MODE:
        st.info("Judge Mode is enabled: use the sidebar to switch to any demo account without a password.")


# ----------------------------------------------------------- testing centre
def testing_centre(user):
    header("Judge Testing Centre")
    st.subheader("Try any new complaint")
    st.caption("Runs the real analysis pipeline WITHOUT saving anything.")
    text = st.text_area("Complaint text", key="tc_text")
    use_fallback = st.checkbox("Force fallback mode (simulate AI outage)", key="tc_fb")
    if st.button("Analyze", key="tc_run"):
        d = safe_call(svc.analyze_report, conn, text, None, False, use_fallback)
        if d:
            show_decision(d, svc.explain(d))
            loc = d.get("location") or {}
            dup_text = "Not applicable (location not verified or report confidential/rejected)."
            if loc.get("status") == "verified" and not d["confidential"] and not d["reject_reason"]:
                dup, reason = duplicate_engine.find_duplicate(conn, loc["location_id"], d["category"], d["normalized_problem"],
                                                              None, loc["location_name"], use_ai=not use_fallback)
                dup_text = (f"Would merge into {dup['public_id']} — {reason}" if dup else f"New issue — {reason}")
            rows = {
                "Detected department": d.get("department") or "—",
                "Detected location": f"{loc.get('location_name') or '—'} ({loc.get('status')})",
                "Location validation": loc.get("reason", ""),
                "Location suggestions": ", ".join(s["name"] for s in loc.get("suggestions", [])) or "—",
                "Problem category": d["category"], "Normalized problem": d["normalized_problem"],
                "AI suggested priority": d["analysis"].get("suggested_priority"), "Final base priority": d["base_priority"],
                "Safety hazard": f"{d['is_hazard']} ({d['hazard_type'] or '—'})",
                "Confidential": f"{d['confidential']} ({d['confidentiality_category'] or '—'})",
                "Prompt injection detected": f"{d['injection']} {d['injection_hits'] or ''}",
                "Duplicate matching": dup_text,
                "Clarification needed": d.get("clarification_question") or "No",
                "Rejected": d.get("reject_reason") or "No",
                "Analysis mode": d["mode"] + (f" (AI error: {d['ai_error']})" if d.get("ai_error") and d["mode"] != "ai" else ""),
            }
            st.table(pd.DataFrame(rows.items(), columns=["Check", "Result"]))
    st.markdown("---")
    st.subheader("Automated acceptance tests")
    st.caption("Executes the real application logic on an isolated temporary database (the live database is untouched).")
    with_ai = st.checkbox("Use the configured AI model for classification tests", value=False, key="tc_ai",
                          disabled=not ai_engine.ai_enabled(conn))
    if st.button("Run all automated tests", type="primary", key="tc_all"):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
        import scenarios
        with st.spinner("Running tests…"):
            st.session_state["tc_results"] = scenarios.run_all(use_ai=with_ai)
    res = st.session_state.get("tc_results")
    if res:
        passed = sum(r["Result"] == "PASSED" for r in res)
        if passed == len(res):
            st.success(f"{passed}/{len(res)} passed")
        else:
            st.error(f"{passed}/{len(res)} passed")
        st.dataframe(pd.DataFrame(res), hide_index=True)


# ---------------------------------------------------------------------- main
user = current_user()
sidebar(user)
if st.session_state.get("view") == "testing" and config.JUDGE_MODE:
    testing_centre(user)
elif not user:
    login_page()
else:
    if user["role"] == "student":
        header(f"Student · {user['cms_id']}")
        student_dash.render(conn, user)
    elif user["role"] == "staff":
        label = user["department_code"] or "Confidential Review"
        header(f"Department Staff · {label}")
        dept_dash.render(conn, user)
    else:
        header("Admin · Campus Operations Command Centre")
        admin_dash.render(conn, user)

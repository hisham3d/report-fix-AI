# ReportFix AI — Test Report

Generated in the build environment on 2026-11-24 (Linux, Python 3.13). Results below are the **actual output** of the
test commands, not hand-written.

## 1. Summary

| Check | Result |
|---|---|
| Syntax check (`py_compile` on every module) | ✅ all files compile |
| Pytest suite (`python -m pytest -q tests`) | ✅ 45 passed in 1.61s |
| Acceptance scenarios (`python tests/scenarios.py`) — 35 mandatory + 2 extra attack/robustness | ✅ 37/37 passed (mode: fallback) |
| Headless UI smoke test — every page for student, staff, confidential handler, confidential officer, admin, login, Testing Centre | ✅ no crashes |
| Headless UI smoke test with every action button pressed | ✅ no crashes; only expected validation messages |

## 2. Acceptance scenario output (fallback mode, isolated temporary database)

```
[PASSED] # 1 Roman Urdu understanding: status=created, dept=Labs, loc=Lab 3, cat=Air Conditioning
[PASSED] # 2 Semantic duplicate: status=merged, issue=1 (expected 1), affected=2
[PASSED] # 3 Different problem, same location: status=created, issue=2, category=Internet/WiFi
[PASSED] # 4 Same problem, different location: status=created, location=Lab 4
[PASSED] # 5 Exaggerated language: priority=Low, hazard=False
[PASSED] # 6 Calm safety hazard: hazard=True, priority=Critical, score=100
[PASSED] # 7 Gas hazard: priority=Critical, warning_shown=True
[PASSED] # 8 Medical emergency: priority=Critical, hazard_type=medical, warning=True
[PASSED] # 9 Prompt injection inside genuine complaint: injection=True, priority=Low, category=Furniture
[PASSED] #10 Malicious command only: status=rejected, issues 7->7, role=student
[PASSED] #11 Harassment confidentiality: status=confidential, tracking=RF-CONF-000EC5, public issues 7->7, handler_sees=True, ordinary_staff_denied=True
[PASSED] #12 Roman Urdu ragging: status=confidential, queue=admin_review, location_clarification=1
[PASSED] #13 Invalid location: status=pending_clarification, location_status=unknown, suggestions=['Lab 1', 'Lab 2', 'Lab 3']
[PASSED] #14 Typographical error: first=pending_clarification suggestions=['LOC-LIB']; after confirm=created dept=Library
[PASSED] #15 Duplicate submission by same student: status=already_reported, affected 2->2
[PASSED] #16 Department access control: view/start/resolve denied=[True, True, True], in EE queue=False
[PASSED] #17 Unauthorized voting: rejected: Vote rejected: only students who reported or joined this issue before the resolution may vote.
[PASSED] #18 Repeated voting: rejected: Vote rejected: you have already voted in this resolution round.; votes stored=1
[PASSED] #19 False resolution: status=Reopened, reopen_count=1
[PASSED] #20 Verified resolution: after 1 confirm=Resolved — Awaiting Verification; after 2 confirms=Verified and Closed
[PASSED] #21 Trust deduction: score 100 -> 90
[PASSED] #22 Trust recovery: after recovery=75, after second call same day=75
[PASSED] #23 Trust reward: status=Verified and Closed, score=90
[PASSED] #24 Trust boundaries: capped=100, floored=0
[PASSED] #25 Crowd escalation: Medium -> High (score 64): Priority increased from Medium to High because 6 unique affected students reported the same issue. Base Medium = 40; +24 crowd bonus (6 unique credible students, effective weight 5.75) → score 64 (High).
[PASSED] #26 Single high-trust student: priority=Low score=15, affected=1, repeat_flag=1
[PASSED] #27 Coordinated new accounts: status=Resolved — Awaiting Verification, open flags=['coordinated_votes', 'new_account_cluster']
[PASSED] #28 Confidential public-board protection: confidential reports=2, leaked_to_public=False
[PASSED] #29 Confidential identity protection: leaked tokens=[], reporter shown as 'Anonymous Student'
[PASSED] #30 Recurring issue: previous closed=Verified and Closed, linked_to=3, recurrence=1, score=45
[PASSED] #31 AI API failure: mode=fallback, error='AI request failed (ConnectionError)', priority=Critical
[PASSED] #32 Database persistence: (users, issues, votes, trust, confidential) before=(26, 16, 10, 7, 3) after=(26, 16, 10, 7, 3)
[PASSED] #34 Safety context / negation: hazard=False, priority=Medium, category=Fire Safety
[PASSED] #35 Confidentiality override attack: status=confidential, injection_detected=True, public issues 16->16
[PASSED] #36 Additional live-attack inputs: statuses=['rejected', 'rejected', 'rejected', 'rejected', 'rejected', 'rejected'], role=student, trust=100
[PASSED] #37 Robust input handling: empty rejected=True, long status=created, notes=['Report was very long and was trimmed to 2000 characters.']
[PASSED] #33 Judge reset: demo issues=10, after reset (issues, conf, votes, submissions)=(0, 0, 0, 0), accounts kept=True

37/37 passed (mode: fallback)
```

## 3. UI smoke test output (no clicks)

`errors=[...]` lists every `st.error` message shown; the only one is the intended SLA overdue warning.

```
300001         ok       errors=[]
300004         ok       errors=[]
staff.cse      ok       errors=[]
staff.library  ok       errors=['Response overdue — Administrative review recommended.']
conf.cse       ok       errors=[]
conf.officer   ok       errors=[]
admin          ok       errors=[]
None           ok       errors=[]
testing        ok       errors=[]
```

## 4. Unseen-complaint probe (fallback classifier)

| Input | Dept | Location | Category | Priority |
|---|---|---|---|---|
| IS block ke washroom mein pani bhar gaya hai aur sewage overflow ho raha hai | IS | IS Block | Plumbing/Water | High |
| cs lab k computers boht slow hain, aadhe band hain | CSE | CSE Computer Lab | Computer/IT Equipment | Medium |
| Reading hall ki lights nahi jal rahi | Library | Library Reading Hall | Electrical | Medium (fixed: was a false fire hazard before Roman-Urdu pre-verb negation was added) |
| Canteen ke samose baasi thay, kai students beemar | Cafeteria | Cafeteria | Food Hygiene | High |
| There's smoke coming out of the switchboard in EE classroom 101 | EE | EE Classroom 101 | Fire Safety | Critical |
| The library wifi is down again!!!! I will complain to the commandant | Library | Library | Internet/WiFi | Medium (threat to complain ignored) |
| ee lab ka oscilloscope kharab hai | EE | EE Power Lab | Lab Equipment | Medium |
| projector | — | (missing → clarification) | Projector/Display | Medium |
| My teacher keeps threatening to fail me if I complain | — | (confidential, independent route) | Harassment/Misconduct | High |
| Lab 2 mein kursi ki taang tooti hui hai | Labs | Lab 2 | Furniture | Low |

## 5. Issues found and fixed during testing

1. Coordinated new accounts could reopen an issue with only two disputes before the vote-cluster rule triggered →
   participants already held as a new-account cluster now cast *held* votes that are excluded until admin review.
2. Fallback duplicate rule merged “cafeteria chair broken” with “cafeteria table wobbly” → fallback now requires the same
   normalized problem except for single-equipment categories (AC, WiFi, projector…).
3. Rate limit (5 / 10 min) blocked rapid judge testing → raised to 8 / 10 min (safety hazards are never rate-limited).
4. “lights nahi jal rahi” was treated as an active fire → Roman Urdu negation before the verb and lighting context added.
5. `strip_emphasis` left a leading “!” → trimmed.

## 6. What could NOT be tested here (please verify on the judging laptop)

* **Live AI mode.** This build environment has no network access and no API key, so every scenario ran in **fallback
  mode**. The AI path is covered by: schema-validation unit tests (invalid enums rejected), the AI-failure scenario (#31:
  unreachable endpoint → graceful fallback), and fact-checking of the AI briefing. Run `python tests/scenarios.py --ai`
  or the Testing Centre with *“Use the configured AI model”* after adding your key.
* **Real browser UI.** Streamlit and RapidFuzz could not be installed here (package installs blocked), so the UI was
  verified with a headless Streamlit stand-in that executes every page and button handler. Fuzzy matching used the
  built-in `difflib` fallback with the same threshold; RapidFuzz is used automatically once installed.
* **Location discovery from the official website** (needs internet). The feature fetches only the public URL, filters
  sensitive facility names and stores results as *Suggested* (not routable).
* **Windows** specifically: the code uses only cross-platform paths (`pathlib`) and the standard library + listed packages.

## 7. Known limitations (honest)

* Fallback mode is a keyword/regex classifier with Roman Urdu vocabulary; it is conservative and clearly labelled, but it
  is not equivalent to AI understanding (e.g. unusual phrasing may become “Other” and ask for clarification).
* Anonymity protects reporter identity from other users and staff; it cannot protect against someone with unrestricted
  access to the local machine or database file.
* The Testing Centre temporarily shifts the in-process clock / AI environment while scenarios run; run it when no one is
  submitting live reports at the same moment.

"""Verified campus location registry, resolution and fuzzy correction.

Rules:
* Only locations with verification_status='Verified' can be used for routing.
* A misspelt location is SUGGESTED ("Did you mean ...?"), never silently replaced.
* An explicit but unregistered room (e.g. "Lab 99") is never routed.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import auth
import config
import database as db

try:
    from rapidfuzz import fuzz as _rf_fuzz  # type: ignore

    def similarity(a: str, b: str) -> float:
        return float(_rf_fuzz.ratio(a, b))
except Exception:  # pragma: no cover - fallback when rapidfuzz isn't installed
    def similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100.0

FUZZY_THRESHOLD = 82.0
KIND_RANK = {"room": 3, "facility": 2, "block": 1}

_NUM_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "aik": "1", "ek": "1", "teen": "3", "char": "4", "chaar": "4", "panch": "5",
    "paanch": "5", "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
}
_ROOM_WORDS = r"(lab|room|classroom|class)"


def normalize(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[‘’'`]", "", t)
    t = re.sub(r"\blaborator(?:y|ies)\b", lambda m: "laboratories" if m.group(0).endswith("ies") else "lab", t)
    t = re.sub(r"\blabarotary\b|\blabortary\b|\blaboratry\b", "lab", t)
    t = re.sub(r"[^a-z0-9\s-]", " ", t)
    t = t.replace("-", " ")
    t = re.sub(r"\s+", " ", t).strip()
    # "lab no 3", "lab number 3", "lab # 3" -> "lab 3"
    t = re.sub(rf"\b{_ROOM_WORDS}\s+(?:no|number|num|nmbr|nbr)\s+(?=\w)", r"\1 ", t)
    # "lab three" -> "lab 3" ; "third lab" -> "lab 3"
    t = re.sub(rf"\b{_ROOM_WORDS}\s+([a-z]+)\b",
               lambda m: f"{m.group(1)} {_NUM_WORDS[m.group(2)]}" if m.group(2) in _NUM_WORDS else m.group(0), t)
    t = re.sub(r"\b(first|second|third|fourth|fifth)\s+lab\b", lambda m: f"lab {_NUM_WORDS[m.group(1)]}", t)
    # "lab3" -> "lab 3"
    t = re.sub(r"\b(lab|room)(\d+)\b", r"\1 \2", t)
    return t


def load_registry(conn, include_unverified: bool = False) -> list[dict]:
    where = "" if include_unverified else "WHERE l.verification_status = 'Verified'"
    locs = db.rows(conn, f"SELECT * FROM locations l {where} ORDER BY name")
    alias_rows = db.rows(conn, "SELECT location_id, alias FROM location_aliases")
    amap: dict[str, list[str]] = {}
    for a in alias_rows:
        amap.setdefault(a["location_id"], []).append(a["alias"])
    for loc in locs:
        names = {normalize(loc["name"])} | {normalize(a) for a in amap.get(loc["id"], [])}
        loc["aliases"] = sorted(n for n in names if n)
    return locs


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _result(status, loc=None, suggestions=None, reason="", confidence=0.0, question=None):
    return {
        "status": status,  # verified | suggest | unknown | missing | ambiguous
        "location_id": loc["id"] if loc else None,
        "location_name": loc["name"] if loc else None,
        "department": loc["department_code"] if loc else None,
        "suggestions": [{"id": s["id"], "name": s["name"], "department": s["department_code"]} for s in (suggestions or [])][:3],
        "reason": reason,
        "confidence": round(confidence, 2),
        "question": question,
    }


def _fuzzy_candidates(text: str, registry: list[dict]) -> list[tuple[float, dict, str]]:
    tokens = text.split()
    grams = set()
    for n in (1, 2, 3):
        for i in range(len(tokens) - n + 1):
            g = " ".join(tokens[i:i + n])
            if len(g) >= 4:
                grams.add(g)
    best: dict[str, tuple[float, dict, str]] = {}
    for loc in registry:
        for alias in loc["aliases"]:
            if len(alias) < 4:
                continue
            a_n = len(alias.split())
            for g in grams:
                if len(g.split()) != a_n or g == alias:
                    continue
                s = similarity(g, alias)
                if s >= FUZZY_THRESHOLD and (loc["id"] not in best or s > best[loc["id"]][0]):
                    best[loc["id"]] = (s, loc, g)
    return sorted(best.values(), key=lambda x: -x[0])


def _similar_rooms(ref: str, registry: list[dict]) -> list[dict]:
    word = ref.split()[0]
    same_kind = [l for l in registry if any(a.startswith(word + " ") and a.split()[-1].isdigit() for a in l["aliases"])]
    scored = sorted(same_kind, key=lambda l: -max(similarity(ref, a) for a in l["aliases"]))
    return scored[:3]


CATEGORY_LOCATION_HINTS = {
    "Food Quality": ["LOC-CAF"], "Food Hygiene": ["LOC-CAF"], "Library Services": ["LOC-LIB"],
    "Internet/WiFi": ["LOC-LIB", "LOC-CSE-BLK", "LOC-CSE-LAB"], "Air Conditioning": ["LOC-LIB", "LOC-LAB-3", "LOC-CSE-201"],
    "Projector/Display": ["LOC-CSE-201", "LOC-EE-101", "LOC-CSE-LAB"], "Lab Equipment": ["LOC-LABS", "LOC-EE-PWR"],
}

QUESTION_BY_CATEGORY = {
    "Air Conditioning": "Which classroom, laboratory or building has the faulty AC?",
    "Internet/WiFi": "Is the problem in the Library, CSE Block, a laboratory or another location?",
    "Projector/Display": "Which classroom or lab has the faulty projector/display?",
    "Food Quality": "Is this about the Cafeteria or another food outlet?",
    "Food Hygiene": "Is this about the Cafeteria or another food outlet?",
}


def resolve(conn, text: str, ai_hint: str | None = None, category: str | None = None) -> dict:
    registry = load_registry(conn)
    norm = normalize(text)
    by_id = {l["id"]: l for l in registry}

    # 1) explicit numbered rooms ("lab 3", "room 12")
    numbered = re.findall(r"\b(lab|room|classroom)\s+(\d{1,4})\b", norm)
    unknown_refs = []
    for word, num in numbered:
        ref = f"{word} {num}"
        hits = [l for l in registry if ref in l["aliases"]]
        if hits:
            loc = hits[0]
            return _result("verified", loc, reason=f"'{ref.title()}' matches the verified location '{loc['name']}'.",
                           confidence=0.97)
        # "cse lab 2"-style references are handled by aliases below
        prefix_alias = re.search(rf"\b([a-z]+)\s+{word}\s+{num}\b", norm)
        if not (prefix_alias and any(_contains_phrase(norm, a) for l in registry for a in l["aliases"])):
            unknown_refs.append(ref)
    if unknown_refs:
        ref = unknown_refs[0]
        sugg = _similar_rooms(ref, registry)
        return _result("unknown", suggestions=sugg,
                       reason=f"'{ref.title()}' is not in the verified campus location registry.",
                       question="We could not verify this location. Did you mean one of the following?")

    # 2) exact alias phrases
    exact = []
    for loc in registry:
        for alias in loc["aliases"]:
            if _contains_phrase(norm, alias):
                exact.append((KIND_RANK.get(loc["kind"], 1), len(alias), loc, alias))
    if exact:
        exact.sort(key=lambda x: (-x[0], -x[1]))
        top_rank = exact[0][0]
        top_locs = {e[2]["id"]: e for e in exact if e[0] == top_rank}
        # Drop a location whose alias is contained in a longer matched alias of another (e.g. "lib" vs "library hall").
        if len(top_locs) > 1:
            locs = [e[2] for e in top_locs.values()]
            # Prefer a location in the same department as a broader matched location (e.g. "CSE block ... cse lab")
            return _result("ambiguous", suggestions=locs,
                           reason="The report mentions more than one verified location.",
                           question="Your report mentions more than one location. Which one has the problem?")
        _, _, loc, alias = exact[0]
        return _result("verified", loc, reason=f"'{alias}' matches the verified location '{loc['name']}'.",
                       confidence=0.95)

    # 3) fuzzy spelling correction -> suggestion only
    fuzzy = _fuzzy_candidates(norm, registry)
    if fuzzy:
        score, loc, gram = fuzzy[0]
        sugg = [f[1] for f in fuzzy[:3]]
        return _result("suggest", suggestions=sugg, confidence=score / 100,
                       reason=f"'{gram}' looks like a misspelling of '{loc['name']}'.",
                       question=f"Did you mean {loc['name']}?")

    # 4) AI hint, accepted only if it maps exactly onto a verified registry entry
    if ai_hint:
        hint = normalize(ai_hint)
        for loc in registry:
            if hint in loc["aliases"] or any(_contains_phrase(hint, a) for a in loc["aliases"] if len(a) >= 4):
                return _result("verified", loc, confidence=0.85,
                               reason=f"The AI interpreted the described place as '{ai_hint}', which matches the "
                                      f"verified location '{loc['name']}'.")
        if re.search(r"\b(lab|room|classroom)\s+\d+", hint):
            return _result("unknown", suggestions=_similar_rooms(re.search(r"\b(lab|room|classroom)\s+\d+", hint).group(0), registry),
                           reason=f"'{ai_hint}' is not a verified location.",
                           question="We could not verify this location. Did you mean one of the following?")

    hints = [by_id[i] for i in CATEGORY_LOCATION_HINTS.get(category or "", []) if i in by_id]
    q = QUESTION_BY_CATEGORY.get(category or "", "Where exactly is this problem? Please name the building, classroom or lab.")
    return _result("missing", suggestions=hints, reason="No location was mentioned in the report.", question=q)


def get_location(conn, location_id: str, verified_only: bool = True) -> dict | None:
    sql = "SELECT * FROM locations WHERE id = ?" + (" AND verification_status='Verified'" if verified_only else "")
    return db.one(conn, sql, (location_id,))


# ------------------------------------------------------------ admin CRUD
def _require_admin(conn, actor, reason):
    auth.require_role(conn, actor, "admin")
    if not (reason or "").strip():
        raise ValueError("A reason is required for admin actions.")


def upsert_location(conn, actor, loc_id: str, name: str, department: str, kind: str, aliases: list[str],
                    source: str, status: str = "Verified", reason: str = "") -> None:
    _require_admin(conn, actor, reason)
    from constants import DEPARTMENTS
    if department not in DEPARTMENTS:
        raise ValueError("Unknown department.")
    if kind not in KIND_RANK:
        raise ValueError("Kind must be room, facility or block.")
    loc_id = (loc_id or "").strip().upper() or ("LOC-" + re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-"))
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO locations(id, name, department_code, kind, verification_source, verification_status, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, department_code=excluded.department_code,
                   kind=excluded.kind, verification_source=excluded.verification_source,
                   verification_status=excluded.verification_status""",
            (loc_id, name.strip(), department, kind, source.strip() or "Admin entry", status, db.now_iso()),
        )
        conn.execute("DELETE FROM location_aliases WHERE location_id = ?", (loc_id,))
        for a in {a.strip().lower() for a in aliases if a.strip()} | {name.strip().lower()}:
            conn.execute("INSERT OR IGNORE INTO location_aliases(location_id, alias) VALUES (?, ?)", (loc_id, a))
        db.audit(conn, actor, "upsert_location", f"{loc_id} ({name}) status={status}", reason)


def remove_location(conn, actor, loc_id: str, reason: str) -> str:
    _require_admin(conn, actor, reason)
    used = db.one(conn, "SELECT COUNT(*) c FROM issues WHERE location_id = ?", (loc_id,))["c"]
    with db.transaction(conn):
        if used:
            conn.execute("UPDATE locations SET verification_status='Rejected' WHERE id = ?", (loc_id,))
            outcome = "Location is referenced by issues, so it was deactivated instead of deleted."
        else:
            conn.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
            outcome = "Location removed."
        db.audit(conn, actor, "remove_location", loc_id, reason)
    return outcome


def seed_locations(conn, path: Path | None = None) -> int:
    path = path or (config.BASE_DIR / "data" / "campus_locations.json")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    n = 0
    with db.transaction(conn):
        for loc in data["locations"]:
            conn.execute(
                """INSERT OR REPLACE INTO locations(id, name, department_code, kind, verification_source,
                       verification_status, created_at) VALUES (?,?,?,?,?,?,?)""",
                (loc["id"], loc["name"], loc["department"], loc.get("kind", "facility"),
                 loc.get("source", "Demonstration registry (fictional)"), "Verified", db.now_iso()),
            )
            for a in set(loc.get("aliases", [])) | {loc["name"].lower()}:
                conn.execute("INSERT OR IGNORE INTO location_aliases(location_id, alias) VALUES (?, ?)", (loc["id"], a))
            n += 1
    return n


def import_locations(conn, actor, payload: str, reason: str) -> int:
    _require_admin(conn, actor, reason)
    data = json.loads(payload)
    items = data["locations"] if isinstance(data, dict) else data
    count = 0
    for loc in items:
        upsert_location(conn, actor, loc.get("id", ""), loc["name"], loc["department"], loc.get("kind", "facility"),
                        loc.get("aliases", []), loc.get("source", "Admin import"), "Verified", reason)
        count += 1
    return count


# ------------------------------------------------ optional public discovery
_SENSITIVE = re.compile(r"armou?ry|ammunition|magazine|quarter\s*guard|guard\s*room|security\s*(office|post|control)|"
                        r"control\s*room|cctv|restricted|weapon|bunker|server\s*room|signals?\s*cent|ops\s*room", re.I)
_CANDIDATE = re.compile(r"\b([A-Z][A-Za-z&]+(?:\s+[A-Z][A-Za-z&]+){0,3}\s+"
                        r"(?:Block|Lab|Laboratory|Library|Hall|Auditorium|Cafeteria|Department|Centre|Center))\b")


def discover_locations(conn, actor, url: str, reason: str) -> list[str]:
    """Fetch a PUBLIC official page and store candidate facility names as *Suggested* (not routable).

    Only the URL is sent over the network. Complaint text / reporter data are never involved.
    """
    _require_admin(conn, actor, reason)
    import requests

    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Enter a full http(s) URL of the official public website.")
    resp = requests.get(url, timeout=10, headers={"User-Agent": "ReportFixAI-location-discovery"})
    resp.raise_for_status()
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", resp.text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    found = []
    for m in _CANDIDATE.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        if _SENSITIVE.search(name) or len(name) > 60:
            continue
        if name.lower() not in {f.lower() for f in found}:
            found.append(name)
    existing = {l["name"].lower() for l in load_registry(conn, include_unverified=True)}
    added = []
    for name in found[:40]:
        if name.lower() in existing:
            continue
        upsert_location(conn, actor, "", name, "Labs" if "lab" in name.lower() else "Library" if "library" in name.lower() else "CSE",
                        "facility", [], f"Public web suggestion: {url}", "Suggested", reason)
        added.append(name)
    return added

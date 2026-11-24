"""Meaning-based duplicate detection.

Candidates must share the canonical verified location ID AND normalized category, and be
active (not closed/rejected). Confidential reports are never in this table, so they can
never merge into public issues. Then semantic confirmation:
  1. embeddings available  -> cosine similarity (>=0.82 same, 0.55-0.82 ask AI, <0.55 different)
  2. AI available           -> AI same-problem verification
  3. fallback               -> same location + same category = same problem
"""
from __future__ import annotations

import json
import math

import ai_engine
import database as db
from constants import ACTIVE_STATUSES, S_CLOSED


SINGLE_EQUIPMENT = {"Air Conditioning", "Internet/WiFi", "Projector/Display", "Gas Leak", "Fire Safety"}


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def find_duplicate(conn, location_id: str, category: str, normalized_problem: str, embedding: list[float] | None,
                   location_name: str, use_ai: bool = True) -> tuple[dict | None, str]:
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    candidates = db.rows(conn, f"""SELECT * FROM issues WHERE location_id = ? AND category = ?
                                   AND status IN ({placeholders}) ORDER BY created_at""",
                         (location_id, category, *ACTIVE_STATUSES))
    if not candidates:
        return None, "No active issue with the same verified location and problem category."
    for issue in candidates:
        emb = json.loads(issue["embedding"]) if issue.get("embedding") else None
        if embedding and emb:
            sim = cosine(embedding, emb)
            if sim >= 0.82:
                return issue, f"Merged: same verified location and category, semantic similarity {sim:.2f}."
            if sim < 0.55:
                continue
        if use_ai and ai_engine.ai_enabled(conn) and issue["normalized_problem"].lower() != normalized_problem.lower():
            verdict = ai_engine.same_problem(issue["normalized_problem"], normalized_problem, location_name, conn)
            if verdict is True:
                return issue, "Merged: same verified location and category; AI verified it is the same problem."
            if verdict is False:
                continue
        # Deterministic fallback: single-equipment categories merge; otherwise the normalized problem must match
        # (e.g. "Chair broken" and "Table broken" at the Cafeteria stay separate).
        if category in SINGLE_EQUIPMENT or issue["normalized_problem"].lower() == normalized_problem.lower():
            return issue, ("Merged: same verified location and same normalized problem "
                           f"('{issue['normalized_problem']}' at {location_name}).")
    return None, "Similar-category issues exist here but were judged to be different problems."


def find_previous_closed(conn, location_id: str, category: str) -> list[dict]:
    return db.rows(conn, """SELECT * FROM issues WHERE location_id = ? AND category = ? AND status = ?
                            ORDER BY closed_at""", (location_id, category, S_CLOSED))

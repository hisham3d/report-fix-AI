"""Deterministic safety detection and priority scoring.

The AI *suggests* a priority. Python decides the final priority:
* genuine safety hazards are always Critical (score 100) and never downgraded,
* emotion/shouting/"urgent" never raises priority (it is stripped before analysis),
* non-hazard reports are capped at High,
* crowd and recurrence bonuses follow a transparent formula.
"""
from __future__ import annotations

import re

from constants import PRIORITY_BASE_SCORE

# (hazard_type, regex, negatable)
_HAZARD_PATTERNS: list[tuple[str, str, bool]] = [
    ("gas", r"\bgas\s+(?:leak\w*|ki\s+(?:smell|boo|bu|badboo|bdboo)|smell\w*|odou?r)|\b(?:smell|boo|bu|odou?r)\s+(?:of|se)\s+gas\b|\bcylinder\s+leak\w*|\bgas\s+nikal", True),
    ("fire", r"\bfire\b(?!\s*(?:extinguisher|drill|exit|alarm|safety|inspection|door|hose|equipment|fighting|marshal|training|sign|bucket|wall))", True),
    ("fire", r"\baag\b(?!\s*bujha)|\bflames?\b|\bon\s+fire\b|\bcaught\s+fire\b|\bdhu(?:a|wa|aa)n\b|\bsmoke\b(?!\s*(?:detector|alarm))|\bburning\b|\bjal\s+(?:raha|rahi|rha|rhi|gaya|gayi|gya)\b|\bjalne\s+ki\s+(?:boo|bu|smell)", True),
    ("electrical", r"\bspark\w*|\bshort\s*circuit\w*|\belectric(?:al)?\s+shock|\belectrocut\w*|\bcurrent\s+(?:lag|lg|maar|mar)\w*|\bkarant\b|\bbijli\s+ka\s+jhatka|\b(?:exposed|naked|nangi|nangay|nangee|open)\s+(?:wire|wires|tar|taar|taaren)\b|\blive\s+wire|\bwire\w*\s+(?:se\s+)?(?:pighal|jal)\w*", True),
    ("structural", r"\b(?:ceiling|roof|chhat|chatt|deewar|diwar|wall|building|staircase|stairs|balcony|beam)\w*\s+(?:\w+\s+){0,3}?(?:collaps\w*|gir\s+(?:rahi|raha|rhi|rha|gayi|gaya|gai|gya|sakti|sakta)|is\s+falling|fell\s+down|falling\s+down|giri|gira)", True),
    ("medical", r"\bsaans\s+nahi|\bsaans\s+nhi|\bnot\s+breathing|\bhosh\s+(?:nahi|nhi)|\bno\s+pulse", False),
    ("medical", r"\bbe\s?hosh\b|\bunconscious\b|\bfaint(?:ed|ing)?\b|\bcollapsed\b|\bbleeding\b|\bkhoon\s+(?:beh|nikal|bah)\w*|\bheart\s+attack|\bseizure\w*|\bdaura\s+(?:para|pada|par|pad)\w*|\bseverely\s+injured|\bbadly\s+injured|\bzakhmi\b|\bmedical\s+emergency|\ballergic\s+reaction|\b(?:is|was|student)\s+choking\b|\bfracture\w*|\bbroken\s+(?:arm|leg|bone)", True),
    ("personal_safety", r"\b(?:gun|pistol|weapon|knife|chaku|chhuri|chhura)\b|\bthreaten\w*\s+to\s+(?:kill|hurt|beat|shoot|stab)|\bjaan\s+se\s+maar|\battack(?:ed|ing)?\s+(?:me|us|him|her|a\s+student|students)|\bkill\s+(?:me|him|her)\b", True),
]

_NEG_BEFORE = re.compile(r"(?:\bno\b|\bnot\b|\bnever\b|\bwithout\b|\bisn'?t\b|\baren'?t\b|\bwasn'?t\b|\bnone\b|\bfree\s+of\b|\bnahi\b|\bnahin\b|\bnhi\b)(?:\s+\w+){0,2}\s*$")
_NEG_AFTER = re.compile(r"^\s*(?:\w+\s+){0,1}(?:nahi|nahin|nhi|nai|nahi\b)\b")
_FALSE_CONTEXT = re.compile(r"\b(?:drill|mock|test(?:ing)?|exercise|training|inspection|poster|movie|game|video|lights?|batti|bulbs?|tube\s*lights?|chulha|stove|candle|mombatti)\b")

_EMPHASIS_WORDS = re.compile(r"\b(?:urgent(?:ly)?|asap|a\.s\.a\.p|immediately|right\s+now|jaldi|jaldi\s+se|fori(?:\s+tor\s+par)?|"
                             r"emergency\s+basis|please\s+please|plz+|pls+|i\s+will\s+complain\s+to\s+\w+|"
                             r"(?:complain|report)\s+(?:to|it\s+to)\s+(?:the\s+)?(?:commandant|principal|higher\s+authorit\w+|"
                             r"ghq|media|dean|vc))\b", re.I)


def strip_emphasis(text: str) -> str:
    """Remove shouting/urgency markers so tone cannot inflate priority."""
    t = text or ""
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        t = t.lower()
    t = re.sub(r"([!?.])\1+", r"\1", t)
    t = _EMPHASIS_WORDS.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^[\s!?.,:;-]+", "", t)


def detect_hazard(text: str) -> tuple[bool, str | None, str]:
    """Deterministic hazard check with negation/context handling."""
    low = (text or "").lower()
    for htype, pattern, negatable in _HAZARD_PATTERNS:
        for m in re.finditer(pattern, low):
            if negatable:
                before = low[max(0, m.start() - 40):m.start()]
                after = low[m.end():m.end() + 20]
                if _NEG_BEFORE.search(before) or _NEG_AFTER.search(after):
                    continue
                clause = re.split(r"[.,;!?]|\bbut\b|\blekin\b|\bmagar\b", low[max(0, m.start() - 60):m.end() + 40])
                window = next((c for c in clause if m.group(0) in c), "")
                if htype == "fire" and _FALSE_CONTEXT.search(window):
                    continue
            return True, htype, f"'{m.group(0)}' indicates a possible immediate {htype.replace('_', ' ')} hazard."
    return False, None, ""


_WIDESPREAD = re.compile(r"\b(?:whole|entire|poore|pure|poora|pura|sare|saare)\s+(?:campus|block|building|lab|department|college)|"
                         r"\ball\s+(?:labs|classrooms|computers|systems|blocks|departments|students)|\bsab\s+(?:labs|computers|systems|jagah)|"
                         r"\beverywhere\s+on\s+campus|\bcampus[-\s]wide\b")
_SEVERE_SANITATION = re.compile(r"\bsewage|\boverflow\w*|\bganda\s+pani|\bflood\w*|\bbhar\s+gaya|\bpani\s+bhar")

CATEGORY_BASELINE = {
    "Air Conditioning": "Medium", "Internet/WiFi": "Medium", "Projector/Display": "Medium", "Electrical": "Medium",
    "Computer/IT Equipment": "Medium", "Lab Equipment": "Medium", "Furniture": "Low", "Food Quality": "Low",
    "Food Hygiene": "High", "Sanitation/Cleanliness": "Medium", "Plumbing/Water": "Medium", "Structural": "Medium",
    "Library Services": "Low", "Fire Safety": "Medium", "Gas Leak": "High", "Medical Emergency": "High",
    "Security/Personal Safety": "High", "Harassment/Misconduct": "High", "Other": "Low",
}

CATEGORY_IMPACT_TEXT = {
    "Low": "a minor inconvenience with limited impact",
    "Medium": "an equipment or facility failure affecting normal academic activity",
    "High": "a significant problem affecting many students or health/sanitation",
}


def baseline_priority(category: str, text: str) -> tuple[str, str]:
    base = CATEGORY_BASELINE.get(category, "Low")
    low = (text or "").lower()
    reason = f"{category} problems are treated as {CATEGORY_IMPACT_TEXT.get(base, base)}."
    if base == "Medium" and _WIDESPREAD.search(low):
        return "High", f"{category} failure appears widespread (affecting a whole area or many users)."
    if category in ("Sanitation/Cleanliness", "Plumbing/Water") and _SEVERE_SANITATION.search(low):
        return "High", "Severe sanitation/water problem (sewage, overflow or flooding)."
    return base, reason


def decide_base_priority(category: str, text: str, ai_priority: str | None, ai_reason: str | None,
                         ai_hazard: bool, det_hazard: bool, hazard_reason: str, injection: bool,
                         mode: str) -> tuple[str, bool, str]:
    """Return (base_priority, is_hazard, reason)."""
    if det_hazard:
        return "Critical", True, "Safety override: " + hazard_reason
    if ai_hazard and not injection and mode == "ai":
        return "Critical", True, "Safety override: the AI recognised a genuine immediate safety hazard. " + (ai_reason or "")
    rule_p, rule_reason = baseline_priority(category, text)
    if mode == "ai" and ai_priority in PRIORITY_BASE_SCORE and not injection:
        p = ai_priority
        if p == "Critical":  # Critical is reserved for verified safety hazards
            p = "High"
            return p, False, (ai_reason or "") + " (Capped at High: Critical is reserved for safety hazards.)"
        return p, False, ai_reason or rule_reason
    if injection:
        rule_reason += " Instructions inside the report were ignored; priority is based only on the actual problem."
    return rule_p, False, rule_reason


def trust_weight(score: int) -> float:
    if score >= 80:
        return 1.0
    if score >= 60:
        return 0.75
    if score >= 40:
        return 0.5
    return 0.25


def recurrence_bonus(count: int) -> int:
    return 0 if count <= 0 else 5 if count == 1 else 10 if count == 2 else 15


def classify_score(score: int) -> str:
    if score >= 100:
        return "Critical"
    if score >= 60:
        return "High"
    if score >= 30:
        return "Medium"
    return "Low"


def compute_priority(base_priority: str, is_hazard: bool, credible_weights: list[float], recurrence_count: int,
                     total_participants: int | None = None) -> tuple[int, str, str]:
    """Return (score, priority_label, explanation)."""
    if is_hazard:
        return 100, "Critical", "Critical safety hazard: score fixed at 100 regardless of crowd size or reporter trust."
    base = PRIORITY_BASE_SCORE[base_priority]
    effective = sum(credible_weights)
    crowd_bonus = min(45, round(5 * max(0.0, effective - 1)))
    rec = recurrence_bonus(recurrence_count)
    score = min(99, base + crowd_bonus + rec)
    label = classify_score(score)
    n = len(credible_weights)
    parts = [f"Base {base_priority} = {base}"]
    if crowd_bonus:
        parts.append(f"+{crowd_bonus} crowd bonus ({n} unique credible students, effective weight {effective:.2f})")
    if rec:
        parts.append(f"+{rec} recurrence bonus ({recurrence_count} previous verified occurrence(s))")
    if total_participants is not None and total_participants > n:
        parts.append(f"{total_participants - n} participant(s) held for review and excluded")
    expl = "; ".join(parts) + f" → score {score} ({label})."
    if label != base_priority:
        reasons = []
        if crowd_bonus:
            reasons.append(f"{n} unique affected students reported the same issue")
        if rec:
            reasons.append("the problem has recurred after verified fixes")
        expl = f"Priority increased from {base_priority} to {label} because " + " and ".join(reasons) + ". " + expl
    return score, label, expl

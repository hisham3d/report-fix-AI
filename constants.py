"""Fixed vocabularies shared by the AI layer and the deterministic rules."""

DEPARTMENTS = {
    "EE": "Electrical Engineering",
    "CSE": "Computer Science and Engineering",
    "IS": "Information Security",
    "Library": "Library",
    "Cafeteria": "Cafeteria",
    "Labs": "Labs",
}

ROLES = ("student", "staff", "admin")

PRIORITIES = ("Low", "Medium", "High", "Critical")
PRIORITY_BASE_SCORE = {"Low": 15, "Medium": 40, "High": 70, "Critical": 100}

CATEGORIES = (
    "Air Conditioning",
    "Internet/WiFi",
    "Projector/Display",
    "Electrical",
    "Computer/IT Equipment",
    "Lab Equipment",
    "Furniture",
    "Food Quality",
    "Food Hygiene",
    "Sanitation/Cleanliness",
    "Plumbing/Water",
    "Structural",
    "Library Services",
    "Fire Safety",
    "Gas Leak",
    "Medical Emergency",
    "Security/Personal Safety",
    "Harassment/Misconduct",
    "Other",
)

HAZARD_TYPES = ("fire", "electrical", "gas", "medical", "structural", "personal_safety")
HAZARD_CATEGORY = {
    "fire": "Fire Safety",
    "electrical": "Electrical",
    "gas": "Gas Leak",
    "medical": "Medical Emergency",
    "structural": "Structural",
    "personal_safety": "Security/Personal Safety",
}

CONFIDENTIAL_CATEGORIES = (
    "harassment",
    "ragging",
    "bullying",
    "intimidation",
    "discrimination",
    "abuse",
    "threats",
    "retaliation",
    "personal_safety",
)

# Lifecycle statuses
S_PENDING = "Pending Clarification"
S_SUBMITTED = "Submitted"
S_ASSIGNED = "Assigned"
S_IN_PROGRESS = "In Progress"
S_RESOLVED = "Resolved — Awaiting Verification"
S_CLOSED = "Verified and Closed"
S_REOPENED = "Reopened"
S_REJECTED = "Rejected with Reason"

STATUSES = (S_PENDING, S_SUBMITTED, S_ASSIGNED, S_IN_PROGRESS, S_RESOLVED, S_CLOSED, S_REOPENED, S_REJECTED)
ACTIVE_STATUSES = (S_SUBMITTED, S_ASSIGNED, S_IN_PROGRESS, S_RESOLVED, S_REOPENED)

PRIORITY_COLORS = {"Critical": "#c62828", "High": "#ef6c00", "Medium": "#f9a825", "Low": "#546e7a"}
STATUS_COLORS = {S_CLOSED: "#2e7d32", S_REOPENED: "#ad1457", S_RESOLVED: "#00838f", S_REJECTED: "#616161"}

SAFETY_GUIDANCE = (
    "⚠️ POSSIBLE IMMEDIATE HAZARD: Move away from the danger and contact campus emergency "
    "personnel / duty staff directly right now (security office, medical inspection room or duty officer). "
    "Do not rely only on this app for emergencies."
)

"""Authentication and backend authorization helpers.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib, 240k iterations, random salt).
Every privileged operation re-loads the acting user from the database and checks
role/department here, so hiding a UI button is never the only protection.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

import database as db

ITERATIONS = 240_000
CMS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")


class AuthError(Exception):
    pass


class PermissionDenied(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _create_user(conn, cms_id, password, role, department_code=None, display_name=None,
                 is_conf_handler=0, is_conf_officer=0, trust_score=100, created_at=None) -> int:
    created = created_at or db.now_iso()
    import config
    rec_date = config.local_today(db.parse_ts(created)).isoformat()
    cur = conn.execute(
        """INSERT INTO users(cms_id, password_hash, role, department_code, display_name, is_conf_handler,
               is_conf_officer, trust_score, last_recovery_date, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cms_id, hash_password(password), role, department_code, display_name, int(is_conf_handler),
         int(is_conf_officer), int(trust_score), rec_date, created),
    )
    return cur.lastrowid


def register_student(conn, cms_id: str, password: str, confirm: str) -> int:
    """Public self-registration. Always creates role='student' with trust 100."""
    cms_id = (cms_id or "").strip()
    if not CMS_RE.match(cms_id):
        raise AuthError("CMS ID must be 3–32 characters: letters, digits, dot, dash or underscore.")
    if len(password or "") < 6:
        raise AuthError("Password must be at least 6 characters.")
    if password != confirm:
        raise AuthError("Passwords do not match.")
    if db.one(conn, "SELECT id FROM users WHERE cms_id = ?", (cms_id,)):
        raise AuthError("This CMS ID is already registered.")
    with db.transaction(conn):
        uid = _create_user(conn, cms_id, password, "student")
    return uid


def create_privileged_user(conn, admin: dict, cms_id: str, password: str, role: str, department_code=None,
                           is_conf_handler=False, is_conf_officer=False, reason: str = "") -> int:
    require_role(conn, admin, "admin")
    if role not in ("staff", "admin", "student"):
        raise AuthError("Invalid role.")
    if role == "staff" and not department_code and not is_conf_officer:
        raise AuthError("Department staff must belong to a department.")
    if not reason.strip():
        raise AuthError("A reason is required for admin actions.")
    if not CMS_RE.match(cms_id or ""):
        raise AuthError("Invalid account ID.")
    if len(password or "") < 6:
        raise AuthError("Password must be at least 6 characters.")
    if db.one(conn, "SELECT id FROM users WHERE cms_id = ?", (cms_id,)):
        raise AuthError("This ID is already registered.")
    with db.transaction(conn):
        uid = _create_user(conn, cms_id, password, role, department_code, None, is_conf_handler, is_conf_officer)
        db.audit(conn, admin, "create_user", f"user:{uid} role:{role} dept:{department_code}", reason)
    return uid


def login(conn, cms_id: str, password: str) -> dict:
    user = db.one(conn, "SELECT * FROM users WHERE cms_id = ? AND active = 1", ((cms_id or "").strip(),))
    if not user or not verify_password(password or "", user["password_hash"]):
        raise AuthError("Invalid CMS ID or password.")
    return public_user(user)


def public_user(user: dict) -> dict:
    return {k: v for k, v in user.items() if k != "password_hash"}


def load_user(conn, user_id: int) -> dict:
    user = db.one(conn, "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,))
    if not user:
        raise PermissionDenied("Account not found or disabled.")
    return public_user(user)


def fresh(conn, actor: dict) -> dict:
    """Re-read the actor from the DB so stale session data can never grant rights."""
    return load_user(conn, actor["id"])


def require_role(conn, actor: dict, *roles: str) -> dict:
    user = fresh(conn, actor)
    if user["role"] not in roles:
        raise PermissionDenied("You are not authorized to perform this action.")
    return user


def assert_issue_access(conn, actor: dict, issue: dict) -> dict:
    """Students may view public issues; staff only their department; admin all."""
    user = fresh(conn, actor)
    if user["role"] == "admin":
        return user
    if user["role"] == "staff":
        if user["department_code"] != issue["department_code"]:
            raise PermissionDenied("Access denied: this issue belongs to another department.")
        return user
    return user  # students: public issue board (non-confidential issues only)


def assert_issue_manage(conn, actor: dict, issue: dict) -> dict:
    user = fresh(conn, actor)
    if user["role"] == "staff" and user["department_code"] == issue["department_code"]:
        return user
    if user["role"] == "admin":
        return user
    raise PermissionDenied("Access denied: only staff of the owning department can manage this issue.")


def reauthenticate(conn, actor: dict, password: str) -> bool:
    row = db.one(conn, "SELECT password_hash FROM users WHERE id = ?", (actor["id"],))
    return bool(row and verify_password(password or "", row["password_hash"]))

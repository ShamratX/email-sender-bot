"""Single-admin login. No new dependencies -- stdlib hashlib/hmac only, so a
VPS that already ran `pip install -r requirements.txt` doesn't need anything
extra for this.

First run: no password is set yet, so /login shows a "set admin password"
form instead of a login form. Once set, that's gone -- only login remains.
Session is a signed cookie (username + expiry, HMAC'd with a random secret
generated once and stored in the settings table), not a full session store --
enough for one operator, one browser.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from . import db

COOKIE_NAME = "session"
SESSION_SECONDS = 30 * 24 * 3600  # 30 days


def _secret() -> bytes:
    key = db.get_setting("session_secret")
    if not key:
        key = os.urandom(32).hex()
        db.set_setting("session_secret", key)
    return bytes.fromhex(key)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + ":" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return hmac.compare_digest(expected.hex(), digest_hex)


def admin_configured() -> bool:
    return bool(db.get_setting("admin_username") and db.get_setting("admin_password_hash"))


def set_admin(username: str, password: str) -> None:
    db.set_setting("admin_username", username.strip())
    db.set_setting("admin_password_hash", hash_password(password))


def check_login(username: str, password: str) -> bool:
    stored_user = db.get_setting("admin_username") or ""
    stored_hash = db.get_setting("admin_password_hash") or ""
    if not hmac.compare_digest(username.strip(), stored_user):
        return False
    return verify_password(password, stored_hash)


def make_session_cookie(username: str) -> str:
    expires = int(time.time()) + SESSION_SECONDS
    payload = f"{username}:{expires}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_session_cookie(value: str | None) -> bool:
    if not value:
        return False
    parts = value.split(":")
    if len(parts) != 3:
        return False
    username, expires_str, sig = parts
    payload = f"{username}:{expires_str}"
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        return int(expires_str) > int(time.time())
    except ValueError:
        return False

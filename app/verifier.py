"""Pre-send email verification. Free, no third-party API key.

Two checks, cheapest first:
  1. Syntax -- same pattern importer.py already uses to reject rows on import.
  2. MX record -- does the domain have a mail server at all? Catches typo'd
     domains (gmial.com), made-up domains, and domains with no mail setup.

This does NOT catch a made-up mailbox on a real, correctly-configured domain
(alice-made-up@gmail.com passes MX but may not exist) -- that needs an SMTP
handshake or a paid verification service like ZeroBounce, which this is not.
It catches the cheap, common cases for free, before a single email is sent.

Results are cached in the verification table and reused for cache_days.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db
from .importer import EMAIL_RE

CACHE_DAYS = 30


def check_syntax(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip().lower()))


def check_mx(domain: str) -> tuple[bool, str]:
    """Returns (has_mx, detail). Falls back to an A-record check: some small
    domains route mail without a dedicated MX record, which is valid."""
    try:
        import dns.resolver  # lazy: only needed when verification actually runs
    except ImportError:
        return True, "dnspython not installed; MX check skipped"

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        if len(answers) > 0:
            return True, f"{len(answers)} MX record(s)"
    except dns.resolver.NXDOMAIN:
        return False, "domain does not exist"
    except dns.resolver.NoAnswer:
        pass  # no MX record; some domains still accept mail via A record
    except Exception as exc:
        return True, f"MX lookup failed, allowing through: {exc}"  # never block on our own network hiccup

    try:
        dns.resolver.resolve(domain, "A", lifetime=8)
        return True, "no MX, but domain has an A record"
    except dns.resolver.NXDOMAIN:
        return False, "domain does not exist"
    except Exception:
        return False, "no MX and no A record"


def verify(email: str) -> str:
    """Checks one address and caches the result. Returns the status string."""
    email = email.strip().lower()
    if not check_syntax(email):
        _save(email, "invalid_syntax", "failed pattern check")
        return "invalid_syntax"

    domain = email.rsplit("@", 1)[-1]
    ok, detail = check_mx(domain)
    status = "valid" if ok else "no_mx"
    _save(email, status, detail)
    return status


def _save(email: str, status: str, detail: str) -> None:
    db.connect().execute(
        "INSERT INTO verification (email, status, checked_at, detail) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(email) DO UPDATE SET status=excluded.status, checked_at=excluded.checked_at, "
        "detail=excluded.detail",
        (email, status, db.utcnow(), detail),
    )


def cached_status(email: str) -> str | None:
    """The most recent result, if still within cache_days. None if never checked
    or stale -- both mean "verify it now" to the caller."""
    row = db.connect().execute(
        "SELECT status, checked_at FROM verification WHERE email=?", (email.strip().lower(),)
    ).fetchone()
    if row is None:
        return None
    checked = db.parse_utc(row["checked_at"])
    if datetime.now(timezone.utc) - checked > timedelta(days=CACHE_DAYS):
        return None
    return row["status"]


def ensure_verified(email: str) -> str:
    """What the send path calls: use the cache, or verify now if stale/missing."""
    cached = cached_status(email)
    if cached is not None:
        return cached
    return verify(email)


def verify_list(list_id: int, limit: int | None = None) -> dict:
    """Batch-verify every lead in a list that has no fresh cached result."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT l.email FROM leads l JOIN lead_list_members m ON m.lead_id = l.id "
        "WHERE m.list_id=?",
        (list_id,),
    ).fetchall()
    counts = {"valid": 0, "invalid_syntax": 0, "no_mx": 0, "skipped_cached": 0}
    checked = 0
    for row in rows:
        if limit is not None and checked >= limit:
            break
        if cached_status(row["email"]) is not None:
            counts["skipped_cached"] += 1
            continue
        status = verify(row["email"])
        counts[status] = counts.get(status, 0) + 1
        checked += 1
    return counts

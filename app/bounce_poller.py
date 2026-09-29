"""Check Resend for the delivery status of recent sends.

There is no webhook here (this app runs on a laptop, not a public server), so
this polls instead. It looks at every 'sent' row from the last 3 days that
has not been checked in the last 10 minutes, asks Resend what happened to it,
and permanently suppresses the address on a hard bounce or a complaint.

Only 'bounced' and 'complained' cause suppression. Everything else (sent,
delivered, delivery_delayed, opened, clicked) is informational only and is
just written to last_status for the lead-detail page to show.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db

LOOKBACK_DAYS = 3
RECHECK_MINUTES = 10
SUPPRESS_ON = {"bounced", "complained"}


def poll(client, limit: int = 25) -> int:
    """One pass. Returns how many sends were checked. Safe to call repeatedly."""
    if not hasattr(client, "get_status"):
        return 0  # fake mode with no status support, or a stub client

    conn = db.connect()
    cutoff_age = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat(
        timespec="seconds"
    )
    cutoff_recheck = (
        datetime.now(timezone.utc) - timedelta(minutes=RECHECK_MINUTES)
    ).isoformat(timespec="seconds")

    rows = conn.execute(
        "SELECT s.id, s.resend_message_id, l.email, s.inbox_id FROM sends s "
        "JOIN leads l ON l.id = s.lead_id "
        "WHERE s.state='sent' AND s.resend_message_id IS NOT NULL "
        "AND s.sent_at >= ? "
        "AND (s.status_checked_at IS NULL OR s.status_checked_at < ?) "
        "ORDER BY s.sent_at DESC LIMIT ?",
        (cutoff_age, cutoff_recheck, limit),
    ).fetchall()

    checked = 0
    for row in rows:
        # each send's own inbox may belong to a different Resend account
        # (multi-domain setups) -- check status with that account's key
        status = client.get_status(row["resend_message_id"], api_key=db.inbox_api_key(row["inbox_id"]))
        checked += 1
        conn.execute(
            "UPDATE sends SET last_status=?, status_checked_at=? WHERE id=?",
            (status, db.utcnow(), row["id"]),
        )
        if status in SUPPRESS_ON:
            newly = db.suppress(row["email"], status, detail=f"resend status: {status}")
            if newly:
                db.log_event("auto_suppressed", row["email"], {"reason": status})
    return checked

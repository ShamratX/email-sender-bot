"""Daily send limits and inbox rotation.

Two numbers per inbox, both settable by hand and both optional to override:
how many NEW (first-contact) emails it may send today, and how many
FOLLOW-UPS -- kept separate so a backlog of due follow-ups can't crowd out
new outreach from the same inbox, or vice versa. An inbox with nothing typed
in for either just uses the global default on the Sending limits card.
"""
from __future__ import annotations

from datetime import date

from . import clock, db


def default_send_limit() -> int:
    return int(db.get_setting("default_daily_send_limit") or 35)


def default_followup_limit() -> int | None:
    raw = db.get_setting("default_daily_followup_limit")
    return int(raw) if raw and raw.strip() else None


def send_limit_for_inbox(inbox) -> int:
    if inbox["daily_limit"] is not None:
        return int(inbox["daily_limit"])
    return default_send_limit()


def followup_limit_for_inbox(inbox) -> int | None:
    """None means no limit -- follow-ups from this inbox are only bounded by
    whatever's due, not by a daily number."""
    if inbox["daily_followup_limit"] is not None:
        return int(inbox["daily_followup_limit"])
    return default_followup_limit()


def sent_today(inbox_id: int | None = None, day: str | None = None, followups_only: bool | None = None) -> int:
    """followups_only=None counts everything, True counts only step>0,
    False counts only step=0 (new/first-contact sends)."""
    day = day or clock.today()
    conn = db.connect()
    clauses = ["state='sent'", "sent_day=?"]
    params: list = [day]
    if inbox_id is not None:
        clauses.append("inbox_id=?")
        params.append(inbox_id)
    if followups_only is True:
        clauses.append("step>0")
    elif followups_only is False:
        clauses.append("step=0")
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM sends WHERE {' AND '.join(clauses)}", params
    ).fetchone()
    return int(row["n"])


def campaign_sent_today(campaign_id: int, day: str | None = None) -> int:
    row = db.connect().execute(
        "SELECT COUNT(*) AS n FROM sends WHERE state='sent' AND sent_day=? AND campaign_id=?",
        (day or clock.today(), campaign_id),
    ).fetchone()
    return int(row["n"])


def enabled_inboxes() -> list:
    return db.connect().execute(
        "SELECT * FROM inboxes WHERE enabled=1 ORDER BY id"
    ).fetchall()


def quota_report(day: str | None = None) -> dict:
    """Per-inbox overview for the Dashboard: new and follow-up usage side by
    side, since they're now separate budgets rather than one shared number."""
    day = day or clock.today()
    rows = []
    for inbox in enabled_inboxes():
        send_limit = send_limit_for_inbox(inbox)
        followup_limit = followup_limit_for_inbox(inbox)
        send_used = sent_today(inbox["id"], day, followups_only=False)
        followup_used = sent_today(inbox["id"], day, followups_only=True)
        rows.append(
            {
                "id": inbox["id"],
                "email": inbox["email"],
                "name": inbox["name"],
                "send_used": send_used,
                "send_limit": send_limit,
                "followup_used": followup_used,
                "followup_limit": followup_limit,
            }
        )
    return {"day": day, "inboxes": rows}


def pick_inbox(day: str | None = None, allowed_ids: set[int] | None = None, is_followup: bool = False):
    """Round-robin over inboxes with quota left for this send TYPE (new vs
    follow-up), each checked against that inbox's own limit for that type.

    allowed_ids restricts the pool to a specific campaign's chosen inboxes.
    The rotation cursor is tracked per-pool and per type, so a campaign's new
    sends and its follow-ups don't fight over one shared cursor, and two
    campaigns with different pools don't fight over one shared cursor either.
    Persisted so restarting the app doesn't reset it to the first inbox.
    """
    day = day or clock.today()
    inboxes = enabled_inboxes()
    pool_key = "-".join(str(i) for i in sorted(allowed_ids)) if allowed_ids is not None else "all"
    cursor_key = f"rotation_cursor_{'followup' if is_followup else 'new'}_{pool_key}"
    if allowed_ids is not None:
        inboxes = [i for i in inboxes if i["id"] in allowed_ids]

    if not inboxes:
        return None, "no enabled inboxes available to this campaign"

    cursor = int(db.get_setting(cursor_key) or 0)
    count = len(inboxes)
    for offset in range(count):
        index = (cursor + offset) % count
        inbox = inboxes[index]
        if is_followup:
            limit = followup_limit_for_inbox(inbox)
            if limit is not None and sent_today(inbox["id"], day, followups_only=True) >= limit:
                continue
        else:
            limit = send_limit_for_inbox(inbox)
            if sent_today(inbox["id"], day, followups_only=False) >= limit:
                continue
        db.set_setting(cursor_key, (index + 1) % count)
        return inbox, None
    reason = "every inbox has reached today's follow-up limit" if is_followup else "every inbox has reached today's send limit"
    return None, reason

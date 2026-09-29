"""Daily limits and inbox rotation.

Two ceilings, both enforced on every send: per inbox per day, and per account
per day. Each Resend Account (domain) can have its own independent warm-up
clock, schedule and daily cap -- a brand-new second domain must not inherit
an older domain's week number or plan limit. An inbox with no Account
assigned uses the global/default settings, unchanged from the single-domain
setup this app started with.
"""
from __future__ import annotations

import json
from datetime import date

from . import clock, db


def _resolve_account_config(account_id: int | None) -> tuple[str, dict, int]:
    """Returns (warmup_start_date, warmup_schedule, daily_cap) for one account,
    falling back to the global Settings values for anything that account
    leaves blank -- and for account_id=None (the default/no-account pool)."""
    if account_id:
        row = db.connect().execute(
            "SELECT warmup_start_date, warmup_schedule, daily_cap FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if row:
            start = row["warmup_start_date"] or db.get_setting("warmup_start_date") or ""
            schedule_raw = row["warmup_schedule"] or db.get_setting("warmup_schedule") or "{}"
            cap = (
                row["daily_cap"]
                if row["daily_cap"] is not None
                else int(db.get_setting("account_daily_cap") or 100)
            )
            return start, json.loads(schedule_raw), cap
    return (
        db.get_setting("warmup_start_date") or "",
        json.loads(db.get_setting("warmup_schedule") or "{}"),
        int(db.get_setting("account_daily_cap") or 100),
    )


def warmup_week_for(account_id: int | None, today: date | None = None) -> int:
    """Derived from that account's own start date, not typed in by hand."""
    start_raw, _, _ = _resolve_account_config(account_id)
    if not start_raw:
        return 99  # warm-up not being tracked: treat as matured
    start = date.fromisoformat(start_raw)
    current = today or clock.now_local().date()
    days = (current - start).days
    if days < 0:
        return 1
    return days // 7 + 1


def per_inbox_limit_for(account_id: int | None, today: date | None = None) -> int:
    _, schedule, _ = _resolve_account_config(account_id)
    week = warmup_week_for(account_id, today)
    key = str(week)
    if key in schedule:
        return int(schedule[key])
    return int(schedule.get("4+", 35))


def account_cap_for(account_id: int | None) -> int:
    _, _, cap = _resolve_account_config(account_id)
    return cap


# Backward-compatible aliases: the global/default account (account_id=None),
# used by the dashboard's overall summary and anywhere that predates
# multi-account support.
def warmup_week(today: date | None = None) -> int:
    return warmup_week_for(None, today)


def per_inbox_limit(today: date | None = None) -> int:
    return per_inbox_limit_for(None, today)


def account_cap() -> int:
    return account_cap_for(None)


def sent_today(inbox_id: int | None = None, day: str | None = None) -> int:
    day = day or clock.today()
    conn = db.connect()
    if inbox_id is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sends WHERE state='sent' AND sent_day=?", (day,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sends WHERE state='sent' AND sent_day=? AND inbox_id=?",
            (day, inbox_id),
        ).fetchone()
    return int(row["n"])


def account_sent_today(account_id: int | None, day: str | None = None) -> int:
    """How much THIS account's inboxes (not the whole app) have sent today --
    what its own daily_cap is actually checked against."""
    day = day or clock.today()
    conn = db.connect()
    if account_id:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sends s JOIN inboxes i ON i.id = s.inbox_id "
            "WHERE s.state='sent' AND s.sent_day=? AND i.account_id=?",
            (day, account_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sends s JOIN inboxes i ON i.id = s.inbox_id "
            "WHERE s.state='sent' AND s.sent_day=? AND i.account_id IS NULL",
            (day,),
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
    """Global/default overview for the main Dashboard. Each inbox row shows
    ITS OWN account's limit (correct even when accounts differ); the overall
    total row is the default/no-account bucket, unchanged from before --
    see a campaign's own dashboard for that campaign's actual account total."""
    day = day or clock.today()
    rows = []
    for inbox in enabled_inboxes():
        account_id = inbox["account_id"]
        limit = per_inbox_limit_for(account_id, None)
        used = sent_today(inbox["id"], day)
        rows.append(
            {
                "id": inbox["id"],
                "email": inbox["email"],
                "name": inbox["name"],
                "account_id": account_id,
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used),
            }
        )
    total_used = sent_today(None, day)
    cap = account_cap()
    return {
        "day": day,
        "week": warmup_week(),
        "per_inbox_limit": per_inbox_limit(),
        "inboxes": rows,
        "account_used": total_used,
        "account_cap": cap,
        "account_remaining": max(0, cap - total_used),
    }


def pick_inbox(day: str | None = None, allowed_ids: set[int] | None = None):
    """Round-robin over inboxes with quota left.

    allowed_ids restricts the pool to a specific campaign's chosen inboxes
    (e.g. only the ones on its own domain/Resend account). Each candidate
    inbox is checked against ITS OWN account's per-inbox limit and daily cap
    -- a pool that spans two accounts enforces both independently, not one
    shared number. The rotation cursor is tracked per-pool (keyed by the
    sorted id set) so two campaigns with different pools don't fight over one
    shared cursor.

    The cursor is persisted so restarting the app does not reset it to the
    first inbox and quietly overload it.
    """
    day = day or clock.today()
    inboxes = enabled_inboxes()
    if allowed_ids is not None:
        inboxes = [i for i in inboxes if i["id"] in allowed_ids]
        cursor_key = "rotation_cursor_" + "-".join(str(i) for i in sorted(allowed_ids))
    else:
        cursor_key = "rotation_cursor"

    if not inboxes:
        return None, "no enabled inboxes available to this campaign"

    cursor = int(db.get_setting(cursor_key) or 0)
    count = len(inboxes)
    account_blocked = 0
    for offset in range(count):
        index = (cursor + offset) % count
        inbox = inboxes[index]
        account_id = inbox["account_id"]
        if account_sent_today(account_id, day) >= account_cap_for(account_id):
            account_blocked += 1
            continue  # this inbox's account is maxed out today -- try the next inbox
        limit = per_inbox_limit_for(account_id, None)
        if sent_today(inbox["id"], day) < limit:
            db.set_setting(cursor_key, (index + 1) % count)
            return inbox, None
    if account_blocked == count:
        return None, "account daily cap reached"
    return None, "every inbox has reached today's warm-up limit"

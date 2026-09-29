"""Who gets an email next, and the send itself.

The order of checks in `blocked_reason` and the reserve -> send -> settle
sequence in `send_one` are the safety core of this app. Read section 6 of
BUILD_PLAN.md before changing either.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import clock, db, templating, verifier, warmup
from .resend_client import SendError


COUNTRY_FIELD_NAMES = ("country", "Country", "COUNTRY")


def _lead_country(lead) -> str:
    """Reads the country value from whichever casing the sheet actually used."""
    fields = json.loads(lead["custom_fields"])
    for name in COUNTRY_FIELD_NAMES:
        if name in fields and fields[name].strip():
            return fields[name].strip()
    return ""


def campaign_steps(campaign_id: int) -> list:
    return db.connect().execute(
        "SELECT * FROM campaign_steps WHERE campaign_id=? ORDER BY step", (campaign_id,)
    ).fetchall()


def template_with_subjects(template_id: int) -> tuple[dict, list[str]]:
    conn = db.connect()
    template = conn.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
    variants = [
        row["subject"]
        for row in conn.execute(
            "SELECT subject FROM subject_variants WHERE template_id=? ORDER BY id",
            (template_id,),
        ).fetchall()
    ]
    return template, variants


def candidates(campaign_id: int, now: datetime | None = None, include_manual: bool = False) -> list[dict]:
    """Every (lead, step) pair that is eligible right now, earliest step first.

    A follow-up is due only when the PREVIOUS step was actually sent and the gap
    has elapsed since that send -- measured from the real send time, so a lead
    imported late still gets correct spacing.
    """
    conn = db.connect()
    campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if campaign is None:
        return []
    now = now or datetime.now(timezone.utc)
    gap = timedelta(days=int(campaign["gap_days"]))
    steps = campaign_steps(campaign_id)
    followups_allowed = (
        campaign["followup_mode"] == "auto"
        or campaign["followups_released"] == 1
        or include_manual
    )

    leads = conn.execute(
        "SELECT l.* FROM leads l JOIN lead_list_members m ON m.lead_id = l.id "
        "WHERE m.list_id = ? "
        "AND l.email NOT IN (SELECT email FROM suppression) ORDER BY l.id",
        (campaign["list_id"],),
    ).fetchall()
    if campaign["country_filter"]:
        wanted = campaign["country_filter"].strip().upper()
        leads = [
            lead for lead in leads
            if _lead_country(lead).upper() == wanted
        ]
    windowed = {c.strip().upper() for c in (campaign["windowed_countries"] or "").split(",") if c.strip()}
    if windowed:
        # Per-campaign choice of which countries are time-gated. A lead whose
        # country is in this set is only eligible during THAT country's own
        # window, checked in THAT country's own timezone -- no manual
        # conversion needed. A lead whose country is NOT in this set (whether
        # unselected or simply unknown) sends anytime, same as before.
        windows = json.loads(db.get_setting("country_windows") or "{}")
        tzs = json.loads(db.get_setting("country_timezones") or "{}")
        leads = [
            lead for lead in leads
            if _lead_country(lead).upper() not in windowed
            or clock.in_window_tz(
                *windows.get(_lead_country(lead).upper(), ["00:00", "23:59"]),
                tzs.get(_lead_country(lead).upper(), "UTC"),
                now,
            )
        ]

    sends = conn.execute(
        "SELECT lead_id, step, state, sent_at FROM sends WHERE campaign_id=?", (campaign_id,)
    ).fetchall()
    by_lead: dict[int, dict[int, dict]] = {}
    for row in sends:
        by_lead.setdefault(row["lead_id"], {})[row["step"]] = row

    out: list[dict] = []
    for lead in leads:
        history = by_lead.get(lead["id"], {})
        for step_row in steps:
            step = int(step_row["step"])
            if step in history:
                continue  # already handled, whatever its state
            if step == 0:
                out.append({"lead": lead, "step": step, "template_id": step_row["template_id"]})
            else:
                previous = history.get(step - 1)
                if previous is None or previous["state"] != "sent" or not previous["sent_at"]:
                    break
                due_at = db.parse_utc(previous["sent_at"]) + gap
                if now < due_at:
                    break
                if not followups_allowed:
                    break
                out.append({"lead": lead, "step": step, "template_id": step_row["template_id"]})
            break  # one step per lead per pass
    return out


def campaign_progress(campaign_id: int) -> dict:
    """Pending / sent / failed counts for one campaign, for the dashboard.

    'Pending' is leads in the campaign's list (matching its country filter and
    not suppressed) who have not yet received the initial email -- the count
    of people still waiting on a first contact. 'Sent' and 'failed' are
    cumulative across every step (initial + follow-ups) for this campaign.
    """
    conn = db.connect()
    campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if campaign is None:
        return {"pending": 0, "sent": 0, "failed": 0}

    leads = conn.execute(
        "SELECT l.* FROM leads l JOIN lead_list_members m ON m.lead_id = l.id "
        "WHERE m.list_id = ? AND l.email NOT IN (SELECT email FROM suppression)",
        (campaign["list_id"],),
    ).fetchall()
    if campaign["country_filter"]:
        wanted = campaign["country_filter"].strip().upper()
        leads = [lead for lead in leads if _lead_country(lead).upper() == wanted]

    step0_sent = {
        row["lead_id"]
        for row in conn.execute(
            "SELECT lead_id FROM sends WHERE campaign_id=? AND step=0 AND state='sent'",
            (campaign_id,),
        ).fetchall()
    }
    pending = sum(1 for lead in leads if lead["id"] not in step0_sent)

    sent = conn.execute(
        "SELECT COUNT(*) n FROM sends WHERE campaign_id=? AND state='sent'", (campaign_id,)
    ).fetchone()["n"]
    failed = conn.execute(
        "SELECT COUNT(*) n FROM sends WHERE campaign_id=? AND state IN ('failed','unknown')",
        (campaign_id,),
    ).fetchone()["n"]
    return {"pending": pending, "sent": sent, "failed": failed}


def due_followups(now: datetime | None = None) -> list[dict]:
    """Everything waiting on a follow-up, for the UI -- regardless of auto/manual."""
    conn = db.connect()
    rows = []
    for campaign in conn.execute(
        "SELECT * FROM campaigns WHERE state IN ('running','paused')"
    ).fetchall():
        for item in candidates(campaign["id"], now=now, include_manual=True):
            if item["step"] == 0:
                continue
            previous = conn.execute(
                "SELECT sent_at FROM sends WHERE campaign_id=? AND lead_id=? AND step=?",
                (campaign["id"], item["lead"]["id"], item["step"] - 1),
            ).fetchone()
            sent_at = db.parse_utc(previous["sent_at"]) if previous and previous["sent_at"] else None
            rows.append(
                {
                    "campaign": campaign["name"],
                    "campaign_id": campaign["id"],
                    "email": item["lead"]["email"],
                    "step": item["step"],
                    "previous_sent_at": sent_at.isoformat(timespec="minutes") if sent_at else "",
                    "days_since": (datetime.now(timezone.utc) - sent_at).days if sent_at else 0,
                }
            )
    return rows


def blocked_reason(campaign, now: datetime | None = None) -> str | None:
    """Checks that stop a whole pass, in order of severity."""
    if db.get_setting("sending_enabled") != "1":
        return "sending is stopped"
    if campaign["state"] != "running":
        return f"campaign is {campaign['state']}"
    if db.get_setting("skip_weekends") == "1" and clock.now_local(now).weekday() >= 5:
        # Monday=0 ... Saturday=5, Sunday=6. Checked in the operator's own
        # timezone (Settings -> timezone), the same clock used everywhere
        # else in the app -- applies to every campaign, including ones using
        # per-country windows, since "no weekend sends" is a blanket rule.
        return "weekend -- sending paused Saturday and Sunday"
    if not (campaign["windowed_countries"] or "").strip() and not clock.in_window(
        campaign["window_start"], campaign["window_end"], now
    ):
        return (
            f"outside send window {campaign['window_start']}-{campaign['window_end']}"
        )
    account_id = campaign["account_id"]
    if warmup.account_sent_today(account_id) >= warmup.account_cap_for(account_id):
        return "account daily cap reached"
    if campaign["daily_cap"] and warmup.campaign_sent_today(campaign["id"]) >= campaign["daily_cap"]:
        return "campaign daily cap reached"
    return None


def send_one(campaign, item: dict, client) -> dict:
    """Reserve, send, settle. Never reordered.

    If the process dies between the send and the settle, the row stays
    'reserved'; sweep_stale_reserved later marks it 'unknown' and nothing
    retries it automatically.
    """
    lead = item["lead"]
    if db.is_suppressed(lead["email"]):
        return {"status": "skipped", "reason": "suppressed", "email": lead["email"]}

    if item["step"] > 0:
        cap = warmup.followup_daily_cap()
        if cap is not None and warmup.followups_sent_today() >= cap:
            # "skipped", not "blocked" -- a full follow-up cap must not halt
            # the rest of this campaign's queue, since later items may well
            # be step-0 (new) sends that have their own, separate budget.
            return {"status": "skipped", "reason": "follow-up daily cap reached", "email": lead["email"]}

    if db.get_setting("verify_before_send") == "1":
        status = verifier.ensure_verified(lead["email"])
        if status in ("invalid_syntax", "no_mx"):
            db.suppress(lead["email"], status, detail="failed pre-send verification")
            db.log_event("verification_failed", lead["email"], {"status": status})
            return {"status": "skipped", "reason": status, "email": lead["email"]}

    account_id = campaign["account_id"]
    if account_id:
        # Simple path: one Account chosen, so its inboxes are the pool --
        # no manual checkbox list to keep in sync.
        allowed_ids = {
            row["id"] for row in db.connect().execute(
                "SELECT id FROM inboxes WHERE account_id=?", (account_id,)
            ).fetchall()
        }
    else:
        allowed_raw = (campaign["allowed_inboxes"] or "").strip()
        allowed_ids = {int(x) for x in allowed_raw.split(",") if x.strip()} if allowed_raw else None
    inbox, reason = warmup.pick_inbox(allowed_ids=allowed_ids)
    if inbox is None:
        return {"status": "blocked", "reason": reason}

    try:
        send_id = db.reserve(lead["id"], campaign["id"], item["step"], inbox["id"])
    except db.AlreadyHandled:
        return {"status": "skipped", "reason": "already handled", "email": lead["email"]}

    template, variants = template_with_subjects(item["template_id"])
    subject, body = templating.compose(
        variants, template["body"], lead,
        footer_enabled=bool(template["footer_enabled"]),
        footer_text=template["footer_text"],
    )
    sender = f"{inbox['name']} <{inbox['email']}>"
    headers = {}
    unsubscribe = templating.list_unsubscribe_header()
    if unsubscribe:
        headers["List-Unsubscribe"] = unsubscribe
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    try:
        message_id = client.send(
            sender=sender,
            to=lead["email"],
            subject=subject,
            text=body,
            idempotency_key=db.idempotency_key(lead["id"], campaign["id"], item["step"]),
            headers=headers or None,
            api_key=db.inbox_api_key(inbox["id"]),
        )
    except SendError as exc:
        db.settle_failed(send_id, str(exc))
        db.log_event("send_failed", lead["email"], {"error": str(exc), "step": item["step"]})
        return {"status": "failed", "reason": str(exc), "email": lead["email"]}

    db.settle_sent(send_id, message_id, subject, body, clock.today())
    db.log_event(
        "sent",
        lead["email"],
        {"step": item["step"], "inbox": inbox["email"], "subject": subject},
    )
    return {
        "status": "sent",
        "email": lead["email"],
        "step": item["step"],
        "inbox": inbox["email"],
        "subject": subject,
    }


def run_pass(client, limit: int | None = None, now: datetime | None = None) -> list[dict]:
    """One sweep across running campaigns. Returns what happened, for the UI/tests."""
    conn = db.connect()
    results: list[dict] = []
    db.sweep_stale_reserved()
    for campaign in conn.execute(
        "SELECT * FROM campaigns WHERE state='running' ORDER BY id"
    ).fetchall():
        reason = blocked_reason(campaign, now)
        if reason:
            results.append({"status": "blocked", "campaign": campaign["name"], "reason": reason})
            continue
        queue = candidates(campaign["id"], now=now)
        if not queue and campaign["followups_released"]:
            db.connect().execute(
                "UPDATE campaigns SET followups_released=0 WHERE id=?", (campaign["id"],)
            )
        for item in queue:
            if limit is not None and sum(1 for r in results if r["status"] == "sent") >= limit:
                return results
            if db.get_setting("sending_enabled") != "1":
                results.append({"status": "blocked", "reason": "stopped mid-pass"})
                return results
            outcome = send_one(campaign, item, client)
            outcome["campaign"] = campaign["name"]
            results.append(outcome)
            if outcome["status"] == "blocked":
                break
    return results

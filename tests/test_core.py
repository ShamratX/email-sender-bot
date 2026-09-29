"""Safety tests. These are the ones that decide whether it is safe to send.

Run: python -m pytest -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import clock, db, engine, importer, templating, warmup  # noqa: E402
from app.resend_client import FakeResend  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("SENDER_DB", str(path))
    db.close()
    conn = db.init(path)
    db.set_setting("sending_enabled", "1")
    db.set_setting("verify_before_send", "0")  # keep the core suite network-independent
    yield conn
    db.close()


def add_inbox(email="contact@example.com", name="Test"):
    cur = db.connect().execute(
        "INSERT INTO inboxes (email, name) VALUES (?, ?)", (email, name)
    )
    return int(cur.lastrowid)


def add_lead(email, **fields):
    cur = db.connect().execute(
        "INSERT INTO leads (email, custom_fields, created_at) VALUES (?, ?, ?)",
        (email, json.dumps(fields), db.utcnow()),
    )
    return int(cur.lastrowid)


def add_template(name="t", subjects=("Hello {{business_name}}",), body="Hi {{business_name}}"):
    conn = db.connect()
    cur = conn.execute(
        "INSERT INTO templates (name, body, created_at) VALUES (?, ?, ?)",
        (name, body, db.utcnow()),
    )
    tid = int(cur.lastrowid)
    for subject in subjects:
        conn.execute(
            "INSERT INTO subject_variants (template_id, subject) VALUES (?, ?)", (tid, subject)
        )
    return tid


def build_campaign(lead_emails, steps=1, gap_days=3, followup_mode="auto"):
    conn = db.connect()
    list_id = importer.ensure_list("test-list")
    for email in lead_emails:
        lead_id = add_lead(email, business_name=email.split("@")[0].title())
        conn.execute(
            "INSERT OR IGNORE INTO lead_list_members (list_id, lead_id) VALUES (?, ?)",
            (list_id, lead_id),
        )
    cur = conn.execute(
        "INSERT INTO campaigns (name, list_id, state, gap_days, window_start, window_end, "
        "followup_mode, created_at) VALUES ('c', ?, 'running', ?, '00:00', '23:59', ?, ?)",
        (list_id, gap_days, followup_mode, db.utcnow()),
    )
    campaign_id = int(cur.lastrowid)
    for step in range(steps):
        conn.execute(
            "INSERT INTO campaign_steps (campaign_id, step, template_id) VALUES (?, ?, ?)",
            (campaign_id, step, add_template(name=f"step-{step}")),
        )
    return conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()


# --- 1. the database physically refuses a second send ---------------------

def test_reserving_twice_raises(store):
    inbox = add_inbox()
    lead = add_lead("a@example.com")
    db.reserve(lead, 1, 0, inbox)
    with pytest.raises(db.AlreadyHandled):
        db.reserve(lead, 1, 0, inbox)


def test_reserve_is_per_step(store):
    inbox = add_inbox()
    lead = add_lead("a@example.com")
    db.reserve(lead, 1, 0, inbox)
    db.reserve(lead, 1, 1, inbox)  # a different step is a different slot
    assert db.connect().execute("SELECT COUNT(*) n FROM sends").fetchone()["n"] == 2


# --- 2. a crash mid-send becomes 'unknown', never 'sent' ------------------

def test_stale_reserved_becomes_unknown(store):
    inbox = add_inbox()
    lead = add_lead("a@example.com")
    send_id = db.reserve(lead, 1, 0, inbox)
    db.connect().execute(
        "UPDATE sends SET reserved_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds"), send_id),
    )
    assert db.sweep_stale_reserved() == 1
    row = db.connect().execute("SELECT state FROM sends WHERE id=?", (send_id,)).fetchone()
    assert row["state"] == "unknown"


def test_fresh_reserved_is_left_alone(store):
    inbox = add_inbox()
    db.reserve(add_lead("a@example.com"), 1, 0, inbox)
    assert db.sweep_stale_reserved() == 0


# --- 3. sending limits -----------------------------------------------------

def test_default_send_limit_used_when_inbox_has_no_override(store):
    add_inbox()
    db.set_setting("default_daily_send_limit", "5")
    assert warmup.quota_report()["inboxes"][0]["send_limit"] == 5


def test_per_inbox_override_wins_over_default(store):
    inbox_id = add_inbox()
    db.set_setting("default_daily_send_limit", "5")
    db.connect().execute("UPDATE inboxes SET daily_limit=20 WHERE id=?", (inbox_id,))
    assert warmup.quota_report()["inboxes"][0]["send_limit"] == 20


def test_followup_limit_is_separate_from_send_limit(store):
    """A follow-up limit reached on an inbox must not stop that same inbox's
    new-send budget, and vice versa -- they're two independent numbers."""
    inbox_id = add_inbox()
    db.connect().execute(
        "UPDATE inboxes SET daily_limit=10, daily_followup_limit=1 WHERE id=?", (inbox_id,)
    )
    today = clock.today()
    # use up the inbox's one follow-up slot
    lead = add_lead("f@example.com")
    send_id = db.reserve(lead, 1, 1, inbox_id)
    db.settle_sent(send_id, "m", "s", "b", today)

    inbox, reason = warmup.pick_inbox(is_followup=True)
    assert inbox is None and reason == "every inbox has reached today's follow-up limit"

    # new-send budget (10/day) is untouched by the follow-up cap being hit
    inbox, reason = warmup.pick_inbox(is_followup=False)
    assert inbox is not None and inbox["id"] == inbox_id


def test_followup_limit_blank_means_unlimited(store):
    inbox_id = add_inbox()
    for i in range(50):
        lead = add_lead(f"f{i}@example.com")
        send_id = db.reserve(lead, 1, 1, inbox_id)
        db.settle_sent(send_id, "m", "s", "b", clock.today())
    inbox, reason = warmup.pick_inbox(is_followup=True)
    assert inbox is not None  # no cap set anywhere -- 50 sent today doesn't block a 51st


def test_rotation_cursor_persists(store):
    add_inbox("one@example.com", "One")
    add_inbox("two@example.com", "Two")
    first, _ = warmup.pick_inbox()
    second, _ = warmup.pick_inbox()
    assert first["email"] != second["email"]
    third, _ = warmup.pick_inbox()
    assert third["email"] == first["email"]  # wrapped back round


# --- 4. import is append only --------------------------------------------

CSV = b"Business Name,Email,City\nAcme Ltd,ACME@example.com ,Dhaka\nBeta,beta@example.com,Sylhet\n"


def test_import_adds_and_reimport_is_a_no_op(store):
    sheet = importer.parse(CSV, "leads.csv")
    column = importer.guess_email_column(sheet.columns)
    assert column == "Email"
    first = importer.commit(sheet, column, "list-a")
    assert (first.new, first.existing) == (2, 0)

    again = importer.commit(importer.parse(CSV, "leads.csv"), column, "list-a")
    assert (again.new, again.existing, again.updated) == (0, 2, 0)
    assert db.connect().execute("SELECT COUNT(*) n FROM leads").fetchone()["n"] == 2


def test_import_never_touches_send_history(store):
    sheet = importer.parse(CSV, "leads.csv")
    importer.commit(sheet, "Email", "list-a")
    lead = db.connect().execute("SELECT id FROM leads WHERE email='acme@example.com'").fetchone()
    inbox = add_inbox()
    send_id = db.reserve(lead["id"], 1, 0, inbox)
    db.settle_sent(send_id, "m", "s", "b", clock.today())

    importer.commit(importer.parse(CSV, "leads.csv"), "Email", "list-a", on_existing="update")
    row = db.connect().execute("SELECT state FROM sends WHERE id=?", (send_id,)).fetchone()
    assert row["state"] == "sent"


def test_import_keeps_suppression(store):
    db.suppress("beta@example.com", "unsubscribe")
    stats = importer.commit(importer.parse(CSV, "leads.csv"), "Email", "list-a")
    assert stats.suppressed == 1
    assert db.is_suppressed("beta@example.com")


def test_import_counts_invalid_and_duplicates(store):
    raw = b"Email\ngood@example.com\nnot-an-email\ngood@example.com\n\n"
    stats, _ = importer.preview(importer.parse(raw, "x.csv"), "Email")
    assert stats.new == 1
    assert stats.invalid == 1  # csv skips the trailing blank line; the bad address counts
    assert stats.duplicate_in_file == 1


# --- 5. follow-up timing --------------------------------------------------

def test_followup_due_only_after_the_gap(store):
    add_inbox()
    campaign = build_campaign(["a@example.com"], steps=2, gap_days=3)
    client = FakeResend()
    engine.run_pass(client)
    assert client.recipients == ["a@example.com"]

    # two days later: not due
    two_days = datetime.now(timezone.utc) + timedelta(days=2)
    assert engine.candidates(campaign["id"], now=two_days) == []

    # three days later: due
    three_days = datetime.now(timezone.utc) + timedelta(days=3, minutes=1)
    due = engine.candidates(campaign["id"], now=three_days)
    assert len(due) == 1 and due[0]["step"] == 1


def test_no_followup_after_a_reply(store):
    add_inbox()
    campaign = build_campaign(["a@example.com"], steps=2, gap_days=3)
    engine.run_pass(FakeResend())
    db.suppress("a@example.com", "replied")
    later = datetime.now(timezone.utc) + timedelta(days=10)
    assert engine.candidates(campaign["id"], now=later) == []


def test_manual_mode_holds_followups_until_released(store):
    add_inbox()
    campaign = build_campaign(["a@example.com"], steps=2, gap_days=3, followup_mode="manual")
    engine.run_pass(FakeResend())
    later = datetime.now(timezone.utc) + timedelta(days=4)
    assert engine.candidates(campaign["id"], now=later) == []
    assert len(engine.due_followups(now=later)) == 1  # visible in the UI

    db.connect().execute("UPDATE campaigns SET followups_released=1 WHERE id=?", (campaign["id"],))
    assert len(engine.candidates(campaign["id"], now=later)) == 1


# --- 6. a full simulated campaign sends each address exactly once ---------

def test_fifty_leads_no_duplicates(store):
    for i in range(4):
        add_inbox(f"i{i}@example.com", f"I{i}")
    emails = [f"lead{i}@example.com" for i in range(50)]
    build_campaign(emails, steps=1)

    client = FakeResend()
    for _ in range(10):
        engine.run_pass(client)

    assert len(client.recipients) == len(set(client.recipients))
    assert len(client.recipients) == 50
    keys = [c["idempotency_key"] for c in client.calls]
    assert len(keys) == len(set(keys))


def test_warmup_limit_stops_the_run(store):
    add_inbox()
    db.set_setting("default_daily_send_limit", "6")
    build_campaign([f"lead{i}@example.com" for i in range(20)], steps=1)
    client = FakeResend()
    for _ in range(10):
        engine.run_pass(client)
    assert len(client.calls) == 6


# --- 7. crash mid-run: no duplicate, exactly one unknown -----------------

def test_crash_between_send_and_settle(store):
    add_inbox()
    campaign = build_campaign(["a@example.com", "b@example.com"], steps=1)
    client = FakeResend()

    class Crash(Exception):
        pass

    class CrashingClient(FakeResend):
        def send(self, **kwargs):
            message_id = super().send(**kwargs)
            raise Crash("power cut after the email left")

    crashing = CrashingClient()
    with pytest.raises(Crash):
        engine.run_pass(crashing)

    # the email left, the row is still 'reserved'
    row = db.connect().execute("SELECT * FROM sends").fetchone()
    assert row["state"] == "reserved"

    # restart: the sweep marks it unknown and nothing retries it
    db.connect().execute(
        "UPDATE sends SET reserved_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds"), row["id"]),
    )
    db.sweep_stale_reserved()
    engine.run_pass(client)

    assert crashing.recipients[0] not in client.recipients  # never sent twice
    states = [r["state"] for r in db.connect().execute("SELECT state FROM sends").fetchall()]
    assert states.count("unknown") == 1


# --- 8. suppression and the stop switch ----------------------------------

def test_suppressed_address_is_never_sent(store):
    add_inbox()
    build_campaign(["a@example.com", "b@example.com"], steps=1)
    db.suppress("a@example.com", "hard_bounce")
    client = FakeResend()
    engine.run_pass(client)
    engine.run_pass(client)
    assert client.recipients == ["b@example.com"]


def test_stop_switch_blocks_everything(store):
    add_inbox()
    build_campaign(["a@example.com"], steps=1)
    db.set_setting("sending_enabled", "0")
    client = FakeResend()
    results = engine.run_pass(client)
    assert client.calls == []
    assert results[0]["reason"] == "sending is stopped"


def test_outside_window_blocks_the_pass(store):
    add_inbox()
    campaign = build_campaign(["a@example.com"], steps=1)
    db.connect().execute(
        "UPDATE campaigns SET window_start='03:00', window_end='03:01' WHERE id=?",
        (campaign["id"],),
    )
    client = FakeResend()
    results = engine.run_pass(client, now=datetime.now(timezone.utc))
    assert client.calls == []
    assert "outside send window" in results[0]["reason"]


# --- 9. templates ---------------------------------------------------------

def test_missing_variable_blocks_save(store):
    with pytest.raises(templating.TemplateError):
        templating.validate(["Hi {{first_name}}"], "body", available=["business_name"])
    templating.validate(["Hi {{business_name}}"], "body", available=["business_name"])


def test_render_and_footer(store):
    db.set_setting("unsubscribe_email", "unsubscribe@example.com")
    db.set_setting("postal_address", "1 Test Road, Dhaka")
    lead = {"email": "a@example.com", "custom_fields": json.dumps({"business_name": "Acme"})}
    subject, body = templating.compose(["Hello {{business_name}}"], "Hi {{business_name}}.", lead)
    assert subject == "Hello Acme"
    assert body.startswith("Hi Acme.")
    assert "UNSUBSCRIBE" in body
    assert "1 Test Road, Dhaka" in body


def test_subject_variants_are_stable_and_spread(store):
    variants = ["A", "B", "C"]
    picks = {templating.pick_subject(variants, f"lead{i}@example.com") for i in range(30)}
    assert picks == {"A", "B", "C"}
    once = templating.pick_subject(variants, "same@example.com")
    assert once == templating.pick_subject(variants, "same@example.com")

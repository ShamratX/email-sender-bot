"""Pre-send verification: syntax, MX lookup, caching, and the send-path skip.

The MX-lookup tests hit real DNS. If you're offline, they'll fail on that
specific check, not on the app logic -- that's expected, not a bug.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, engine, verifier  # noqa: E402
from app.resend_client import FakeResend  # noqa: E402
from tests.test_core import add_inbox, add_lead, build_campaign, store  # noqa: E402,F401


def test_bad_syntax_is_rejected_without_network(store):
    assert verifier.check_syntax("not-an-email") is False
    assert verifier.check_syntax("a@b.com") is True


def test_real_domain_has_mx(store):
    ok, detail = verifier.check_mx("gmail.com")
    assert ok is True, detail


def test_made_up_domain_has_no_mx(store):
    ok, detail = verifier.check_mx("this-domain-does-not-exist-abc123xyz.com")
    assert ok is False, detail


def test_verify_caches_the_result(store):
    verifier.verify("someone@gmail.com")
    assert verifier.cached_status("someone@gmail.com") == "valid"
    # ensure_verified must not hit the network a second time within the cache window
    result = verifier.ensure_verified("someone@gmail.com")
    assert result == "valid"


def test_invalid_syntax_is_suppressed_before_reserving(store):
    db.set_setting("verify_before_send", "1")
    add_inbox()
    campaign = build_campaign(["not-an-email-at-all"], steps=1)
    client = FakeResend()
    engine.run_pass(client)
    assert client.calls == []
    assert db.is_suppressed("not-an-email-at-all") is True
    row = db.connect().execute(
        "SELECT status FROM verification WHERE email='not-an-email-at-all'"
    ).fetchone()
    assert row["status"] == "invalid_syntax"


def test_dead_domain_is_suppressed_before_reserving(store):
    db.set_setting("verify_before_send", "1")
    add_inbox()
    campaign = build_campaign(["a@this-domain-does-not-exist-abc123xyz.com"], steps=1)
    client = FakeResend()
    engine.run_pass(client)
    assert client.calls == []
    assert db.is_suppressed("a@this-domain-does-not-exist-abc123xyz.com") is True


def test_valid_address_sends_normally_when_verification_on(store):
    db.set_setting("verify_before_send", "1")
    add_inbox()
    build_campaign(["real@gmail.com"], steps=1)
    client = FakeResend()
    engine.run_pass(client)
    assert client.recipients == ["real@gmail.com"]


def test_verification_off_sends_without_checking(store):
    db.set_setting("verify_before_send", "0")
    add_inbox()
    build_campaign(["not-an-email-at-all"], steps=1)
    client = FakeResend()
    engine.run_pass(client)
    assert client.recipients == ["not-an-email-at-all"]  # unverified, sent anyway

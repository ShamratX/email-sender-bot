"""Resend wrapper.

Two modes, chosen by the send_mode setting:
  fake -- records the call, sends nothing. The default, and what the tests use.
  live -- real HTTP call to Resend, with the idempotency key from the reserve step.
"""
from __future__ import annotations

import os
import time
import uuid

API_URL = "https://api.resend.com/emails"
TRANSIENT = {429, 500, 502, 503, 504}


class SendError(Exception):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class FakeResend:
    """Records every call instead of sending. Used by tests and by fake mode."""

    def __init__(self, fail_on=None):
        self.calls: list[dict] = []
        self.fail_on = fail_on or set()

    def send(self, *, sender, to, subject, text, idempotency_key, headers=None, api_key=None):
        if to in self.fail_on:
            raise SendError(f"simulated permanent failure for {to}", transient=False)
        self.calls.append(
            {
                "sender": sender,
                "to": to,
                "subject": subject,
                "text": text,
                "idempotency_key": idempotency_key,
            }
        )
        return f"fake-{uuid.uuid4().hex[:16]}"

    @property
    def recipients(self) -> list[str]:
        return [c["to"] for c in self.calls]

    def get_status(self, message_id: str, api_key: str | None = None) -> str | None:
        """Fake mode never bounces on its own. Tests can monkeypatch this."""
        return None


class LiveResend:
    def __init__(self, api_key: str | None = None, max_attempts: int = 3):
        self.api_key = api_key or os.environ.get("RESEND_API_KEY", "")
        self.max_attempts = max_attempts
        if not self.api_key:
            raise SendError("RESEND_API_KEY is not set; cannot send in live mode")

    def send(self, *, sender, to, subject, text, idempotency_key, headers=None, api_key=None):
        """api_key overrides self.api_key for this one send -- lets a single
        LiveResend instance send through a different inbox's own Resend
        account/domain (multi-domain setups), without needing one client
        instance per domain."""
        import httpx  # lazy: fake mode needs no HTTP stack

        payload = {"from": sender, "to": [to], "subject": subject, "text": text}
        if headers:
            payload["headers"] = headers
        request_headers = {
            "Authorization": f"Bearer {api_key or self.api_key}",
            "Idempotency-Key": idempotency_key,
        }
        delay = 2.0
        last = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = httpx.post(
                    API_URL, json=payload, headers=request_headers, timeout=30
                )
            except Exception as exc:  # network-level failure: transient
                last = f"network error: {exc}"
                if attempt == self.max_attempts:
                    raise SendError(last, transient=True)
                time.sleep(delay)
                delay *= 2
                continue
            if response.status_code < 300:
                return response.json().get("id", "")
            last = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code in TRANSIENT and attempt < self.max_attempts:
                time.sleep(delay)
                delay *= 2
                continue
            raise SendError(last, transient=response.status_code in TRANSIENT)
        raise SendError(last, transient=True)

    def get_status(self, message_id: str, api_key: str | None = None) -> str | None:
        """GET /emails/:id -> the 'last_event' field.

        UNVERIFIED against live Resend docs (web lookup was unavailable when this
        was written). Expected values, from Resend's documented email object:
        sent, delivered, delivery_delayed, bounced, complained, opened, clicked.
        Confirm this against a real send before trusting it to drive suppression
        at volume -- see the smoke-test note in worker.py.

        api_key must match whichever account actually sent this message -- in
        a multi-domain/multi-account setup, checking with the wrong key just
        gets a 401/404 and silently returns None, not an error, so bounces on
        a different account's sends would otherwise go undetected.
        """
        import httpx

        try:
            response = httpx.get(
                f"{API_URL}/{message_id}",
                headers={"Authorization": f"Bearer {api_key or self.api_key}"},
                timeout=15,
            )
        except Exception:
            return None
        if response.status_code != 200:
            return None
        return response.json().get("last_event")


def build_client(mode: str):
    return FakeResend() if mode != "live" else LiveResend()

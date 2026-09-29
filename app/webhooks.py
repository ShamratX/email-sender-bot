"""Resend's real bounce/complaint mechanism: a webhook, signed with Svix.

Verification follows Svix's documented scheme (stable, used by many
providers besides Resend): HMAC-SHA256 over "{id}.{timestamp}.{body}" using
the base64 part of the whsec_ secret, compared against the svix-signature
header. This was written from that spec, not tested against a live Resend
webhook yet -- use Resend's dashboard "Send test event" button on this
endpoint once it's public and confirm a 200 before trusting it at volume.

An unsigned or wrongly-signed request is rejected outright: without this
check, anyone who finds the URL could suppress arbitrary addresses.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from . import db

SUPPRESS_ON = {"email.bounced", "email.complained"}


class InvalidSignature(Exception):
    pass


def verify(payload: bytes, svix_id: str, svix_timestamp: str, svix_signature: str) -> None:
    secret = os.environ.get("RESEND_WEBHOOK_SECRET", "")
    if not secret:
        raise InvalidSignature("RESEND_WEBHOOK_SECRET is not set")
    secret_bytes = base64.b64decode(secret.removeprefix("whsec_"))
    signed_content = f"{svix_id}.{svix_timestamp}.{payload.decode()}"
    expected = base64.b64encode(
        hmac.new(secret_bytes, signed_content.encode(), hashlib.sha256).digest()
    ).decode()
    # svix-signature can list several "v1,<sig>" entries (key rotation) --
    # a match on any of them is valid.
    given = [part.split(",", 1)[1] for part in svix_signature.split() if part.startswith("v1,")]
    if not any(hmac.compare_digest(expected, sig) for sig in given):
        raise InvalidSignature("signature did not match")


def handle_event(event: dict) -> str | None:
    """Returns the address suppressed, or None if this event didn't need one."""
    event_type = event.get("type", "")
    if event_type not in SUPPRESS_ON:
        return None
    data = event.get("data", {})
    to = data.get("to")
    email = to[0] if isinstance(to, list) and to else to
    if not email:
        return None
    newly = db.suppress(email, event_type.removeprefix("email."), detail=f"resend webhook: {event_type}")
    if newly:
        db.log_event("auto_suppressed", email, {"reason": event_type})
    return email

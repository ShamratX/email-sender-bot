"""Template rendering, validation and the compliance footer.

Templates use {{variable}} placeholders drawn from the lead's own columns, so
per-lead customisation comes straight from the uploaded sheet. A template that
references a column the data does not have is rejected at save time -- a blank
{{business_name}} in a live send is worse than a failed save.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import db

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_ .-]+?)\s*\}\}")


class TemplateError(Exception):
    pass


def variables_used(text: str) -> set[str]:
    return {m.group(1).strip() for m in PLACEHOLDER.finditer(text)}


def validate(subject_variants: list[str], body: str, available: list[str]) -> None:
    if not subject_variants or not any(s.strip() for s in subject_variants):
        raise TemplateError("at least one subject line is required")
    if not body.strip():
        raise TemplateError("body is empty")
    used: set[str] = set()
    for text in [*subject_variants, body]:
        used |= variables_used(text)
    known = {v.lower() for v in available} | {"email"}
    missing = sorted(v for v in used if v.lower() not in known)
    if missing:
        raise TemplateError(
            "these variables are not present in your lead data: " + ", ".join(missing)
        )


def render(text: str, lead: dict) -> str:
    fields = {"email": lead["email"]}
    fields.update(json.loads(lead["custom_fields"]) if isinstance(lead["custom_fields"], str)
                  else lead["custom_fields"])
    lowered = {k.lower(): v for k, v in fields.items()}

    def swap(match: re.Match) -> str:
        return str(lowered.get(match.group(1).strip().lower(), ""))

    return PLACEHOLDER.sub(swap, text)


def pick_subject(variants: list[str], email: str) -> str:
    """Stable per-address choice, so a batch does not ship identical subjects."""
    if len(variants) == 1:
        return variants[0]
    digest = hashlib.sha256(email.encode()).digest()
    return variants[digest[0] % len(variants)]


def footer() -> str:
    """Short by request, but keeps what CAN-SPAM etc. actually require: an
    unsubscribe method and a postal address. The longer "why you got this"
    line was cut -- not legally required, just courtesy text."""
    address = db.get_setting("postal_address") or ""
    unsubscribe = db.get_setting("unsubscribe_email") or ""
    lines = ["", "---"]
    if unsubscribe:
        lines.append(f"Unsubscribe: reply UNSUBSCRIBE or email {unsubscribe}.")
    else:
        lines.append("Unsubscribe: reply UNSUBSCRIBE.")
    if address:
        lines.append(address)
    return "\n".join(lines)


def compose(
    subject_variants: list[str],
    body: str,
    lead: dict,
    footer_enabled: bool = True,
    footer_text: str | None = None,
) -> tuple[str, str]:
    """Returns (subject, body).

    footer_enabled=False sends with NO unsubscribe line or address at all --
    turning that off is a compliance decision the operator makes knowingly,
    not something the app defaults to. footer_text overrides the global
    default wording for this one template; leave it None to use the
    Settings-page footer() as-is.
    """
    subject = render(pick_subject(subject_variants, lead["email"]), lead)
    rendered = render(body, lead)
    if footer_enabled:
        custom = (footer_text or "").strip()
        rendered += "\n---\n" + render(custom, lead) if custom else "\n" + footer()
    return subject, rendered


def list_unsubscribe_header() -> str | None:
    unsubscribe = db.get_setting("unsubscribe_email") or ""
    return f"<mailto:{unsubscribe}>" if unsubscribe else None

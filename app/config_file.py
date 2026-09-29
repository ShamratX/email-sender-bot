"""Two-way sync between config.json and the templates table.

Templates can be edited two ways, and both stay in sync now:
  - the Templates page in the UI -- saving there also writes the matching
    entry back into config.json, so a later restart won't undo the edit.
  - config.json in the project root, hand-edited -- reloaded into the
    database on every app start.

Either side can be the one you actually touch; the other catches up. A
template created in the UI with a name config.json doesn't have yet gets
added to the file too, so config.json becomes a full, current mirror of
every template. This does not validate variables against your lead data at
load time (you may not have imported yet); the UI still validates on save.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        db.log_event("config_json_error", None, {"error": str(exc)})
        return {}


def write_back_template(name: str, subjects: list[str], body: str) -> None:
    """Called after a UI save. Updates (or adds) this template's entry in
    config.json so the file reflects what's now in the database.

    Writes are all-or-nothing: build the new dict in memory, then replace the
    file's contents in one write. A crash mid-write can't leave the file with
    half its content overwritten.
    """
    config = load()
    config.setdefault("templates", {})
    config["templates"][name] = {"subjects": subjects, "body": body}
    try:
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        db.log_event("config_json_write_error", None, {"error": str(exc)})


def sync_templates() -> int:
    """Upsert every non-empty template from config.json into the database."""
    config = load()
    templates = config.get("templates", {})
    conn = db.connect()
    updated = 0
    for name, spec in templates.items():
        subjects = [s.strip() for s in spec.get("subjects", []) if s.strip()]
        body = (spec.get("body") or "").strip()
        if not subjects or not body:
            continue  # blank entry in the file: leave whatever is in the UI alone
        row = conn.execute("SELECT id FROM templates WHERE name=?", (name,)).fetchone()
        if row:
            tid = int(row["id"])
            conn.execute("UPDATE templates SET body=? WHERE id=?", (body, tid))
        else:
            cur = conn.execute(
                "INSERT INTO templates (name, body, created_at) VALUES (?, ?, ?)",
                (name, body, db.utcnow()),
            )
            tid = int(cur.lastrowid)
        conn.execute("DELETE FROM subject_variants WHERE template_id=?", (tid,))
        for subject in subjects:
            conn.execute(
                "INSERT INTO subject_variants (template_id, subject) VALUES (?, ?)",
                (tid, subject),
            )
        updated += 1
    if updated:
        db.log_event("config_json_synced", None, {"templates": updated})
    return updated

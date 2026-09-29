"""SQLite access, schema and the atomic send-state transitions.

Everything the app knows lives in one file: state/sender.db. The reserve/settle
protocol in this module is the only thing standing between a crash and a
duplicate email, so changes here need the tests in tests/ to stay green.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
DB_PATH = STATE_DIR / "sender.db"

RESERVE_TIMEOUT_MINUTES = 10

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lead_lists (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
  id            INTEGER PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  custom_fields TEXT NOT NULL DEFAULT '{}',
  source_file   TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lead_list_members (
  list_id INTEGER NOT NULL,
  lead_id INTEGER NOT NULL,
  PRIMARY KEY (list_id, lead_id)
);

CREATE TABLE IF NOT EXISTS templates (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  body          TEXT NOT NULL,
  footer_enabled INTEGER NOT NULL DEFAULT 1,
  footer_text   TEXT,      -- NULL = use the global default from Settings
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subject_variants (
  id          INTEGER PRIMARY KEY,
  template_id INTEGER NOT NULL,
  subject     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,   -- e.g. "Domain 1", "Amazon Resend"
  api_key    TEXT NOT NULL,          -- set once here, not per inbox
  warmup_start_date TEXT DEFAULT '', -- this account's OWN warm-up clock;
                                      -- blank = falls back to the global setting below
  warmup_schedule   TEXT DEFAULT '', -- this account's OWN per-inbox limits (JSON);
                                      -- blank = falls back to the global schedule
  daily_cap         INTEGER,         -- this account's OWN Resend plan limit;
                                      -- NULL = falls back to the global account_daily_cap
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inboxes (
  id         INTEGER PRIMARY KEY,
  email      TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,
  enabled    INTEGER NOT NULL DEFAULT 1,
  api_key    TEXT,       -- legacy: an inbox's own key, kept for backward compatibility.
                          -- new setups use account_id below instead.
  account_id INTEGER      -- which Account this inbox belongs to; blank = default/shared .env key
);

CREATE TABLE IF NOT EXISTS campaigns (
  id                  INTEGER PRIMARY KEY,
  name                TEXT NOT NULL UNIQUE,
  list_id             INTEGER NOT NULL,
  state               TEXT NOT NULL DEFAULT 'draft',   -- draft|running|paused|done
  gap_days            INTEGER NOT NULL DEFAULT 3,
  window_start        TEXT NOT NULL DEFAULT '09:00',
  window_end          TEXT NOT NULL DEFAULT '18:00',
  followup_mode       TEXT NOT NULL DEFAULT 'manual',  -- auto|manual
  followups_released  INTEGER NOT NULL DEFAULT 0,
  daily_cap           INTEGER,
  country_filter      TEXT,      -- e.g. 'US' -- only leads whose country field matches this
  respect_country_windows INTEGER NOT NULL DEFAULT 0,  -- legacy, superseded by windowed_countries below
  windowed_countries TEXT NOT NULL DEFAULT '',  -- comma-separated codes e.g. 'AU,UK' -- only these
                                                  -- countries' leads are gated by the per-country window;
                                                  -- everyone else (including unlisted countries) sends anytime
  allowed_inboxes TEXT NOT NULL DEFAULT '',      -- legacy multi-select, kept for backward compatibility
  account_id INTEGER,                            -- simple path: pick one Account, uses all its inboxes.
                                                  -- blank = any enabled inbox (old behaviour, unchanged)
  created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_steps (
  campaign_id INTEGER NOT NULL,
  step        INTEGER NOT NULL,
  template_id INTEGER NOT NULL,
  PRIMARY KEY (campaign_id, step)
);

CREATE TABLE IF NOT EXISTS sends (
  id                INTEGER PRIMARY KEY,
  lead_id           INTEGER NOT NULL,
  campaign_id       INTEGER NOT NULL,
  step              INTEGER NOT NULL,
  inbox_id          INTEGER NOT NULL,
  state             TEXT NOT NULL,          -- reserved|sent|failed|unknown
  idempotency_key   TEXT NOT NULL UNIQUE,
  resend_message_id TEXT,
  last_status       TEXT,
  status_checked_at TEXT,
  subject_used      TEXT,
  body_rendered     TEXT,
  reserved_at       TEXT NOT NULL,
  sent_at           TEXT,
  sent_day          TEXT,
  error             TEXT,
  UNIQUE (lead_id, campaign_id, step)
);

CREATE TABLE IF NOT EXISTS verification (
  email      TEXT PRIMARY KEY,
  status     TEXT NOT NULL,      -- valid|invalid_syntax|no_mx|unknown
  checked_at TEXT NOT NULL,
  detail     TEXT
);

CREATE TABLE IF NOT EXISTS suppression (
  email      TEXT PRIMARY KEY,
  reason     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  detail     TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY,
  email      TEXT,
  kind       TEXT NOT NULL,
  payload    TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
  id         INTEGER PRIMARY KEY,
  filename   TEXT NOT NULL,
  list_id    INTEGER,
  stats      TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sends_lookup ON sends (campaign_id, step, state);
CREATE INDEX IF NOT EXISTS idx_sends_day    ON sends (sent_day, inbox_id);
"""

DEFAULT_SETTINGS = {
    "timezone": "Asia/Dhaka",
    "sending_enabled": "0",
    "send_mode": "fake",  # fake | live -- live requires RESEND_API_KEY
    "default_daily_send_limit": "35",     # used per inbox when that inbox has no Send/day of its own
    "default_daily_followup_limit": "",   # blank = no limit, used when an inbox has no Follow-up/day of its own
    "delay_min_seconds": "45",
    "delay_max_seconds": "180",
    "rotation_cursor": "0",
    "postal_address": "",
    "unsubscribe_email": "",
    "verify_before_send": "1",
    "skip_weekends": "1",  # default on, per operator request
    # Times below are each country's OWN local business hours -- no manual
    # timezone conversion needed. country_timezones says which clock each
    # country's start/end is read in.
    "country_windows": json.dumps({
        "AU": ["09:00", "17:00"],
        "UK": ["09:00", "17:00"],
        "CA": ["09:00", "17:00"],
        "US": ["09:00", "17:00"],
    }),
    "country_timezones": json.dumps({
        "AU": "Australia/Sydney",
        "UK": "Europe/London",
        "CA": "America/Toronto",
        "US": "America/New_York",
    }),
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """One connection per thread. WAL so the worker and the web app coexist."""
    target = str(path or os.environ.get("SENDER_DB") or DB_PATH)
    cached = getattr(_local, "conn", None)
    if cached is not None and getattr(_local, "path", None) == target:
        return cached
    if cached is not None:
        cached.close()
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    _local.conn = conn
    _local.path = target
    return conn


def close() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


def init(path: Path | str | None = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent column adds for databases created before a schema change."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sends)").fetchall()}
    if "status_checked_at" not in columns:
        conn.execute("ALTER TABLE sends ADD COLUMN status_checked_at TEXT")

    campaign_columns = {row["name"] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "country_filter" not in campaign_columns:
        conn.execute("ALTER TABLE campaigns ADD COLUMN country_filter TEXT")
    if "respect_country_windows" not in campaign_columns:
        conn.execute(
            "ALTER TABLE campaigns ADD COLUMN respect_country_windows INTEGER NOT NULL DEFAULT 0"
        )
    if "windowed_countries" not in campaign_columns:
        conn.execute("ALTER TABLE campaigns ADD COLUMN windowed_countries TEXT NOT NULL DEFAULT ''")
        # migrate existing campaigns: the old blanket on/off becomes "all four" or "none"
        conn.execute(
            "UPDATE campaigns SET windowed_countries='AU,UK,CA,US' WHERE respect_country_windows=1"
        )
    if "allowed_inboxes" not in campaign_columns:
        conn.execute("ALTER TABLE campaigns ADD COLUMN allowed_inboxes TEXT NOT NULL DEFAULT ''")
    if "account_id" not in campaign_columns:
        conn.execute("ALTER TABLE campaigns ADD COLUMN account_id INTEGER")

    inbox_columns = {row["name"] for row in conn.execute("PRAGMA table_info(inboxes)").fetchall()}
    if "api_key" not in inbox_columns:
        conn.execute("ALTER TABLE inboxes ADD COLUMN api_key TEXT")
    if "account_id" not in inbox_columns:
        conn.execute("ALTER TABLE inboxes ADD COLUMN account_id INTEGER")
    if "daily_limit" not in inbox_columns:
        # Manual per-inbox send limit -- NULL means "use the global default".
        conn.execute("ALTER TABLE inboxes ADD COLUMN daily_limit INTEGER")
    if "daily_followup_limit" not in inbox_columns:
        # Same idea, kept as a separate number so a backlog of due follow-ups
        # can't crowd out first-contact sends from the same inbox, or vice
        # versa. NULL = use the global default (which may itself be "no limit").
        conn.execute("ALTER TABLE inboxes ADD COLUMN daily_followup_limit INTEGER")

    account_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "warmup_start_date" not in account_columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN warmup_start_date TEXT DEFAULT ''")
    if "warmup_schedule" not in account_columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN warmup_schedule TEXT DEFAULT ''")
    if "daily_cap" not in account_columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN daily_cap INTEGER")

    template_columns = {row["name"] for row in conn.execute("PRAGMA table_info(templates)").fetchall()}
    if "footer_enabled" not in template_columns:
        conn.execute("ALTER TABLE templates ADD COLUMN footer_enabled INTEGER NOT NULL DEFAULT 1")
    if "footer_text" not in template_columns:
        conn.execute("ALTER TABLE templates ADD COLUMN footer_text TEXT")


# --- settings -------------------------------------------------------------

def get_setting(key: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def set_setting(key: str, value) -> None:
    connect().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --- events ---------------------------------------------------------------

def log_event(kind: str, email: str | None = None, payload: dict | None = None) -> None:
    connect().execute(
        "INSERT INTO events (email, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (email, kind, json.dumps(payload or {}), utcnow()),
    )


# --- suppression ----------------------------------------------------------

def suppress(email: str, reason: str, detail: str | None = None) -> bool:
    """Add an address to the permanent exclusion list. Never removed by the app."""
    email = email.strip().lower()
    cur = connect().execute(
        "INSERT OR IGNORE INTO suppression (email, reason, created_at, detail) "
        "VALUES (?, ?, ?, ?)",
        (email, reason, utcnow(), detail),
    )
    if cur.rowcount:
        log_event("suppressed", email, {"reason": reason})
        return True
    return False


def inbox_api_key(inbox_id: int) -> str | None:
    """Resolves which Resend key an inbox actually sends with:
    its own key (legacy per-inbox field) > its Account's key > None (falls
    back to RESEND_API_KEY in .env, the original single-account setup)."""
    row = connect().execute(
        "SELECT i.api_key AS own_key, a.api_key AS account_key "
        "FROM inboxes i LEFT JOIN accounts a ON a.id = i.account_id WHERE i.id=?",
        (inbox_id,),
    ).fetchone()
    if row is None:
        return None
    return row["own_key"] or row["account_key"] or None


def is_suppressed(email: str) -> bool:
    row = connect().execute(
        "SELECT 1 FROM suppression WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    return row is not None


# --- the send state machine ----------------------------------------------

class AlreadyHandled(Exception):
    """This (lead, campaign, step) already has a send row. Never send again."""


def _install_id() -> str:
    """A random value generated once per database, so the same lead_id/
    campaign_id/step numbers never collide with another install's idempotency
    keys against the same Resend account -- e.g. testing locally, then
    running the real thing on a VPS with a fresh database, reuses small
    sequential IDs that would otherwise produce the identical key Resend
    already saw with different email content, and get rejected with a 409."""
    value = get_setting("install_id")
    if not value:
        import os

        value = os.urandom(16).hex()
        set_setting("install_id", value)
    return value


def idempotency_key(lead_id: int, campaign_id: int, step: int) -> str:
    raw = f"{_install_id()}:{lead_id}:{campaign_id}:{step}"
    return hashlib.sha256(raw.encode()).hexdigest()


def reserve(lead_id: int, campaign_id: int, step: int, inbox_id: int) -> int:
    """Step 1 of the protocol: claim the slot BEFORE talking to the provider.

    Raises AlreadyHandled if any row exists for this triple, including one left
    behind by an earlier crash. That refusal is the whole point.
    """
    key = idempotency_key(lead_id, campaign_id, step)
    try:
        cur = connect().execute(
            "INSERT INTO sends (lead_id, campaign_id, step, inbox_id, state, "
            "idempotency_key, reserved_at) VALUES (?, ?, ?, ?, 'reserved', ?, ?)",
            (lead_id, campaign_id, step, inbox_id, key, utcnow()),
        )
    except sqlite3.IntegrityError as exc:
        raise AlreadyHandled(f"lead={lead_id} campaign={campaign_id} step={step}") from exc
    return int(cur.lastrowid)


def settle_sent(send_id: int, message_id: str, subject: str, body: str, day: str) -> None:
    connect().execute(
        "UPDATE sends SET state='sent', resend_message_id=?, subject_used=?, "
        "body_rendered=?, sent_at=?, sent_day=? WHERE id=? AND state='reserved'",
        (message_id, subject, body, utcnow(), day, send_id),
    )


def settle_failed(send_id: int, error: str) -> None:
    connect().execute(
        "UPDATE sends SET state='failed', error=? WHERE id=? AND state='reserved'",
        (error[:500], send_id),
    )


def sweep_stale_reserved(now: datetime | None = None) -> int:
    """A reserved row older than the timeout means the process died mid-send.

    We cannot know whether the email left. So it becomes 'unknown' and is never
    retried automatically -- it surfaces in the UI for a human decision.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=RESERVE_TIMEOUT_MINUTES)).isoformat(timespec="seconds")
    cur = connect().execute(
        "UPDATE sends SET state='unknown', error='process died between reserve and settle' "
        "WHERE state='reserved' AND reserved_at < ?",
        (cutoff,),
    )
    return cur.rowcount

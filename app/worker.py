"""Background sender. One thread, started by run.py inside the web process.

It wakes every few seconds, does at most one send, then sleeps a randomised
pause so a batch does not go out on a machine-perfect interval.
"""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path

from . import bounce_poller, db, engine
from .resend_client import build_client

STOP_FILE = Path(__file__).resolve().parent.parent / "state" / "STOP"
BOUNCE_POLL_SECONDS = 600  # 10 minutes

_thread: threading.Thread | None = None
_stop = threading.Event()
_status = {"last_pass": None, "last_result": "not started yet", "last_bounce_check": None}
_last_bounce_poll = 0.0


def status() -> dict:
    return dict(_status)


def _hard_stopped() -> bool:
    return STOP_FILE.exists()


def _maybe_poll_bounces(client) -> None:
    """Runs on its own 10-minute clock, independent of the sending on/off switch.

    A bounce from an email already sent matters even while sending is stopped --
    it must not sit un-suppressed just because the operator paused the campaign.
    """
    global _last_bounce_poll
    now = time.monotonic()
    if now - _last_bounce_poll < BOUNCE_POLL_SECONDS:
        return
    _last_bounce_poll = now
    try:
        checked = bounce_poller.poll(client)
        _status["last_bounce_check"] = db.utcnow()
        if checked:
            db.log_event("bounce_poll", None, {"checked": checked})
    except Exception as exc:
        db.log_event("bounce_poll_error", None, {"error": str(exc)})


def _loop() -> None:
    db.init()
    while not _stop.is_set():
        try:
            client = build_client(db.get_setting("send_mode") or "fake")
            _maybe_poll_bounces(client)

            if _hard_stopped():
                db.set_setting("sending_enabled", "0")
                _status["last_result"] = "halted by state/STOP file"
                _stop.wait(5)
                continue
            if db.get_setting("sending_enabled") != "1":
                _status["last_result"] = "stopped"
                _stop.wait(5)
                continue

            results = engine.run_pass(client, limit=1)
            _status["last_pass"] = db.utcnow()
            sent = [r for r in results if r["status"] == "sent"]
            if sent:
                _status["last_result"] = f"sent to {sent[0]['email']}"
                low = int(db.get_setting("delay_min_seconds") or 45)
                high = int(db.get_setting("delay_max_seconds") or 180)
                _stop.wait(random.uniform(low, max(low, high)))
            else:
                reasons = {r.get("reason") for r in results if r.get("reason")}
                _status["last_result"] = "; ".join(sorted(r for r in reasons if r)) or "nothing due"
                _stop.wait(15)
        except Exception as exc:  # a worker crash must never take the UI down
            _status["last_result"] = f"worker error: {exc}"
            db.log_event("worker_error", None, {"error": str(exc)})
            _stop.wait(15)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="sender-worker", daemon=True)
    _thread.start()


def shutdown() -> None:
    _stop.set()

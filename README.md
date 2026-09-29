# Email Sender Bot

Local web app. Upload a spreadsheet of leads, write templates, send through Resend on a schedule, with warm-up limits you control from a dashboard.

## Every command, one place

| Where | Command |
|---|---|
| **Your Windows PC** | `cd "D:\Desktop\Important files\Email sender bot"` then `.\start.ps1` |
| **Windows VPS** | `cd C:\path\to\email-sender-bot` then `.\start.ps1` |
| **Linux VPS — install (once)** | `sudo apt update && sudo apt install -y python3 python3-venv python3-pip` |
| **Linux VPS — set up (once)** | `cd /path/to/email-sender-bot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| **Linux VPS — run** | `.venv/bin/python run.py` |
| **Linux VPS — run in background, survive SSH disconnect** | `nohup .venv/bin/python run.py > log.txt 2>&1 &` |
| **Run the tests (any device)** | `python -m pytest -v` (Windows) or `.venv/bin/python -m pytest -v` (Linux) |
| **Stop it** | `Ctrl+C` in the terminal it's running in, or the dashboard **STOP** button, or create an empty file `state/STOP` |

Full explanation of each below.

## Run it

```powershell
cd "D:\Desktop\Important files\Email sender bot"
.\start.ps1
```

First run creates `.venv` and installs dependencies. After that it starts in a second and opens `http://127.0.0.1:8420` in your browser. `Ctrl+C` stops everything.

**Sending is OFF when the app starts.** Nothing goes out until you press _Start sending_ on the dashboard, and the mode is `fake` until you change it in Settings — so you can click through the whole app without a single real email leaving.

## First-time setup, in order

1. **Settings** — set your timezone, postal address and unsubscribe address. Leave the mode on `fake` for now.
2. **Inboxes** — add your sender addresses. Set the warm-up start date and your Resend plan's daily cap (100 on the free tier).
3. **Upload sheet** — drop a `.csv` or `.xlsx`. Confirm the email column, check the preview counts, import.
4. **Templates** — write the initial email and up to three follow-ups. Use `{{Column Name}}` for anything from your sheet. Preview against a real lead before saving anything you intend to send.
5. **Campaigns** — pick the list, the templates, the gap between follow-ups, the send window, and whether follow-ups go out automatically or wait for you.
6. Press **Run** on the campaign, then **Start sending** on the dashboard. Watch the activity log fill up in fake mode.
7. When you are satisfied, switch the mode to `live` in Settings, with `RESEND_API_KEY` set in `.env`.

## The safety rules built into it

- **Imports only add.** No import deletes a lead, clears send history or removes a suppression. Existing addresses default to _Skip_.
- **One send per lead per campaign step**, enforced by a database constraint, not by application logic. Even a total failure of everything above it cannot produce a duplicate.
- **Reserve before send.** The row is written before the email leaves. If the process dies in between, the row becomes `unknown` and is never retried automatically — it waits for you on the dashboard under _Needs attention_.
- **Two daily ceilings** — per inbox and per account — both checked before every send.
- **Suppression is permanent.** There is no delete button.
- **Follow-ups stop** for anyone who replied, bounced, complained or unsubscribed.

## Stopping

- **STOP button** on the dashboard — halts within about 5 seconds and stays stopped across restarts.
- **`Ctrl+C`** in PowerShell.
- **`state/STOP`** — create this empty file and the worker halts even if the web UI is unreachable.

All three are safe at any moment. A half-finished send is detected on restart, never repeated.

## Tests

```powershell
python -m pytest -v
```

23 tests covering the duplicate-send guarantee, crash recovery, warm-up caps, append-only import, follow-up timing and template validation. Do not send real email if any of them fail.

## Backing up / moving machines

Everything is in `state/sender.db`, plus the sheets in `state/uploads/`. Copy the whole `state/` folder. Run only one copy at a time — two instances against two database files will double-send, and no code can prevent that.

## Pre-send verification

On by default (Settings → "Check each address before sending"). Before every send: checks syntax, then looks up the domain's MX record. Bad syntax or a dead domain → the address is skipped and permanently suppressed, same as a bounce — no email attempted, no warm-up quota spent.

Free, no API key, no signup. Catches typo'd domains (`gmial.com`), made-up domains, and malformed addresses. Does **not** catch a made-up mailbox on a real domain (`totally-fake-name@gmail.com` passes, because gmail.com has mail servers) — that needs an SMTP handshake or a paid service like ZeroBounce, not built here. Results are cached 30 days per address so re-imports don't re-check.

## Bounce handling

Runs automatically, every 10 minutes, independent of the Start/Stop switch. Checks the last 3 days of sent emails against Resend's delivery status. `bounced` or `complained` → permanently suppressed, same as a manual entry. Everything else (delivered, opened, clicked) is recorded but does not block anything.

**Caveat:** this relies on Resend's `GET /emails/:id` `last_event` field, which was implemented from documented API shape without a live docs lookup (web access was unavailable when built). Before trusting this at volume: send one email to an address you know will hard-bounce (e.g. a made-up address at a real domain), wait 10 minutes, check the Suppression page and the `events` log for an `auto_suppressed` entry. If it doesn't appear, the field name needs correcting in [app/resend_client.py](app/resend_client.py) `get_status()`.

Soft bounces, catch-all addresses, and spam-folder placement are not caught by this — see BUILD_PLAN.md section 7 for what a full IMAP reply/bounce poller would additionally need.

## Not built yet

`reply_poller.py` (IMAP reply and unsubscribe detection). Until it exists, replies must be added on the Suppression page by hand. See BUILD_PLAN.md section 6.

## Running on a VPS (Linux)

Built and tested for local Windows use — moving it to a VPS needs a few manual steps, not a straight file copy.

**What to copy from your PC:**
- `app/`, `config.json`, `requirements.txt`, `run.py`
- `.env` (your real `RESEND_API_KEY` — never commit this)
- `state/` if you want to keep existing leads and send history

**What NOT to copy:**
- `.venv/` — Windows binaries, won't run on Linux. Reinstall fresh (below).
- `start.ps1` — PowerShell only, won't run on Linux.

**Install on the VPS (Ubuntu/Debian example):**

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

cd /path/to/email-sender-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Run it:**

```bash
.venv/bin/python run.py
```

**Keep it running after you disconnect SSH** — pick one:
- `nohup .venv/bin/python run.py > log.txt 2>&1 &` (quick, informal)
- `screen` or `tmux`, start the app inside, detach
- A `systemd` service (most reliable, auto-restarts on crash or reboot) — ask if you want this set up

**Before exposing it beyond your own machine (Cloudflare Tunnel, public IP, etc.):**
`run.py` currently binds to `127.0.0.1` only, and there is **no login/password** — anyone who can reach the URL has full control (can send emails, change settings, everything). Needed before going public:
1. Change the bind address to `0.0.0.0` in `run.py`.
2. Add a login page (username/password) in front of the whole app.

Neither is built yet. Do not point a tunnel or public IP at this app until both exist — ask and they can be added.

## Running on a Windows VPS

Much simpler than Linux — the app was built and tested on Windows, so it works almost exactly like your PC.

**Copy everything**, including `.venv/` this time (Windows binaries match a Windows VPS, no reinstall needed) — or skip `.venv/` and let `start.ps1` recreate it fresh on first run, same as it did on your PC.

**Run it:** same as local —

```powershell
cd C:\path\to\email-sender-bot
.\start.ps1
```

**Keep it running after you disconnect RDP** — a PowerShell window closes when you log off by default. Options:
- Run it inside `Task Scheduler` as a scheduled task set to "Run whether user is logged on or not."
- Use [NSSM](https://nssm.cc/) to install it as a proper Windows service (most reliable, auto-restarts on crash/reboot) — ask if you want this set up.
- Simplest but fragile: stay logged into RDP with the window open (breaks if you disconnect).

**Same security gap applies here too** — `run.py` binds to `127.0.0.1` only and has no login. Don't expose it via Cloudflare Tunnel or a public IP until the `0.0.0.0` binding and a login page are added (see the Linux section above — the fix is identical on Windows).

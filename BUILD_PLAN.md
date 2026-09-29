# Email Sender Bot — Build Plan v4 (local UI, one command)

**How you run it:** open PowerShell, run one command, the browser opens on the dashboard. Everything after that happens in the UI.

```powershell
.\start.ps1
```

That script activates the virtualenv, starts the app on `http://127.0.0.1:8420`, starts the background worker inside the same process, and opens your browser. `Ctrl+C` stops everything.

**Lead source is out of scope.** You upload spreadsheets. Where the rows came from is your business.

---

## 1. Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.11 + FastAPI | One language for app, worker and Resend calls |
| UI | Jinja2 + one hand-written `style.css` | Plain HTML forms and tables. No Tailwind, no HTMX, no JavaScript framework, no build step |
| Database | SQLite (WAL mode) | One file. Atomic transactions. Copy it to back up |
| Scheduling | APScheduler, in the same process as the web app | One command, one process, one thing to stop |
| Binding | `127.0.0.1` only | Not reachable from the network. No TLS or login needed for v1 |

Single process is the right call here because you run it yourself on your own machine. There is nothing to orchestrate.

### 1.1 UI rules

Keep it plain. These are hard constraints, not preferences:

- Plain HTML: `<form>`, `<table>`, `<button>`, `<select>`. Every action is a normal form POST followed by a redirect.
- One stylesheet, `static/style.css`, written by hand — a few hundred lines at most. No framework, no CDN, so the app works with no internet connection.
- No JavaScript, with one exception: the file-upload drop zone may use a few lines, and even that falls back to a normal `<input type="file">`.
- Pages that need to refresh themselves use `<meta http-equiv="refresh">`.
- Progress bars are a `<div>` with a percentage width. Charts are not needed anywhere.

The reason is not taste. Every line of frontend is a line that can break between you and the STOP button.

### 1.2 What running locally costs you

Three real consequences. Two have clean workarounds, one does not:

- **No webhooks.** Resend cannot call back to `127.0.0.1`. Instead the worker **polls Resend for the status of recent sends** every 10 minutes and records delivered, bounced and complained from that. Slightly delayed, otherwise equivalent. *(Confirm the retrieve-email endpoint's exact status field against Resend's current docs at build time rather than assuming it.)*
- **No hosted unsubscribe page.** Solved with a `mailto:` unsubscribe instead of a link: `List-Unsubscribe: <mailto:unsubscribe@yourdomain.com>`, plus a visible "reply with UNSUBSCRIBE to opt out" line in the footer. The IMAP poller reads that mailbox and suppresses automatically. This satisfies the requirement without any public server. If you later want a one-click link, that needs a cheap always-on host — a separate decision, not a blocker now.
- **The PC must be on to send.** No workaround. If the machine sleeps mid-campaign, sending stops and resumes when you start it again — safely, without duplicates, but time passes. Disable sleep during send windows.

---

## 2. Structure

```
email-sender-bot/
├── start.ps1                # the one command
├── start.bat                # same, for double-click
├── run.py                   # boots uvicorn + worker, opens browser
├── app/
│   ├── main.py              # FastAPI routes
│   ├── worker.py            # send loop, follow-up scanner, status poller
│   ├── db.py                # schema, migrations, atomic state transitions
│   ├── importer.py          # CSV/XLSX parse, column mapping, append-only import
│   ├── templating.py        # variable rendering, preview, validation
│   ├── resend_client.py     # send + status polling, retries, idempotency keys
│   ├── warmup.py            # per-inbox + account daily caps, rotation
│   ├── followups.py         # who is due, and why
│   ├── reply_poller.py      # IMAP: replies, unsubscribes, bounce notices
│   ├── compliance.py        # footer, suppression checks
│   ├── templates/           # Jinja2 pages, plain HTML
│   └── static/style.css     # one stylesheet, hand-written
├── state/
│   ├── sender.db            # everything lives here
│   ├── uploads/             # every sheet you ever uploaded, kept as-is
│   └── backups/             # automatic daily copies
├── logs/
├── .env                     # RESEND_API_KEY, IMAP passwords — gitignored
├── .env.example
├── requirements.txt
└── README.md
```

---

## 3. Uploading sheets — append only, always

This is the rule you asked for, stated as the system behaviour:

> **An import can only add. It can never delete a lead, never clear send history, and never un-suppress an address.**

Concretely, when you upload a sheet:

- **New emails** → inserted as new leads.
- **Emails already in the database** → handled by a choice you make on the preview screen, defaulting to the safest option:
  - *Skip existing* **(default)** — leave them exactly as they are.
  - *Fill blanks only* — add custom fields that are currently empty, change nothing that already has a value.
  - *Update fields* — refresh custom fields from the new sheet. Even here, send history, status and suppression are untouched.
- **Emails on the suppression list** → imported but flagged as suppressed, never queued. They appear in the preview count so you know.
- **Rows missing an email, or with invalid syntax** → skipped and listed, so you can fix the sheet.

There is no "replace list" and no "clear leads" action anywhere in the app. Deleting a lead is possible one at a time from its detail page, and even that leaves the send history and any suppression row in place.

The original file is kept in `state/uploads/` with its import timestamp, so you can always see which sheet a lead came from.

### Import screen flow
1. Drop a `.csv` or `.xlsx`.
2. **Map columns** — the app guesses `email`, `business_name`, `city`, and so on; you confirm. Email is the only required mapping. Every other column becomes a **custom field** you can use in templates.
3. **Preview** — first 10 mapped rows, plus counts: new, already existing, suppressed, invalid, duplicated within the file itself.
4. Pick the existing-lead behaviour, choose which list to add to, and import.

---

## 4. Follow-ups

### 4.1 How the bot decides who needs one

A background scan runs every 15 minutes and marks a lead **due for follow-up N** when all of these hold:

1. Step N−1 was actually sent (state `sent`), not merely queued.
2. At least `gap_days` have passed since that send — default 3, editable per campaign.
3. N is within the campaign's follow-up count — default 3.
4. The lead has **not** replied, bounced, complained or unsubscribed.
5. There is no existing send row for this step (the database refuses a duplicate regardless).

The gap is measured from the last email actually sent to that lead, not from the campaign start — so a lead imported late still gets a correct 3-day spacing.

### 4.2 What you see

A **Follow-ups** page listing everyone due, grouped by campaign, showing for each lead: which step, when the previous email went out, how many days have passed, and which template will be used. Counts appear as a badge on the dashboard: *"Follow-ups due: 14"*.

### 4.3 How they get sent — your choice, per campaign

- **Auto** — the worker sends them during the campaign's send window, under the same warm-up caps as everything else. Nothing for you to do.
- **Manual** — nothing sends until you click *Send follow-ups now* on the page. Useful while you still want eyes on every batch.

A per-campaign toggle, switchable at any time. Follow-ups and initial emails draw from the same daily quota, because the recipient's mail provider does not care which one you call it.

---

## 5. Dashboard

Top to bottom, the page you keep open:

- **STOP button.** Large, always visible, top of the page. Click it and sending halts within 5 seconds. Anything mid-flight finishes settling into the database, then nothing further starts. The button turns into *Resume*. State survives a restart — if you stopped it, it stays stopped until you resume.
- **Today, per inbox** — sent / limit, as bars. One row per inbox.
- **Today, account total** — sent / account cap. Red at 90%.
- **Warm-up** — current week, today's per-inbox limit, date of the next increase.
- **Queue** — sending now, due today, follow-ups due, waiting on the gap.
- **Live activity** — last 50 events. The page carries `<meta http-equiv="refresh" content="15">` so it updates itself. No JavaScript.
- **Needs attention** — `unknown` sends awaiting your decision, failures, campaigns that stopped early.

### Other pages
- **Leads** — searchable, filterable table; detail page with every custom field and the exact body of every email sent to that person.
- **Templates** — subject variants (rotated per lead by a hash of the address, so no batch ships identical subject lines), body editor with a variable picker built from your actual columns, and a live preview against a real lead. Save is blocked if a template uses a variable your data does not have — a blank `{{business_name}}` in a live send is worse than a failed save.
- **Campaigns** — list + initial template + up to 3 follow-ups + gap days + allowed inboxes + send window + auto/manual follow-up toggle.
- **Inboxes and warm-up** — add inboxes, edit the week→limit table, set the account cap.
- **Suppression** — searchable, add manually, **no delete button**.
- **Settings** — API keys (write-only fields), send windows, delay range between sends, footer address.

---

## 6. Sending core — the part that must not be simplified

### 6.1 Reserve before send

A naive sender calls Resend, then records the result. If the process dies after the request leaves but before the response arrives, the email went out and nothing recorded it. Next run sends it again, to a real person. On a laptop that gets closed mid-run, this is not a hypothetical.

So:

1. **Reserve** — insert a `sends` row, `state='reserved'`, `idempotency_key = sha256(lead_id + campaign_id + step)`. Commit. If `UNIQUE(lead_id, campaign_id, step)` rejects it, it is already handled — skip.
2. **Send** — call Resend with that key as the `Idempotency-Key` header, so a replay returns the original result instead of sending again.
3. **Settle** — update to `sent` with the message id, or `failed` with the error.

A crash between 2 and 3 leaves `reserved`. On restart, any `reserved` row older than 10 minutes becomes `unknown` and is **never retried automatically** — it appears under *Needs attention* for you to decide. An unsent email costs one lead; a double send costs your domain.

Retry only on HTTP 429 and 5xx, at most twice, always re-using the same key.

### 6.2 Checks before every send, in this order

1. Address in `suppression` → skip forever.
2. A send row exists for this `(lead, campaign, step)` → skip.
3. Follow-up gap not yet elapsed → not yet.
4. Outside the campaign's send window → stop this pass.
5. Inbox limit, campaign cap or account cap reached → stop this pass.
6. STOP is active → halt immediately.

### 6.3 Warm-up

Two ceilings, both enforced: per inbox per day, and per account per day. The account cap matters because Resend's free tier allows 100/day across everything — four inboxes at 35 is 140, which fails silently halfway through a run.

| Week | Per inbox/day | 4 inboxes | Fits free tier (100/day)? |
|---|---|---|---|
| 1 | 6 | 24 | yes |
| 2 | 12 | 48 | yes |
| 3 | 20 | 80 | yes |
| 4+ | 35 | 140 | **no — needs Resend Pro** |

The table is editable in the UI. Raising a limit during weeks 1–4 requires confirming a dialog, because that is the fastest way to burn a young domain.

Inboxes rotate round-robin, and the cursor is stored in the database so a restart does not reset to the first inbox and overload it.

### 6.4 Pacing
Randomised 45–180 seconds between sends, configurable. Twenty identically-spaced sends is a machine signature.

### 6.5 Schema core

```sql
CREATE TABLE sends (
  id                INTEGER PRIMARY KEY,
  lead_id           INTEGER NOT NULL,
  campaign_id       INTEGER NOT NULL,
  step              INTEGER NOT NULL,      -- 0 = initial, 1..3 = follow-ups
  inbox_id          INTEGER NOT NULL,
  state             TEXT NOT NULL,         -- reserved|sent|failed|unknown
  idempotency_key   TEXT NOT NULL UNIQUE,
  resend_message_id TEXT,
  last_status       TEXT,                  -- from status polling
  subject_used      TEXT,
  body_rendered     TEXT,                  -- exactly what was sent
  reserved_at       TEXT NOT NULL,
  sent_at           TEXT,
  error             TEXT,
  UNIQUE(lead_id, campaign_id, step)
);

CREATE TABLE suppression (
  email      TEXT PRIMARY KEY,
  reason     TEXT NOT NULL,   -- hard_bounce|complaint|unsubscribe|replied|manual|invalid
  created_at TEXT NOT NULL,
  detail     TEXT
);
```

Plus `leads` (with a JSON `custom_fields` column), `lead_lists`, `campaigns`, `templates`, `subject_variants`, `inboxes`, `settings`, `imports`, `events`.

`UNIQUE(lead_id, campaign_id, step)` is the defence that survives every bug above it. Even if the UI, the warm-up logic and the worker all fail at once, the database physically refuses a second send.

---

## 7. Inbound handling (IMAP poller, every 15 minutes)

Connects to each sender inbox and reads unseen mail:

- **A reply from a lead** (sender matches, or `In-Reply-To` matches a stored message id) → suppress with reason `replied`. All follow-ups stop. The message is left unread so you still see it.
- **An unsubscribe request** (to the unsubscribe address, or a body matching UNSUBSCRIBE / REMOVE / STOP) → suppress with reason `unsubscribe`.
- **A bounce notice** (from `MAILER-DAEMON`, or an RFC 3464 delivery-status report) → suppress if permanent. This is a second net under the Resend status polling.
- **Auto-replies and out-of-office** (`Auto-Submitted` or `X-Autoreply` headers) → logged, **not** treated as a reply, follow-ups continue.

---

## 8. Compliance — appended by code, not by templates

Every email carries the unsubscribe instruction, a `List-Unsubscribe` header, your postal address, and one line saying why the person was contacted. The system appends them, so a malformed template cannot omit them, and the preview shows them greyed out.

Country rules are not symmetrical. **US (CAN-SPAM)** is opt-out and the most permissive. **UK (PECR / UK GDPR)** allows B2B with sender identification and opt-out. **Australia (Spam Act 2003)** and especially **Canada (CASL)** require consent, with a narrow inferred-consent carve-out for conspicuously published business addresses; CASL carries per-message penalties. Decide whether your lead source clears that bar before uploading a Canadian list. This is a planning summary, not legal advice.

---

## 9. Testing before the first real send

1. Reserving twice for one `(lead, campaign, step)` raises.
2. A `reserved` row older than the timeout becomes `unknown`, never `sent`.
3. Warm-up: per-inbox and account caps enforced; rotation cursor survives restart; the day boundary is computed in your timezone, not UTC.
4. Import: uploading the same sheet twice adds nothing and changes no history; a sheet with 3 new rows adds exactly 3.
5. Follow-up scan: a lead sent 2 days ago is not due; 3 days ago is due; one who replied is never due.
6. Fake Resend client, 50-lead simulated campaign — no address receives a step twice.
7. **Kill the process mid-run (`Ctrl+C`, or pull the power), restart.** No duplicate send, exactly one `unknown` row. This is the test people skip and the one that proves the design.
8. Live send of 3 emails to your own addresses on Gmail, Outlook and Yahoo. Check raw headers: SPF, DKIM, DMARC pass, `List-Unsubscribe` present — and check which folder it landed in. Inbox placement is the only real measure of warm-up; a "delivered" status says nothing about the spam folder.
9. Then one real campaign capped at 5.

---

## 10. Build order, with pass conditions

| # | Step | Pass condition |
|---|---|---|
| 1 | `run.py`, `start.ps1`, `db.py`, migrations, base layout + `style.css` | One command opens the dashboard in the browser |
| 2 | `importer.py` + upload / mapping / preview screens | Test 4 passes; CSV and XLSX both work |
| 3 | Leads table + detail page | Search and filter stay responsive at 5,000 rows |
| 4 | `templating.py` + templates screen + live preview | Missing variable blocks save; preview matches the real send byte for byte |
| 5 | `warmup.py` + inboxes screen | Test 3 passes |
| 6 | `resend_client.py` + fake client + send state machine | Tests 1, 2, 6 pass |
| 7 | `worker.py` + campaigns + **STOP button** | Test 7 passes; STOP halts within 5 seconds and survives a restart |
| 8 | `followups.py` + follow-ups page + auto/manual toggle | Test 5 passes |
| 9 | Dashboard counters and live activity | Numbers match direct database queries |
| 10 | Status polling + `reply_poller.py` | A bounce suppresses; a real reply suppresses; an auto-reply does not |
| 11 | Backups, logs, README runbook | Test 8 passes |
| 12 | Warm-up week 1 begins | First campaign capped at 5, reviewed by hand |

Steps 1–7 are the safety core. No real email before step 7 passes.

---

## 11. Operations

- **Backups** — a daily `sqlite3 sender.db ".backup"` into `state/backups/`, keeping 14, plus a *Back up now* button in Settings. Never copy a live SQLite file with `copy` — a copy taken mid-write can be corrupt.
- **One instance only.** Running two copies on two machines against two database files will double-send, and no code can prevent that. If you move machines, move the whole `state/` folder and run it in one place.
- **Stopping** — the STOP button, or `Ctrl+C` in PowerShell. Both are safe at any moment; the reserve protocol means a half-finished send is detected, not repeated.

---

## 12. Out of scope for v1

- Any public URL, hosted unsubscribe page, or VPS deployment.
- Multi-user accounts.
- Open and click tracking — hurts deliverability during warm-up.
- A/B testing beyond subject-line rotation.
- WhatsApp and SMS.
- Docker, Postgres, Redis, Celery. None are needed at this volume, and each adds a way to fail.

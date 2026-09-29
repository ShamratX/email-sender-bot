"""FastAPI routes. Plain HTML forms, POST then redirect. No JavaScript."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import clock, config_file, db, engine, importer, templating, warmup, worker
from .resend_client import build_client

BASE = Path(__file__).resolve().parent
UPLOADS = BASE.parent / "state" / "uploads"

app = FastAPI(title="Email Sender Bot")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
pages = Jinja2Templates(directory=str(BASE / "templates"))


@app.on_event("startup")
def startup() -> None:
    db.init()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    config_file.sync_templates()
    worker.start()


def page(request: Request, name: str, **context) -> HTMLResponse:
    context.setdefault("sending_enabled", db.get_setting("sending_enabled") == "1")
    context.setdefault("followups_due", len(engine.due_followups()))
    return pages.TemplateResponse(request, name, context)


def back(url: str = "/") -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --- dashboard ------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = db.connect()
    quota = warmup.quota_report()
    events = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT 50"
    ).fetchall()
    attention = conn.execute(
        "SELECT s.*, l.email FROM sends s JOIN leads l ON l.id = s.lead_id "
        "WHERE s.state IN ('unknown','failed') ORDER BY s.id DESC LIMIT 50"
    ).fetchall()
    campaigns = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
    progress = {c["id"]: engine.campaign_progress(c["id"]) for c in campaigns}

    return page(
        request,
        "dashboard.html",
        quota=quota,
        events=events,
        attention=attention,
        campaigns=campaigns,
        progress=progress,
        worker_status=worker.status(),
        mode=db.get_setting("send_mode"),
    )


@app.get("/country-windows", response_class=HTMLResponse)
def country_windows_page(request: Request):
    return page(
        request,
        "country_windows.html",
        country_windows=json.loads(db.get_setting("country_windows") or "{}"),
        country_timezones=json.loads(db.get_setting("country_timezones") or "{}"),
    )


@app.post("/country-windows")
def country_windows_save(
    au_start: str = Form("09:00"), au_end: str = Form("17:00"), au_tz: str = Form("Australia/Sydney"),
    uk_start: str = Form("09:00"), uk_end: str = Form("17:00"), uk_tz: str = Form("Europe/London"),
    ca_start: str = Form("09:00"), ca_end: str = Form("17:00"), ca_tz: str = Form("America/Toronto"),
    us_start: str = Form("09:00"), us_end: str = Form("17:00"), us_tz: str = Form("America/New_York"),
):
    """One place to edit all four countries' send-time windows and their own
    timezones. Any campaign with 'respect per-country windows' turned on reads
    this at send time -- editing it here changes every such campaign at once,
    no per-campaign edit. Each window is in that country's own local time --
    no manual conversion to your own timezone needed."""
    db.set_setting("country_windows", json.dumps({
        "AU": [au_start, au_end], "UK": [uk_start, uk_end],
        "CA": [ca_start, ca_end], "US": [us_start, us_end],
    }))
    db.set_setting("country_timezones", json.dumps({
        "AU": au_tz.strip(), "UK": uk_tz.strip(),
        "CA": ca_tz.strip(), "US": us_tz.strip(),
    }))
    return back("/country-windows")


@app.post("/sending/{action}")
def sending(action: str):
    db.set_setting("sending_enabled", "1" if action == "start" else "0")
    db.log_event("sending_" + ("started" if action == "start" else "stopped"))
    return back("/")


# --- leads and import -----------------------------------------------------

@app.get("/leads", response_class=HTMLResponse)
def leads(request: Request, q: str = "", status: str = ""):
    conn = db.connect()
    where = "WHERE 1=1"
    params: list = []
    if q:
        where += " AND (l.email LIKE ? OR l.custom_fields LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if status == "suppressed":
        where += " AND l.email IN (SELECT email FROM suppression)"
    elif status == "sent":
        where += " AND EXISTS (SELECT 1 FROM sends x WHERE x.lead_id=l.id AND x.state='sent')"
    elif status == "new":
        where += (
            " AND NOT EXISTS (SELECT 1 FROM sends x WHERE x.lead_id=l.id)"
            " AND l.email NOT IN (SELECT email FROM suppression)"
        )

    rows = conn.execute(
        "SELECT l.*, (SELECT reason FROM suppression s WHERE s.email = l.email) AS suppressed, "
        "(SELECT COUNT(*) FROM sends x WHERE x.lead_id = l.id AND x.state='sent') AS sent_count "
        f"FROM leads l {where} ORDER BY l.id DESC LIMIT 300",
        params,
    ).fetchall()
    shown = conn.execute(f"SELECT COUNT(*) AS n FROM leads l {where}", params).fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    return page(request, "leads.html", rows=rows, q=q, status=status, total=total, shown=shown)


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: int):
    conn = db.connect()
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    sends = conn.execute(
        "SELECT s.*, c.name AS campaign FROM sends s LEFT JOIN campaigns c ON c.id=s.campaign_id "
        "WHERE s.lead_id=? ORDER BY s.step",
        (lead_id,),
    ).fetchall()
    return page(
        request,
        "lead_detail.html",
        lead=lead,
        fields=json.loads(lead["custom_fields"]),
        sends=sends,
        suppressed=db.is_suppressed(lead["email"]),
    )


@app.post("/leads/{lead_id}/edit")
async def lead_edit(lead_id: int, request: Request):
    form = await request.form()
    conn = db.connect()
    lead = conn.execute("SELECT custom_fields FROM leads WHERE id=?", (lead_id,)).fetchone()
    if lead is None:
        return back("/leads")
    fields = json.loads(lead["custom_fields"])
    for key in list(fields.keys()):
        posted = form.get(f"field_{key}")
        if posted is not None:
            fields[key] = posted
    conn.execute("UPDATE leads SET custom_fields=? WHERE id=?", (json.dumps(fields), lead_id))
    db.log_event("lead_edited", None, {"lead_id": lead_id})
    return back(f"/leads/{lead_id}")


@app.post("/leads/{lead_id}/delete")
def lead_delete(lead_id: int):
    """Removes the lead record. Send history rows reference lead_id but are kept
    for audit -- they just won't join back to a live lead row afterward."""
    conn = db.connect()
    email = conn.execute("SELECT email FROM leads WHERE id=?", (lead_id,)).fetchone()
    conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))
    conn.execute("DELETE FROM lead_list_members WHERE lead_id=?", (lead_id,))
    db.log_event("lead_deleted", email["email"] if email else None, {"lead_id": lead_id})
    return back("/leads")


def _import_summary(stats_json: str) -> str:
    """Turns the raw stats JSON into a short human line, skipping zero counts."""
    stats = json.loads(stats_json)
    labels = {
        "new": "new", "existing": "already had", "updated": "updated",
        "suppressed": "suppressed", "invalid": "invalid", "duplicate_in_file": "duplicate in file",
    }
    parts = [f"{stats[key]} {label}" for key, label in labels.items() if stats.get(key)]
    return ", ".join(parts) if parts else "nothing changed"


@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    conn = db.connect()
    lists = conn.execute("SELECT * FROM lead_lists ORDER BY name").fetchall()
    history = [
        {**dict(row), "summary": _import_summary(row["stats"])}
        for row in conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 10").fetchall()
    ]
    return page(request, "import.html", lists=lists, history=history, preview=None)


@app.post("/import", response_class=HTMLResponse)
async def import_run(
    request: Request,
    sheet: UploadFile,
    list_name: str = Form(...),
    email_column: str = Form(""),
    on_existing: str = Form("skip"),
    confirm: str = Form(""),
):
    raw = await sheet.read()
    filename = Path(sheet.filename or "upload.csv").name
    saved = UPLOADS / f"{db.utcnow().replace(':', '-')}_{filename}"
    saved.write_bytes(raw)

    parsed = importer.parse(raw, filename)
    column = email_column or importer.guess_email_column(parsed.columns) or ""
    conn = db.connect()
    lists = conn.execute("SELECT * FROM lead_lists ORDER BY name").fetchall()
    history = [
        {**dict(row), "summary": _import_summary(row["stats"])}
        for row in conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 10").fetchall()
    ]

    if not column:
        return page(
            request,
            "import.html",
            lists=lists,
            history=history,
            preview=None,
            error="No email column found. Pick one and upload again.",
            columns=parsed.columns,
        )

    if confirm != "yes":
        stats, sample = importer.preview(parsed, column)
        return page(
            request,
            "import.html",
            lists=lists,
            history=history,
            preview={
                "stats": stats,
                "sample": sample,
                "columns": parsed.columns,
                "column": column,
                "filename": filename,
                "saved": saved.name,
                "list_name": list_name,
                "on_existing": on_existing,
                "total": len(parsed.rows),
            },
        )

    stats = importer.commit(parsed, column, list_name, on_existing)
    db.log_event("import", None, stats.as_dict())
    return back("/leads")


@app.post("/import/confirm")
async def import_confirm(
    saved: str = Form(...),
    column: str = Form(...),
    list_name: str = Form(...),
    on_existing: str = Form("skip"),
):
    path = UPLOADS / saved
    parsed = importer.parse(path.read_bytes(), saved.split("_", 1)[-1])
    stats = importer.commit(parsed, column, list_name, on_existing)
    db.log_event("import", None, stats.as_dict())
    return back("/leads")


# --- templates ------------------------------------------------------------

@app.get("/templates", response_class=HTMLResponse)
def template_list(request: Request):
    conn = db.connect()
    rows = conn.execute("SELECT * FROM templates ORDER BY id").fetchall()
    subjects = {}
    for row in rows:
        subjects[row["id"]] = [
            r["subject"]
            for r in conn.execute(
                "SELECT subject FROM subject_variants WHERE template_id=? ORDER BY id",
                (row["id"],),
            ).fetchall()
        ]
    return page(
        request,
        "templates.html",
        rows=rows,
        subjects=subjects,
        available=importer.custom_field_names(),
    )


@app.post("/templates")
def template_save(
    name: str = Form(...),
    subjects: str = Form(...),
    body: str = Form(...),
    template_id: str = Form(""),
    footer_enabled: str = Form("1"),
    footer_text: str = Form(""),
):
    variants = [s.strip() for s in subjects.splitlines() if s.strip()]
    available = importer.custom_field_names()
    try:
        templating.validate(variants, body, available)
    except templating.TemplateError as exc:
        return back(f"/templates?error={exc}")

    enabled = 1 if footer_enabled == "1" else 0
    footer_value = footer_text.strip() or None

    conn = db.connect()
    if template_id:
        conn.execute(
            "UPDATE templates SET name=?, body=?, footer_enabled=?, footer_text=? WHERE id=?",
            (name, body, enabled, footer_value, int(template_id)),
        )
        tid = int(template_id)
        conn.execute("DELETE FROM subject_variants WHERE template_id=?", (tid,))
    else:
        try:
            cur = conn.execute(
                "INSERT INTO templates (name, body, footer_enabled, footer_text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, body, enabled, footer_value, db.utcnow()),
            )
        except db.sqlite3.IntegrityError:
            return back(f"/templates?error=a template named '{name}' already exists")
        tid = int(cur.lastrowid)
    for variant in variants:
        conn.execute(
            "INSERT INTO subject_variants (template_id, subject) VALUES (?, ?)", (tid, variant)
        )
    config_file.write_back_template(name, variants, body)
    return back("/templates")


@app.post("/templates/{template_id}/delete")
def template_delete(template_id: int):
    """Removes the template and its subject variants. Any campaign step still
    pointing at it will simply skip that step on the next pass (the join in
    engine.candidates() requires the template to exist); past sends keep their
    stored subject/body regardless, since that content is copied into the
    sends row at send time, not looked up live."""
    conn = db.connect()
    conn.execute("DELETE FROM subject_variants WHERE template_id=?", (template_id,))
    conn.execute("DELETE FROM campaign_steps WHERE template_id=?", (template_id,))
    conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
    db.log_event("template_deleted", None, {"template_id": template_id})
    return back("/templates")


@app.get("/templates/{template_id}/preview", response_class=HTMLResponse)
def template_preview(request: Request, template_id: int, lead_id: int = 0):
    conn = db.connect()
    lead = (
        conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if lead_id
        else conn.execute("SELECT * FROM leads ORDER BY id LIMIT 1").fetchone()
    )
    template, variants = engine.template_with_subjects(template_id)
    leads_all = conn.execute("SELECT id, email FROM leads ORDER BY id LIMIT 200").fetchall()
    if lead is None:
        return page(
            request, "preview.html", template=template, subject="", body="",
            lead=None, leads=leads_all,
        )
    subject, body = templating.compose(
        variants, template["body"], lead,
        footer_enabled=bool(template["footer_enabled"]),
        footer_text=template["footer_text"],
    )
    return page(
        request, "preview.html", template=template, subject=subject, body=body,
        lead=lead, leads=leads_all,
    )


# --- campaigns ------------------------------------------------------------

@app.get("/campaigns", response_class=HTMLResponse)
def campaign_list(request: Request, msg: str = "", error: str = ""):
    conn = db.connect()
    rows = conn.execute(
        "SELECT c.*, (SELECT name FROM lead_lists WHERE id=c.list_id) AS list_name, "
        "(SELECT name FROM accounts WHERE id=c.account_id) AS account_name, "
        "(SELECT COUNT(*) FROM sends s WHERE s.campaign_id=c.id AND s.state='sent') AS sent "
        "FROM campaigns c ORDER BY c.id DESC"
    ).fetchall()
    steps = {
        row["id"]: conn.execute(
            "SELECT cs.step, cs.template_id, t.name, t.body, "
            "(SELECT subject FROM subject_variants WHERE template_id=t.id ORDER BY id LIMIT 1) AS subject "
            "FROM campaign_steps cs JOIN templates t ON t.id=cs.template_id "
            "WHERE cs.campaign_id=? ORDER BY cs.step",
            (row["id"],),
        ).fetchall()
        for row in rows
    }

    return page(
        request,
        "campaigns.html",
        rows=rows,
        steps=steps,
        msg=msg,
        error=error,
        inboxes=conn.execute("SELECT * FROM inboxes ORDER BY id").fetchall(),
        accounts=conn.execute("SELECT * FROM accounts ORDER BY name").fetchall(),
    )


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(request: Request, campaign_id: int, msg: str = "", error: str = ""):
    """This campaign's own dashboard: its progress, its inbox usage, its
    activity, its steps -- everything scoped to just this one campaign,
    separate from the shared list/overview page."""
    conn = db.connect()
    campaign = conn.execute(
        "SELECT c.*, (SELECT name FROM lead_lists WHERE id=c.list_id) AS list_name, "
        "(SELECT name FROM accounts WHERE id=c.account_id) AS account_name "
        "FROM campaigns c WHERE c.id=?",
        (campaign_id,),
    ).fetchone()
    if campaign is None:
        return back("/campaigns")

    steps = conn.execute(
        "SELECT cs.step, cs.template_id, t.name, t.body, "
        "(SELECT subject FROM subject_variants WHERE template_id=t.id ORDER BY id LIMIT 1) AS subject "
        "FROM campaign_steps cs JOIN templates t ON t.id=cs.template_id "
        "WHERE cs.campaign_id=? ORDER BY cs.step",
        (campaign_id,),
    ).fetchall()

    if campaign["account_id"]:
        active_inbox_ids = {
            row["id"] for row in conn.execute(
                "SELECT id FROM inboxes WHERE account_id=?", (campaign["account_id"],)
            ).fetchall()
        }
    else:
        active_inbox_ids = {
            int(x) for x in (campaign["allowed_inboxes"] or "").split(",") if x.strip()
        }
    all_inboxes = conn.execute("SELECT * FROM inboxes ORDER BY id").fetchall()
    scoped_inboxes = (
        [i for i in all_inboxes if i["id"] in active_inbox_ids] if active_inbox_ids else all_inboxes
    )
    quota = warmup.quota_report()
    inbox_usage = [
        row for row in quota["inboxes"]
        if not active_inbox_ids or row["id"] in active_inbox_ids
    ]
    # this campaign's own account cap/usage, not the global default bucket --
    # a campaign on "Domain B" must see Domain B's own plan limit, not Domain A's
    campaign_account_id = campaign["account_id"]
    account_used = warmup.account_sent_today(campaign_account_id)
    account_cap_value = warmup.account_cap_for(campaign_account_id)

    recent_sends = conn.execute(
        "SELECT s.*, l.email FROM sends s JOIN leads l ON l.id=s.lead_id "
        "WHERE s.campaign_id=? ORDER BY s.id DESC LIMIT 30",
        (campaign_id,),
    ).fetchall()
    attention = conn.execute(
        "SELECT s.*, l.email FROM sends s JOIN leads l ON l.id=s.lead_id "
        "WHERE s.campaign_id=? AND s.state IN ('unknown','failed') ORDER BY s.id DESC LIMIT 30",
        (campaign_id,),
    ).fetchall()

    return page(
        request,
        "campaign_detail.html",
        c=campaign,
        steps=steps,
        progress=engine.campaign_progress(campaign_id),
        inbox_usage=inbox_usage,
        account_used=account_used,
        account_cap=account_cap_value,
        recent_sends=recent_sends,
        attention=attention,
        inboxes=all_inboxes,
        accounts=conn.execute("SELECT * FROM accounts ORDER BY name").fetchall(),
        active_inbox_ids=active_inbox_ids,
        msg=msg,
        error=error,
    )


@app.post("/campaigns")
async def campaign_create(
    request: Request,
    name: str = Form(...),
    sheet: UploadFile | None = None,
    initial_subject: str = Form(...),
    initial_body: str = Form(...),
    followup1_subject: str = Form(""),
    followup1_body: str = Form(""),
    followup2_subject: str = Form(""),
    followup2_body: str = Form(""),
    followup3_subject: str = Form(""),
    followup3_body: str = Form(""),
    gap_days: int = Form(3),
    followup_mode: str = Form("manual"),
    daily_cap: str = Form(""),
    windowed_countries: list[str] = Form([]),
    allowed_inboxes: list[str] = Form([]),
    account_id: str = Form(""),
):
    """Self-contained campaign creation, like Instantly/Lemlist: leads and
    every email step are entered right here, not picked from a separate
    shared Templates/Upload page. A dedicated list and dedicated templates
    are created behind the scenes, named after this campaign, so nothing
    needs to be cross-referenced across pages afterward."""
    conn = db.connect()
    if conn.execute("SELECT 1 FROM campaigns WHERE name=?", (name,)).fetchone():
        return back(f"/campaigns?error=a campaign named '{name}' already exists")

    import_stats = None
    if sheet is not None and sheet.filename:
        raw = await sheet.read()
        if raw:
            filename = Path(sheet.filename).name
            saved = UPLOADS / f"{db.utcnow().replace(':', '-')}_{filename}"
            saved.write_bytes(raw)
            parsed = importer.parse(raw, filename)
            column = importer.guess_email_column(parsed.columns)
            if not column:
                return back(
                    f"/campaigns?error=Could not find an email column in {filename}. "
                    f"Columns seen: {', '.join(parsed.columns)}"
                )
            import_stats = importer.commit(parsed, column, name, on_existing="skip")

    list_row = conn.execute("SELECT id FROM lead_lists WHERE name=?", (name,)).fetchone()
    if list_row is None:
        list_id = importer.ensure_list(name)  # empty list -- leads can be added later
    else:
        list_id = list_row["id"]

    available = importer.custom_field_names(list_id)
    steps = [(0, initial_subject, initial_body)]
    for n, (subj, body) in enumerate(
        [(followup1_subject, followup1_body), (followup2_subject, followup2_body),
         (followup3_subject, followup3_body)],
        start=1,
    ):
        if subj.strip() or body.strip():
            steps.append((n, subj, body))

    for step, subject, body in steps:
        try:
            templating.validate([subject], body, available)
        except templating.TemplateError as exc:
            return back(f"/campaigns?error=Step {step} template problem: {exc}")

    cur = conn.execute(
        "INSERT INTO campaigns (name, list_id, gap_days, window_start, window_end, "
        "followup_mode, daily_cap, windowed_countries, allowed_inboxes, account_id, created_at) "
        "VALUES (?, ?, ?, '00:00', '23:59', ?, ?, ?, ?, ?, ?)",
        (
            name, list_id, gap_days, followup_mode,
            int(daily_cap) if daily_cap.strip() else None,
            ",".join(c.strip().upper() for c in windowed_countries if c.strip()),
            ",".join(i.strip() for i in allowed_inboxes if i.strip()),
            int(account_id) if account_id.strip() else None,
            db.utcnow(),
        ),
    )
    campaign_id = int(cur.lastrowid)

    for step, subject, body in steps:
        template_name = f"{name} :: step {step}"
        tcur = conn.execute(
            "INSERT INTO templates (name, body, created_at) VALUES (?, ?, ?)",
            (template_name, body, db.utcnow()),
        )
        template_id = int(tcur.lastrowid)
        conn.execute(
            "INSERT INTO subject_variants (template_id, subject) VALUES (?, ?)",
            (template_id, subject),
        )
        conn.execute(
            "INSERT INTO campaign_steps (campaign_id, step, template_id) VALUES (?, ?, ?)",
            (campaign_id, step, template_id),
        )

    msg = f"Campaign '{name}' created with {len(steps)} step(s)."
    if import_stats:
        msg += f" Leads: {import_stats.new} new, {import_stats.existing} already had, {import_stats.invalid} invalid."
    return back(f"/campaigns/{campaign_id}?msg={msg}")


@app.post("/campaigns/{campaign_id}/steps")
def campaign_update_steps(
    campaign_id: int,
    initial_subject: str = Form(...),
    initial_body: str = Form(...),
    followup1_subject: str = Form(""),
    followup1_body: str = Form(""),
    followup2_subject: str = Form(""),
    followup2_body: str = Form(""),
    followup3_subject: str = Form(""),
    followup3_body: str = Form(""),
    gap_days: int = Form(3),
    daily_cap: str = Form(""),
    windowed_countries: list[str] = Form([]),
    allowed_inboxes: list[str] = Form([]),
    account_id: str = Form(""),
):
    """Edits each step's subject/body in place, inline, no dropdown picker --
    same self-contained shape as creating a campaign. Leads already sent a
    step keep the body actually sent to them (sends.body_rendered is a
    permanent record); this only changes what happens to leads not yet there.
    """
    conn = db.connect()
    campaign = conn.execute("SELECT list_id FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if campaign is None:
        return back("/campaigns")
    available = importer.custom_field_names(campaign["list_id"])

    conn.execute(
        "UPDATE campaigns SET gap_days=?, daily_cap=?, windowed_countries=?, "
        "allowed_inboxes=?, account_id=? WHERE id=?",
        (
            gap_days,
            int(daily_cap) if daily_cap.strip() else None,
            ",".join(c.strip().upper() for c in windowed_countries if c.strip()),
            ",".join(i.strip() for i in allowed_inboxes if i.strip()),
            int(account_id) if account_id.strip() else None,
            campaign_id,
        ),
    )

    steps = [(0, initial_subject, initial_body)]
    for n, (subj, body) in enumerate(
        [(followup1_subject, followup1_body), (followup2_subject, followup2_body),
         (followup3_subject, followup3_body)],
        start=1,
    ):
        if subj.strip() or body.strip():
            steps.append((n, subj, body))
    wanted_steps = {s[0] for s in steps}

    for step, subject, body in steps:
        try:
            templating.validate([subject], body, available)
        except templating.TemplateError as exc:
            return back(f"/campaigns/{campaign_id}?error=Step {step} template problem: {exc}")

    existing = {
        row["step"]: row["template_id"]
        for row in conn.execute(
            "SELECT step, template_id FROM campaign_steps WHERE campaign_id=?", (campaign_id,)
        ).fetchall()
    }
    name_row = conn.execute("SELECT name FROM campaigns WHERE id=?", (campaign_id,)).fetchone()

    for step, subject, body in steps:
        if step in existing:
            tid = existing[step]
            conn.execute("UPDATE templates SET body=? WHERE id=?", (body, tid))
            conn.execute("DELETE FROM subject_variants WHERE template_id=?", (tid,))
            conn.execute(
                "INSERT INTO subject_variants (template_id, subject) VALUES (?, ?)", (tid, subject)
            )
        else:
            tcur = conn.execute(
                "INSERT INTO templates (name, body, created_at) VALUES (?, ?, ?)",
                (f"{name_row['name']} :: step {step}", body, db.utcnow()),
            )
            tid = int(tcur.lastrowid)
            conn.execute(
                "INSERT INTO subject_variants (template_id, subject) VALUES (?, ?)", (tid, subject)
            )
            conn.execute(
                "INSERT INTO campaign_steps (campaign_id, step, template_id) VALUES (?, ?, ?)",
                (campaign_id, step, tid),
            )

    for step in list(existing):
        if step not in wanted_steps:
            conn.execute(
                "DELETE FROM campaign_steps WHERE campaign_id=? AND step=?", (campaign_id, step)
            )

    db.log_event("campaign_steps_updated", None, {"campaign_id": campaign_id})
    return back(f"/campaigns/{campaign_id}?msg=Campaign '{name_row['name']}' updated.")


@app.post("/campaigns/{campaign_id}/add-leads")
async def campaign_add_leads(campaign_id: int, sheet: UploadFile):
    """Adds more leads straight into this campaign's own list -- the whole
    point of the self-contained design is never needing to go to a separate
    Upload page for a campaign already set up."""
    conn = db.connect()
    campaign = conn.execute("SELECT name, list_id FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if campaign is None:
        return back("/campaigns")
    raw = await sheet.read()
    if not raw:
        return back("/campaigns")
    filename = Path(sheet.filename or "upload.csv").name
    saved = UPLOADS / f"{db.utcnow().replace(':', '-')}_{filename}"
    saved.write_bytes(raw)
    parsed = importer.parse(raw, filename)
    column = importer.guess_email_column(parsed.columns)
    if not column:
        return back(f"/campaigns/{campaign_id}?error=Could not find an email column in {filename}.")
    list_name = conn.execute(
        "SELECT name FROM lead_lists WHERE id=?", (campaign["list_id"],)
    ).fetchone()["name"]
    stats = importer.commit(parsed, column, list_name, on_existing="skip")
    db.log_event("import", None, stats.as_dict())
    return back(
        f"/campaigns/{campaign_id}?msg=Added to '{campaign['name']}': {stats.new} new, "
        f"{stats.existing} already had, {stats.invalid} invalid."
    )


@app.post("/campaigns/{campaign_id}/delete")
def campaign_delete(campaign_id: int):
    """Removes the campaign and its step config. Sends already made under it
    stay in the sends table for audit (lead history), just no longer joined
    to a live campaign row."""
    conn = db.connect()
    conn.execute("DELETE FROM campaign_steps WHERE campaign_id=?", (campaign_id,))
    conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
    db.log_event("campaign_deleted", None, {"campaign_id": campaign_id})
    return back("/campaigns")


@app.post("/campaigns/{campaign_id}/{action}")
def campaign_action(campaign_id: int, action: str):
    conn = db.connect()
    if action in ("run", "pause", "done"):
        state = {"run": "running", "pause": "paused", "done": "done"}[action]
        conn.execute("UPDATE campaigns SET state=? WHERE id=?", (state, campaign_id))
    elif action == "release-followups":
        conn.execute("UPDATE campaigns SET followups_released=1 WHERE id=?", (campaign_id,))
    elif action == "auto":
        conn.execute("UPDATE campaigns SET followup_mode='auto' WHERE id=?", (campaign_id,))
    elif action == "manual":
        conn.execute("UPDATE campaigns SET followup_mode='manual' WHERE id=?", (campaign_id,))
    return back(f"/campaigns/{campaign_id}")


# --- follow-ups -----------------------------------------------------------

@app.get("/followups", response_class=HTMLResponse)
def followups(request: Request):
    return page(request, "followups.html", rows=engine.due_followups())


@app.post("/followups/send-now")
def followups_send_now():
    db.connect().execute(
        "UPDATE campaigns SET followups_released=1 WHERE state='running'"
    )
    db.log_event("followups_released")
    return back("/followups")


# --- inboxes, warm-up, settings, suppression ------------------------------

@app.get("/inboxes", response_class=HTMLResponse)
def inboxes(request: Request):
    conn = db.connect()
    accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
    return page(
        request,
        "inboxes.html",
        rows=conn.execute(
            "SELECT i.*, a.name AS account_name FROM inboxes i "
            "LEFT JOIN accounts a ON a.id=i.account_id ORDER BY i.id"
        ).fetchall(),
        accounts=accounts,
        account_schedules={a["id"]: json.loads(a["warmup_schedule"] or "{}") for a in accounts},
        account_weeks={a["id"]: warmup.warmup_week_for(a["id"]) for a in accounts},
        quota=warmup.quota_report(),
        schedule=json.loads(db.get_setting("warmup_schedule") or "{}"),
        start_date=db.get_setting("warmup_start_date"),
        cap=warmup.account_cap(),
    )


@app.post("/accounts/{account_id}/warmup")
def account_warmup_save(
    account_id: int,
    warmup_start_date: str = Form(""),
    week1: str = Form(""), week2: str = Form(""), week3: str = Form(""), week4: str = Form(""),
    daily_cap: str = Form(""),
):
    """This account's own warm-up clock, schedule and plan cap -- separate
    from the global defaults, so a second domain doesn't inherit the first
    domain's week number or a mismatched plan limit."""
    schedule = {}
    for key, value in [("1", week1), ("2", week2), ("3", week3), ("4+", week4)]:
        if value.strip():
            schedule[key] = int(value)
    db.connect().execute(
        "UPDATE accounts SET warmup_start_date=?, warmup_schedule=?, daily_cap=? WHERE id=?",
        (
            warmup_start_date.strip(),
            json.dumps(schedule) if schedule else "",
            int(daily_cap) if daily_cap.strip() else None,
            account_id,
        ),
    )
    return back("/inboxes")


@app.post("/accounts")
def account_add(name: str = Form(...), api_key: str = Form(...)):
    try:
        db.connect().execute(
            "INSERT INTO accounts (name, api_key, created_at) VALUES (?, ?, ?)",
            (name.strip(), api_key.strip(), db.utcnow()),
        )
    except db.sqlite3.IntegrityError:
        return back(f"/inboxes?error=an account named '{name}' already exists")
    return back("/inboxes")


@app.post("/accounts/{account_id}/delete")
def account_delete(account_id: int):
    """Inboxes on this account fall back to the shared .env key, not deleted."""
    conn = db.connect()
    conn.execute("UPDATE inboxes SET account_id=NULL WHERE account_id=?", (account_id,))
    conn.execute("UPDATE campaigns SET account_id=NULL WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    return back("/inboxes")


@app.post("/inboxes")
def inbox_add(
    email: str = Form(...), name: str = Form(...),
    api_key: str = Form(""), account_id: str = Form(""),
):
    db.connect().execute(
        "INSERT OR IGNORE INTO inboxes (email, name, api_key, account_id) VALUES (?, ?, ?, ?)",
        (email.strip().lower(), name, api_key.strip() or None, int(account_id) if account_id.strip() else None),
    )
    return back("/inboxes")


@app.post("/inboxes/{inbox_id}/toggle")
def inbox_toggle(inbox_id: int):
    db.connect().execute(
        "UPDATE inboxes SET enabled = 1 - enabled WHERE id=?", (inbox_id,)
    )
    return back("/inboxes")


@app.post("/inboxes/{inbox_id}/edit")
def inbox_edit(
    inbox_id: int, email: str = Form(...), name: str = Form(...),
    api_key: str = Form(""), account_id: str = Form(""),
):
    try:
        db.connect().execute(
            "UPDATE inboxes SET email=?, name=?, api_key=?, account_id=? WHERE id=?",
            (
                email.strip().lower(), name.strip(), api_key.strip() or None,
                int(account_id) if account_id.strip() else None, inbox_id,
            ),
        )
    except db.sqlite3.IntegrityError:
        return back(f"/inboxes?error=another inbox already uses {email.strip().lower()}")
    return back("/inboxes")


@app.post("/inboxes/{inbox_id}/delete")
def inbox_delete(inbox_id: int):
    """Removes the inbox from rotation. Sends already made from it keep their
    record (sends.inbox_id becomes an orphaned reference, harmless for audit)."""
    conn = db.connect()
    row = conn.execute("SELECT email FROM inboxes WHERE id=?", (inbox_id,)).fetchone()
    conn.execute("DELETE FROM inboxes WHERE id=?", (inbox_id,))
    db.log_event("inbox_deleted", row["email"] if row else None, {})
    return back("/inboxes")


@app.post("/warmup")
def warmup_save(
    start_date: str = Form(""),
    week1: int = Form(6),
    week2: int = Form(12),
    week3: int = Form(20),
    week4: int = Form(35),
    account_cap: int = Form(100),
):
    db.set_setting("warmup_start_date", start_date.strip())
    db.set_setting(
        "warmup_schedule", json.dumps({"1": week1, "2": week2, "3": week3, "4+": week4})
    )
    db.set_setting("account_daily_cap", account_cap)
    return back("/inboxes")


@app.get("/suppression", response_class=HTMLResponse)
def suppression(request: Request, q: str = ""):
    sql = "SELECT * FROM suppression"
    params: list = []
    if q:
        sql += " WHERE email LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY created_at DESC LIMIT 500"
    return page(
        request,
        "suppression.html",
        rows=db.connect().execute(sql, params).fetchall(),
        q=q,
        total=db.connect().execute("SELECT COUNT(*) AS n FROM suppression").fetchone()["n"],
    )


@app.post("/suppression")
def suppression_add(emails: str = Form(...), reason: str = Form("manual")):
    added = 0
    for line in emails.replace(",", "\n").splitlines():
        email = line.strip().lower()
        if email and db.suppress(email, reason):
            added += 1
    return back("/suppression")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    keys = [
        "timezone",
        "send_mode",
        "delay_min_seconds",
        "delay_max_seconds",
        "postal_address",
        "unsubscribe_email",
        "verify_before_send",
        "skip_weekends",
    ]
    return page(
        request,
        "settings.html",
        values={k: db.get_setting(k) for k in keys},
        has_key=bool(__import__("os").environ.get("RESEND_API_KEY")),
    )


@app.post("/settings")
def settings_save(
    timezone: str = Form("Asia/Dhaka"),
    send_mode: str = Form("fake"),
    delay_min_seconds: int = Form(45),
    delay_max_seconds: int = Form(180),
    postal_address: str = Form(""),
    unsubscribe_email: str = Form(""),
    verify_before_send: str = Form("0"),
    skip_weekends: str = Form("0"),
):
    for key, value in {
        "timezone": timezone,
        "send_mode": send_mode,
        "delay_min_seconds": delay_min_seconds,
        "delay_max_seconds": delay_max_seconds,
        "postal_address": postal_address,
        "unsubscribe_email": unsubscribe_email,
        "verify_before_send": "1" if verify_before_send == "1" else "0",
        "skip_weekends": "1" if skip_weekends == "1" else "0",
    }.items():
        db.set_setting(key, value)
    return back("/settings")


# --- needs attention ------------------------------------------------------

@app.post("/sends/{send_id}/resolve")
def resolve_unknown(send_id: int, outcome: str = Form("sent")):
    """A human decides what an 'unknown' row really was. No automatic guessing."""
    state = "sent" if outcome == "sent" else "failed"
    db.connect().execute(
        "UPDATE sends SET state=?, error='resolved by operator' WHERE id=? AND state='unknown'",
        (state, send_id),
    )
    return back("/")

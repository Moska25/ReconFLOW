"""FastAPI routes. Thin on purpose: every number and every rule comes from a module."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db, exceptions as exc, generate, ingest, matching, reporting
from app.controls import (ACTORS, APPROVAL_THRESHOLD_MINOR, ControlError, DEFAULT_ACTOR,
                          ROLES, audit, blocked_reason, can, decide_approval,
                          needs_approval, require, role_of, submit_for_approval)

BASE = Path(__file__).resolve().parent
app = FastAPI(title="ReconFLOW")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

PROJECT = "ReconFLOW"
TAGLINE = "Reconciliation and exception operations"
DESCRIPTION = ("Invoice-to-payment reconciliation with a typed exception queue, "
               "maker-checker approval and a database-enforced append-only audit trail.")
NAV = [("/", "Dashboard"), ("/workspace", "Workspace"), ("/exceptions", "Exceptions"),
       ("/approvals", "Approvals"), ("/audit", "Audit"), ("/import", "Import"),
       ("/quality", "Quality")]

templates.env.filters["money"] = ingest.fmt_money
templates.env.globals["changed"] = reporting.state_diff
templates.env.globals.update(
    reasons=exc.REASONS, buckets=exc.BUCKETS, roles=ROLES, actors=ACTORS,
    rate_date=ingest.RATE_DATE, rates=ingest.RATES,
    threshold=APPROVAL_THRESHOLD_MINOR, can=can,
    # constants the pages draw against, so no template restates a threshold
    cfg=matching.DEFAULT_CONFIG, sla_days=exc.SLA_DAYS,
)


ERRORS = {
    404: ("No such page",
          "That route does not exist in this application.",
          "The address was typed, linked or guessed wrong. Nothing was changed."),
    405: ("Wrong method for this route",
          "This address exists, but not for the method the request used.",
          "A form was posted to a page that only answers GET, or the reverse."),
    500: ("Something failed on the server",
          "The request reached the application and the application could not finish it.",
          "Nothing was written. The server log holds the traceback."),
}


def get_conn():
    conn = db.connect()
    db.bootstrap(conn)
    if os.environ.get("VERCEL") and not db.is_seeded(conn):
        # ponytail: a deployed instance starts with an empty /tmp database, so the first
        # request seeds it (0.7s from the bundled CSVs). Locally run.sh does the seeding,
        # and the empty-database test relies on an unseeded database staying unseeded.
        from app import seed as _seed
        conn.close()
        _seed.seed()
        conn = db.connect()
    return conn


def ctx(request: Request, active: str, **extra) -> dict:
    """The single place page furniture is assembled, so every page matches."""
    actor = request.cookies.get("rf_actor", DEFAULT_ACTOR)
    if actor not in ACTORS:
        actor = DEFAULT_ACTOR
    role = role_of(actor)
    conn = get_conn()
    try:
        at_risk = reporting.value_at_risk(conn)
    finally:
        conn.close()
    base = {
        "request": request,
        "project_name": PROJECT,
        "project_tagline": TAGLINE,
        "project_description": DESCRIPTION,
        "nav": NAV,
        "active": active,
        "footer_note": f"Synthetic dataset, as of {generate.AS_OF.isoformat()} - "
                       f"FX fixed at {ingest.RATE_DATE}",
        "actor": actor,
        "role": role,
        "role_label": ROLES[role]["label"],
        "role_note": ROLES[role]["note"],
        "msg": request.query_params.get("msg", ""),
        "level": request.query_params.get("level", "info"),
        "as_of": generate.AS_OF,
        "at_risk": at_risk,
    }
    base.update(extra)
    return base


def back(url: str, msg: str = "", level: str = "info") -> RedirectResponse:
    if msg:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode({'msg': msg, 'level': level})}"
    return RedirectResponse(url, status_code=303)


def render(name: str, request: Request, active: str, **extra):
    context = ctx(request, active, **extra)
    context.pop("request", None)
    return templates.TemplateResponse(request=request, name=name, context=context)


@app.exception_handler(StarletteHTTPException)
def http_error(request: Request, exc: StarletteHTTPException):
    """Answer errors in the application's own shell, with the real status code.

    FastAPI's default is a JSON body, which is right for an API and wrong for a
    page a person reached by mistake. The status code is passed through unchanged:
    a 404 that answers 200 misleads monitoring as much as it misleads the reader.
    """
    heading, explain, detail = ERRORS.get(
        exc.status_code,
        ("Request refused", "The server would not answer this request.", str(exc.detail)))
    response = render("error.html", request, "", code=exc.status_code,
                      heading=heading, explain=explain, detail=detail)
    response.status_code = exc.status_code
    return response


# ------------------------------------------------------------------------ dashboard

@app.get("/")
def dashboard(request: Request):
    conn = get_conn()
    try:
        data = reporting.dashboard(conn, generate.AS_OF)
        return render("dashboard.html", request, "/", d=data,
                      manifest=generate.load_manifest())
    finally:
        conn.close()


# ------------------------------------------------------------------------ workspace

@app.get("/workspace")
def workspace(request: Request, view: str = "matches", status: str = "", rule: str = "",
              currency: str = "", q: str = ""):
    conn = get_conn()
    try:
        return render(
            "workspace.html", request, "/workspace", view=view,
            f={"status": status, "rule": rule, "currency": currency, "q": q},
            rows=reporting.matches(conn, status, rule, currency, q) if view == "matches" else [],
            invoices=reporting.open_invoices(conn) if view == "invoices" else [],
            payments=reporting.open_payments(conn) if view == "payments" else [],
            counts=reporting.counts(conn),
            rules=reporting.distinct(conn, "matches", "rule"),
            currencies=reporting.distinct(conn, "matches", "currency"),
        )
    finally:
        conn.close()


@app.get("/workspace/match/{match_id}")
def match_detail(request: Request, match_id: int):
    conn = get_conn()
    try:
        row = reporting.match_detail(conn, match_id)
        if row is None:
            return back("/workspace", f"Match {match_id} does not exist.", "warn")
        return render("match_detail.html", request, "/workspace", m=row,
                      cfg=matching.DEFAULT_CONFIG)
    finally:
        conn.close()


@app.post("/workspace/match/{match_id}/confirm")
def confirm_match(request: Request, match_id: int, decision: str = Form(...),
                  note: str = Form("")):
    conn = get_conn()
    try:
        actor = request.cookies.get("rf_actor", DEFAULT_ACTOR)
        role = role_of(actor)
        row = reporting.match_detail(conn, match_id)
        if row is None:
            return back("/workspace", f"Match {match_id} does not exist.", "warn")
        try:
            require(role, "propose", f"confirm match {match_id}")
        except ControlError as err:
            return back(f"/workspace/match/{match_id}", str(err), "warn")

        if decision == "confirm" and needs_approval("MANUAL_MATCH", abs(row["payment_minor"])):
            approval_id = submit_for_approval(
                conn, action_type="MANUAL_MATCH", entity_type="match", entity_id=match_id,
                entity_label=f"{row['invoice_label']} / {row['payment_label']}",
                payload={"note": note, "confidence": row["confidence"]},
                amount_minor=abs(row["payment_minor"]), maker=actor, maker_role=role)
            return back("/approvals",
                        f"Above the {ingest.fmt_money(APPROVAL_THRESHOLD_MINOR)} GEL threshold, "
                        f"so approval {approval_id} was raised instead of posting directly.",
                        "warn")

        new_status = "confirmed" if decision == "confirm" else "rejected"
        conn.execute("UPDATE matches SET status = ?, method = 'manual' WHERE id = ?",
                     (new_status, match_id))
        conn.commit()
        audit(conn, actor, role, f"MATCH_{new_status.upper()}", "match", match_id,
              before={"status": row["status"]}, after={"status": new_status, "note": note})
        return back(f"/workspace/match/{match_id}", f"Match {new_status}.", "info")
    finally:
        conn.close()


# ----------------------------------------------------------------------- exceptions

@app.get("/exceptions")
def exception_queue(request: Request, reason: str = "", severity: str = "", status: str = "",
                    bucket: str = "", assignee: str = ""):
    conn = get_conn()
    try:
        return render(
            "exceptions.html", request, "/exceptions",
            f={"reason": reason, "severity": severity, "status": status,
               "bucket": bucket, "assignee": assignee},
            rows=reporting.exception_list(conn, reason, severity, status, bucket, assignee),
            summary=reporting.exceptions_by_reason(conn),
            ageing=reporting.ageing(conn),
            assignees=reporting.distinct(conn, "exceptions", "assignee"),
        )
    finally:
        conn.close()


@app.get("/exceptions/{exception_id}")
def exception_detail(request: Request, exception_id: int):
    conn = get_conn()
    try:
        row = reporting.exception_detail(conn, exception_id)
        if row is None:
            return back("/exceptions", f"Exception {exception_id} does not exist.", "warn")
        return render("exception_detail.html", request, "/exceptions", e=row,
                      assignees=exc.ASSIGNEES)
    finally:
        conn.close()


@app.post("/exceptions/{exception_id}/action")
def exception_action(request: Request, exception_id: int, action: str = Form(...),
                     note: str = Form(""), resolution: str = Form(""),
                     assignee: str = Form("")):
    conn = get_conn()
    url = f"/exceptions/{exception_id}"
    try:
        actor = request.cookies.get("rf_actor", DEFAULT_ACTOR)
        role = role_of(actor)
        row = reporting.exception_detail(conn, exception_id)
        if row is None:
            return back("/exceptions", f"Exception {exception_id} does not exist.", "warn")
        try:
            if action == "assign":
                exc.assign(conn, exception_id, assignee, actor, role)
                return back(url, f"Reassigned to {assignee}.", "info")
            if action in ("investigating", "resolved"):
                exc.transition(conn, exception_id, action, actor, role, note, resolution)
                return back(url, f"Exception moved to {action}.", "info")
            if action == "write_off":
                amount = row["value_minor"]
                if not needs_approval("WRITE_OFF", amount):
                    exc.transition(conn, exception_id, "resolved", actor, role,
                                   note or "Written off below the approval threshold.",
                                   resolution or "Written off")
                    return back(url, "Below the approval threshold, so it was written off "
                                     "directly and logged.", "info")
                approval_id = submit_for_approval(
                    conn, action_type="WRITE_OFF", entity_type="exception",
                    entity_id=exception_id, entity_label=row["entity_label"],
                    payload={"note": note, "reason_code": row["reason_code"]},
                    amount_minor=amount, maker=actor, maker_role=role)
                return back("/approvals",
                            f"Write-off of GEL {ingest.fmt_money(amount)} exceeds the "
                            f"threshold. Approval {approval_id} is pending a second person.",
                            "warn")
            return back(url, f"Unknown action '{action}'.", "warn")
        except ControlError as err:
            return back(url, str(err), "warn")
    finally:
        conn.close()


# ------------------------------------------------------------------------ approvals

@app.get("/approvals")
def approvals(request: Request, status: str = ""):
    conn = get_conn()
    try:
        actor = request.cookies.get("rf_actor", DEFAULT_ACTOR)
        role = role_of(actor)
        sql = "SELECT * FROM approvals"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY status='pending' DESC, id DESC",
                                              params)]
        for row in rows:
            row["payload"] = json.loads(row["payload"] or "{}")
            row["blocked"] = blocked_reason(row, actor, role)
        return render("approvals.html", request, "/approvals", rows=rows,
                      f={"status": status})
    finally:
        conn.close()


@app.post("/approvals/{approval_id}/decide")
def decide(request: Request, approval_id: int, decision: str = Form(...),
           note: str = Form("")):
    conn = get_conn()
    try:
        actor = request.cookies.get("rf_actor", DEFAULT_ACTOR)
        role = role_of(actor)
        try:
            decide_approval(conn, approval_id, decision, actor, role, note)
        except ControlError as err:
            return back("/approvals", str(err), "warn")
        return back("/approvals", f"Approval {approval_id} {decision}.", "info")
    finally:
        conn.close()


# ---------------------------------------------------------------------------- audit

@app.get("/audit")
def audit_page(request: Request, actor: str = "", action: str = "", entity_type: str = ""):
    conn = get_conn()
    try:
        return render("audit.html", request, "/audit",
                      f={"actor": actor, "action": action, "entity_type": entity_type},
                      rows=reporting.audit_list(conn, actor, action, entity_type),
                      # not "actors": that name is a template global used by the role bar
                      actor_options=reporting.distinct(conn, "audit_log", "actor"),
                      actions=reporting.distinct(conn, "audit_log", "action"),
                      entities=reporting.distinct(conn, "audit_log", "entity_type"),
                      total=conn.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"])
    finally:
        conn.close()


# --------------------------------------------------------------------------- import

@app.get("/import")
def import_page(request: Request):
    conn = get_conn()
    try:
        batches = [dict(r) for r in conn.execute(
            "SELECT * FROM import_batches ORDER BY id DESC")]
        for batch in batches:
            batch["rejects"] = [dict(r) for r in conn.execute(
                "SELECT * FROM rejected_rows WHERE batch_id = ? ORDER BY row_no",
                (batch["id"],))]
        return render("import.html", request, "/import", batches=batches,
                      health=reporting.import_health(conn),
                      manifest=generate.load_manifest())
    finally:
        conn.close()


@app.post("/import/upload")
async def upload(request: Request, kind: str = Form("payments"),
                 upload_file: UploadFile = File(...)):
    conn = get_conn()
    try:
        actor = request.cookies.get("rf_actor", DEFAULT_ACTOR)
        role = role_of(actor)
        content = await upload_file.read()
        if not content:
            return back("/import", "The uploaded file was empty.", "warn")
        try:
            batch = ingest.import_bytes(conn, upload_file.filename or "upload.csv",
                                        content, kind, actor, role)
        except ControlError as err:
            return back("/import", str(err), "warn")
        if batch["status"] == "duplicate":
            return back("/import",
                        f"Identical content was already imported, so batch {batch['id']} "
                        f"posted nothing. {batch['rows_dupe']} rows skipped.", "warn")
        if batch["status"] == "rejected":
            return back("/import", f"Batch {batch['id']} rejected: required columns missing.",
                        "warn")
        return back("/import",
                    f"Batch {batch['id']}: {batch['rows_accepted']} accepted, "
                    f"{batch['rows_rejected']} rejected, {batch['rows_dupe']} already known. "
                    f"Re-run matching to apply them.", "info")
    finally:
        conn.close()


@app.post("/import/rematch")
def rematch(request: Request):
    conn = get_conn()
    try:
        actor = request.cookies.get("rf_actor", DEFAULT_ACTOR)
        role = role_of(actor)
        try:
            stats = matching.run_matching(conn, actor=actor, role=role)
        except ControlError as err:
            return back("/import", str(err), "warn")
        exc.rebuild(conn, generate.AS_OF, actor, role)
        return back("/import",
                    f"Matching re-run: {stats['auto_match_rate']}% of payments matched "
                    f"automatically.", "info")
    finally:
        conn.close()


# -------------------------------------------------------------------------- quality

# What each test module is there to protect. The counts and outcomes come from the
# run itself; only these sentences are written by hand, and none of them claims a
# result - they say what the area is, not whether it passed.
AREAS = [
    ("tests/test_matching.py", "The matching engine",
     "Rules in priority order, the tolerance boundary, grouping, determinism, and the "
     "ceiling that stops a payment posting on an amount coincidence."),
    ("tests/test_controls.py", "Segregation of duties",
     "Maker-checker refusing self-approval, role permissions, and the database-enforced "
     "append-only audit trail."),
    ("tests/test_ingest.py", "Defensive import",
     "Both decimal conventions, column aliasing, content-hash idempotency, and per-row "
     "rejection with a reason."),
    ("tests/test_statements.py", "Bank statement formats",
     "MT940 and CAMT.053 parsed to the same payments, including the debit indicator that "
     "decides whether cash came in or went out."),
    ("tests/test_exceptions.py", "The exception taxonomy",
     "Every reason code detected, ageing buckets at their edges, SLA thresholds, and the "
     "resolution lifecycle."),
    ("tests/test_app.py", "The web layer",
     "Every route serving, and the refusals driven through the real HTTP forms rather "
     "than asserted in isolation."),
]


@app.get("/quality")
def quality(request: Request):
    path = db.DATA_DIR / "quality.json"
    report = {}
    if path.exists():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}

    by_module = report.get("by_module", {})
    areas = []
    for module, title, protects in AREAS:
        cases = by_module.get(module, [])
        if not cases:
            continue
        areas.append({
            "module": module, "title": title, "protects": protects, "cases": cases,
            "passed": sum(1 for c in cases if c["outcome"] == "passed"),
            "failed": sum(1 for c in cases if c["outcome"] != "passed"),
        })
    # anything the list above does not name still shows, so the page cannot hide a module
    for module, cases in by_module.items():
        if not any(a["module"] == module for a in areas):
            areas.append({
                "module": module, "title": module, "protects": "", "cases": cases,
                "passed": sum(1 for c in cases if c["outcome"] == "passed"),
                "failed": sum(1 for c in cases if c["outcome"] != "passed"),
            })

    return render("quality.html", request, "/quality", report=report, areas=areas,
                  manifest=generate.load_manifest(), cfg=matching.DEFAULT_CONFIG)


# -------------------------------------------------------------------------- favicon

@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    """Both names, one local file. Browsers still request .ico unprompted."""
    return FileResponse(BASE / "static" / "favicon.svg", media_type="image/svg+xml")


# ----------------------------------------------------------------------- demo role

@app.get("/role/{actor}")
def switch_role(request: Request, actor: str):
    target = request.query_params.get("next", "/")
    if actor not in ACTORS:
        return back(target, f"Unknown demo user '{actor}'.", "warn")
    response = back(target, f"Now acting as {actor} ({ROLES[role_of(actor)]['label']}).", "info")
    response.set_cookie("rf_actor", actor, httponly=True, samesite="lax")
    return response

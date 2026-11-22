"""FastAPI app: upload / sample runs, live SSE run view, dashboard stats, review queue, reference data, policy."""
from __future__ import annotations

import asyncio
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, extract_ai, pipeline, narrate

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}
PACE = dict(normal=config.STAGE_MIN_MS, slow=1300, instant=0)
_slots = threading.Semaphore(3)

POLICY_META = [
    ("price_tolerance_pct", "Unit-price tolerance", "%", "How far a unit price may exceed the PO price before it is escalated."),
    ("total_tolerance_pct", "Total tolerance", "%", "Allowed difference between invoice net and the PO-priced total or remaining balance."),
    ("total_tolerance_abs", "Total tolerance (floor)", "$", "Absolute slack; the larger of this and the % applies."),
    ("qty_over_receipt_tolerance_pct", "Over-receipt tolerance", "%", "Billed quantity allowed above goods received (3-way match)."),
    ("auto_approve_limit", "Auto-approval limit", "$", "Invoices above this always go to a budget owner."),
    ("min_field_confidence", "Minimum field confidence", "0-1", "Invoice number and total must be read at least this confidently."),
    ("implied_po_min_confidence", "Inferred-PO confidence", "0-1", "When no PO is printed, minimum score to adopt the best-matching PO."),
    ("implied_po_min_margin", "Inferred-PO lead", "0-1", "...and how far ahead of the runner-up it must be."),
    ("stale_invoice_days", "Stale invoice after", "days", "Older invoices are escalated."),
    ("duplicate_similarity", "Duplicate number similarity", "0-1", "Invoice-number similarity at which a near-copy counts as a duplicate."),
    ("duplicate_date_window_days", "Duplicate date window", "days", "Same vendor + amount within this many days counts as the same invoice."),
    ("arithmetic_tolerance", "Rounding tolerance", "$", "Slack when re-adding subtotal, tax and total."),
    ("require_po", "Require a PO", "yes/no", "Invoices without any resolvable PO cannot be auto-approved."),
]

SECONDS_MANUAL = 7 * 60      # assumption shown in the UI: a person spends ~7 minutes on one invoice


def ensure_state():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    first = not os.path.exists(db.DB_PATH)
    db.init_schema()
    if first or not db.q("SELECT 1 FROM vendors LIMIT 1"):
        db.restore_seed()
    # a run left 'running' by a previous crash is closed out so the UI never hangs on it
    db.x("UPDATE runs SET status='error', error='interrupted by restart' WHERE status='running'")


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(ensure_state)
    yield


app = FastAPI(title="Clearance - AP invoice desk", lifespan=lifespan)


def run_dir(rid):
    return os.path.join(config.DATA_DIR, "runs", str(int(rid)))


def run_file(rid):
    hits = glob.glob(os.path.join(run_dir(rid), "original.*"))
    return hits[0] if hits else None


def _worker(rid, mode, dry, pace):
    with _slots:
        pipeline.process(rid, mode=mode, dry_run=dry, pace_ms=pace)


def start_run(path, filename, source, sample_id, mode, pace, dry_run=False, whatif_of=None):
    mode = mode if mode in ("auto", "local") else "auto"
    rid = pipeline.create_run(path, filename, source=source, sample_id=sample_id, mode=mode, dry_run=dry_run, whatif_of=whatif_of)
    threading.Thread(target=_worker, args=(rid, mode, dry_run, PACE.get(pace, config.STAGE_MIN_MS)), daemon=True).start()
    return rid


def _row_public(r, with_result=False):
    r = dict(r)
    r["code"] = f"RUN-{r['id']:04d}"
    try:
        r["reason_codes"] = json.loads(r["reason_codes"]) if r.get("reason_codes") else []
    except Exception:
        r["reason_codes"] = []
    res = r.pop("result_json", None)
    r.pop("file_path", None)
    if with_result:
        r["result"] = json.loads(res) if res else None
    return r


# ---------------------------------------------------------------- system
@app.get("/api/system")
def system(probe: int = 0):
    ai = dict(configured=extract_ai.available(), models=list(config.GEMINI_MODELS))
    if probe:
        ai["probe"] = extract_ai.ping()
    tess = ""
    try:
        tess = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, timeout=5).stdout.splitlines()[0]
    except Exception:
        tess = "not found"
    return dict(ok=True, ai=ai, tesseract=tess, as_of=(config.AS_OF_DATE or dt.date.today().isoformat()),
                runs=db.q("SELECT COUNT(*) c FROM runs", one=True)["c"], stage_ms=config.STAGE_MIN_MS)


@app.post("/api/ai/ping")
def ai_ping():
    return extract_ai.ping()


# ---------------------------------------------------------------- samples
@app.get("/api/samples")
def samples():
    with open(os.path.join(config.SAMPLES_DIR, "manifest.json")) as f:
        man = json.load(f)
    last = {}
    for r in db.rows("SELECT id, sample_id, decision, review_status FROM runs WHERE source='sample' AND dry_run=0 AND status='done' ORDER BY id"):
        last[r["sample_id"]] = dict(run_id=r["id"], decision=r["decision"], review_status=r["review_status"])
    for m in man:
        m["last"] = last.get(m["id"])
    return man


@app.get("/api/samples/{sid}/file")
def sample_file(sid: str):
    with open(os.path.join(config.SAMPLES_DIR, "manifest.json")) as f:
        man = {m["id"]: m for m in json.load(f)}
    if sid not in man:
        raise HTTPException(404, "unknown sample")
    return FileResponse(os.path.join(config.SAMPLES_DIR, man[sid]["file"]), filename=man[sid]["file"])


# ---------------------------------------------------------------- runs
@app.post("/api/runs")
async def upload_run(file: UploadFile = File(...), mode: str = Form("auto"), pace: str = Form("normal")):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type '{ext or '?'}'. Upload a PDF or an image (PNG, JPG, TIFF).")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    size = 0
    try:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > config.MAX_UPLOAD_MB * 1024 * 1024:
                raise HTTPException(413, f"File is larger than {config.MAX_UPLOAD_MB} MB.")
            tmp.write(chunk)
        tmp.close()
        if size == 0:
            raise HTTPException(400, "The file is empty.")
        rid = await asyncio.to_thread(start_run, tmp.name, os.path.basename(file.filename), "upload", None, mode, pace)
    finally:
        try:
            tmp.close()
            os.unlink(tmp.name)
        except OSError:
            pass
    return dict(run_id=rid, code=f"RUN-{rid:04d}")


@app.post("/api/runs/sample")
async def sample_run(body: dict):
    sid = body.get("sample_id")
    with open(os.path.join(config.SAMPLES_DIR, "manifest.json")) as f:
        man = {m["id"]: m for m in json.load(f)}
    if sid not in man:
        raise HTTPException(404, "unknown sample")
    path = os.path.join(config.SAMPLES_DIR, man[sid]["file"])
    rid = await asyncio.to_thread(start_run, path, man[sid]["file"], "sample", sid, body.get("mode", "auto"), body.get("pace", "normal"))
    return dict(run_id=rid, code=f"RUN-{rid:04d}")


@app.post("/api/runs/{rid}/whatif")
async def whatif(rid: int, body: dict | None = None):
    r = db.row("SELECT * FROM runs WHERE id=?", (rid,))
    src = run_file(rid)
    if not r or not src:
        raise HTTPException(404, "run not found")
    body = body or {}
    new = await asyncio.to_thread(start_run, src, r["filename"], "whatif", r.get("sample_id"), body.get("mode", r.get("engine_mode") or "auto"),
                                  body.get("pace", "normal"), True, rid)
    return dict(run_id=new, code=f"RUN-{new:04d}", of=rid)


@app.get("/api/runs")
def list_runs(decision: str = "", q: str = "", include_seed: int = 1, review: str = "", limit: int = 300, offset: int = 0):
    cond, args = ["1=1"], []
    if decision:
        cond.append("decision = ?"); args.append(decision.upper())
    if review:
        cond.append("review_status = ?"); args.append(review)
    if not include_seed:
        cond.append("source != 'seed'")
    if q:
        cond.append("(vendor_name LIKE ? OR invoice_number LIKE ? OR po_number LIKE ? OR filename LIKE ? OR headline LIKE ?)")
        args += [f"%{q}%"] * 5
    sql = ("SELECT id,created_at,finished_at,filename,source,sample_id,engine_used,status,decision,confidence,risk,vendor_id,vendor_name,invoice_number,"
           "invoice_date,currency,total,po_number,headline,reason_codes,duration_ms,review_status,review_action,supersedes_run_id,dry_run,whatif_of,error "
           f"FROM runs WHERE {' AND '.join(cond)} ORDER BY id DESC LIMIT ? OFFSET ?")
    return [_row_public(r) for r in db.rows(sql, (*args, limit, offset))]


@app.get("/api/runs/{rid}")
def get_run(rid: int):
    r = db.row("SELECT * FROM runs WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "run not found")
    events = []
    for e in db.rows("SELECT seq,ts,stage,status,payload FROM run_events WHERE run_id=? ORDER BY seq", (rid,)):
        e["payload"] = json.loads(e["payload"])
        events.append(e)
    out = _row_public(r, with_result=True)
    out["events"] = events
    out["audit"] = db.rows("SELECT ts,actor,action,note FROM audit WHERE run_id=? ORDER BY id", (rid,))
    out["has_file"] = bool(run_file(rid))
    return out


@app.get("/api/runs/{rid}/events")
async def run_events(rid: int, request: Request, last: int = 0):
    seq0 = int(request.headers.get("last-event-id") or last or 0)

    async def gen():
        seq, idle = seq0, 0.0
        yield "retry: 1500\n\n"
        while True:
            rows = await asyncio.to_thread(db.rows, "SELECT seq,ts,stage,status,payload FROM run_events WHERE run_id=? AND seq>? ORDER BY seq", (rid, seq))
            for e in rows:
                seq = e["seq"]
                data = dict(seq=seq, ts=e["ts"], stage=e["stage"], status=e["status"], payload=json.loads(e["payload"]))
                yield f"id: {seq}\nevent: stage\ndata: {json.dumps(data, default=str)}\n\n"
                if e["stage"] == "run":
                    return
            if rows:
                idle = 0.0
            else:
                idle += 0.15
                row = await asyncio.to_thread(db.row, "SELECT status FROM runs WHERE id=?", (rid,))
                if row is None or (row["status"] in ("done", "error") and idle > 0.6):
                    yield "event: end\ndata: {}\n\n"
                    return
                if idle > 120:
                    return
                if int(idle / 0.15) % 100 == 0:
                    yield ": keep-alive\n\n"
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/api/runs/{rid}/page/{n}")
def run_page(rid: int, n: int):
    p = os.path.join(run_dir(rid), f"page-{n}.jpg")
    if not os.path.exists(p):
        raise HTTPException(404, "page image not available")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/runs/{rid}/file")
def run_original(rid: int):
    p = run_file(rid)
    if not p:
        raise HTTPException(404, "file not available")
    return FileResponse(p, filename=db.row("SELECT filename FROM runs WHERE id=?", (rid,))["filename"])


@app.post("/api/runs/{rid}/review")
def review(rid: int, body: dict):
    r = db.row("SELECT * FROM runs WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "run not found")
    if r["review_status"] != "open":
        raise HTTPException(409, "This run is not waiting for review.")
    action = body.get("action")
    note = (body.get("note") or "").strip()
    actor = (body.get("actor") or "AP reviewer").strip()[:60]
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    if len(note) < 3:
        raise HTTPException(400, "A reason is required for every manual decision.")
    res = json.loads(r["result_json"]) if r.get("result_json") else {}
    led = db.row("SELECT * FROM ledger WHERE run_id=?", (rid,))
    if action == "approve":
        if led:
            db.x("UPDATE ledger SET status='approved', note=? WHERE id=?", (f"manual approval: {note}", led["id"]))
            if not db.row("SELECT 1 x FROM consumption WHERE ledger_id=?", (led["id"],)):
                po = res.get("po") or {}
                pipeline.consume(led["id"], rid, dict(po=po.get("po"), lines=po.get("lines") or []), res.get("invoice") or {})
        db.x("UPDATE runs SET review_status='approved', review_action=? WHERE id=?", (f"Approved by {actor} (override)", rid))
    else:
        if led:
            db.x("UPDATE ledger SET status='rejected', note=? WHERE id=?", (f"manual rejection: {note}", led["id"]))
        db.x("UPDATE runs SET review_status='rejected', review_action=? WHERE id=?", (f"Rejected by {actor}", rid))
    db.x("INSERT INTO audit (run_id,ts,actor,action,note) VALUES (?,?,?,?,?)", (rid, db.now_iso(), actor, action, note))
    return get_run(rid)


# ---------------------------------------------------------------- dashboard
CODES_RISK = ("DUPLICATE_INVOICE", "BANK_MISMATCH", "BANK_CHANGE_NOTICE", "EMAIL_LOOKALIKE", "HIDDEN_TEXT", "INJECTION_TEXT", "VENDOR_BLOCKED", "VENDOR_UNKNOWN", "TAXID_MISMATCH")


@app.get("/api/stats")
def stats(include_seed: int = 1):
    cond = "status='done' AND dry_run=0" + ("" if include_seed else " AND source != 'seed'")
    rs = db.rows(f"SELECT id,created_at,decision,total,risk,duration_ms,reason_codes,review_status,vendor_name,confidence FROM runs WHERE {cond}")
    n = len(rs)
    by_dec = {"APPROVE": 0, "HOLD": 0, "ESCALATE": 0, "REJECT": 0}
    amt = {"APPROVE": 0.0, "HOLD": 0.0, "ESCALATE": 0.0, "REJECT": 0.0}
    days, reasons, vendors, blocked_risk, risk_n = {}, {}, {}, 0.0, 0
    for r in rs:
        d = r["decision"]
        by_dec[d] = by_dec.get(d, 0) + 1
        amt[d] = amt.get(d, 0.0) + (r["total"] or 0.0)
        day = (r["created_at"] or "")[:10]
        days.setdefault(day, {"APPROVE": 0, "HOLD": 0, "ESCALATE": 0, "REJECT": 0})
        days[day][d] += 1
        codes = json.loads(r["reason_codes"] or "[]")
        if d != "APPROVE":
            for c in codes[:2]:
                reasons[c] = reasons.get(c, 0) + 1
            if any(c in CODES_RISK for c in codes):
                blocked_risk += r["total"] or 0.0
                risk_n += 1
        v = r["vendor_name"] or "Unknown"
        vendors.setdefault(v, {"n": 0, "amt": 0.0})
        vendors[v]["n"] += 1
        vendors[v]["amt"] += r["total"] or 0.0
    avg_ms = (sum(r["duration_ms"] or 0 for r in rs) / n) if n else 0
    open_review = db.q("SELECT COUNT(*) c FROM runs WHERE review_status='open' AND dry_run=0", one=True)["c"]
    # ensure continuous day axis
    series = []
    if days:
        d0, d1 = dt.date.fromisoformat(min(days)), dt.date.fromisoformat(max(days))
        d = d0
        while d <= d1:
            k = d.isoformat()
            series.append(dict(day=k, **days.get(k, {"APPROVE": 0, "HOLD": 0, "ESCALATE": 0, "REJECT": 0})))
            d += dt.timedelta(days=1)
    return dict(
        processed=n, by_decision=by_dec, amount=amt, stp_rate=(by_dec["APPROVE"] / n if n else 0), avg_seconds=round(avg_ms / 1000, 1),
        hours_saved=round(by_dec["APPROVE"] * SECONDS_MANUAL / 3600 + (n - by_dec["APPROVE"]) * SECONDS_MANUAL * 0.5 / 3600, 1),
        manual_minutes_assumed=SECONDS_MANUAL // 60, risk_blocked=round(blocked_risk, 2), risk_blocked_n=risk_n, open_review=open_review,
        series=series, reasons=sorted(([k, v] for k, v in reasons.items()), key=lambda kv: -kv[1])[:8],
        vendors=sorted(([k, v["n"], round(v["amt"], 2)] for k, v in vendors.items()), key=lambda kv: -kv[2])[:6])


# ---------------------------------------------------------------- reference + policy
@app.get("/api/reference")
def reference():
    pol = db.get_policy()
    return dict(vendors=db.vendors(), pos=db.po_summary(), policy=pol,
                policy_meta=[dict(key=k, label=l, unit=u, help=h, value=pol.get(k)) for k, l, u, h in POLICY_META if k in pol],
                ledger=db.rows("SELECT id,vendor_id,invoice_number,invoice_date,po_number,total,status,paid_date,source FROM ledger ORDER BY id DESC LIMIT 60"))


@app.put("/api/policy")
def put_policy(body: dict):
    return db.set_policy(body)


@app.post("/api/admin/reset")
def admin_reset():
    ok = db.restore_seed()
    return dict(ok=True, seeded=ok)


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(config.STATIC_DIR, "index.html"), headers={"Cache-Control": "no-cache"})

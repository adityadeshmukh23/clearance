"""SQLite store: reference data (vendors / POs), invoice ledger, PO consumption, runs, events, audit."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import shutil
import sqlite3
import threading

from . import config
from .normalize import invoice_number_core, norm_invoice_number

_local = threading.local()
DB_PATH = os.path.join(config.DATA_DIR, "clearance.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (vendor_id TEXT PRIMARY KEY, legal_name TEXT, aliases TEXT, tax_id TEXT, status TEXT,
  status_note TEXT, payment_terms TEXT, currency TEXT, bank_name TEXT, bank_account TEXT, bank_routing TEXT, email TEXT,
  phone TEXT, address TEXT);
CREATE TABLE IF NOT EXISTS pos (po_number TEXT PRIMARY KEY, vendor_id TEXT, issue_date TEXT, currency TEXT,
  tax_rate_pct REAL, status TEXT, buyer TEXT, cost_center TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS po_lines (po_number TEXT, line_no INTEGER, sku TEXT, description TEXT, qty_ordered REAL,
  unit_price REAL, qty_received REAL, needs_receipt INTEGER, PRIMARY KEY (po_number, line_no));
CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT, invoice_number TEXT,
  norm_number TEXT, core_number TEXT, invoice_date TEXT, po_number TEXT, net REAL, tax REAL, total REAL, currency TEXT,
  status TEXT, paid_date TEXT, file_hash TEXT, run_id INTEGER, source TEXT, created_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS consumption (id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, line_no INTEGER,
  qty REAL, amount REAL, ledger_id INTEGER, run_id INTEGER);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, finished_at TEXT, filename TEXT,
  file_hash TEXT, file_path TEXT, size INTEGER, source TEXT, sample_id TEXT, engine_mode TEXT, engine_used TEXT,
  status TEXT, decision TEXT, confidence REAL, risk INTEGER, vendor_id TEXT, vendor_name TEXT, invoice_number TEXT,
  invoice_date TEXT, currency TEXT, total REAL, po_number TEXT, headline TEXT, reason_codes TEXT, result_json TEXT,
  duration_ms INTEGER, review_status TEXT, review_action TEXT, supersedes_run_id INTEGER, dry_run INTEGER DEFAULT 0,
  error TEXT, as_of TEXT, whatif_of INTEGER);
CREATE TABLE IF NOT EXISTS run_events (run_id INTEGER, seq INTEGER, ts REAL, stage TEXT, status TEXT, payload TEXT,
  PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, actor TEXT,
  action TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS policy (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_ledger_vendor ON ledger(vendor_id);
CREATE INDEX IF NOT EXISTS ix_ledger_hash ON ledger(file_hash);
CREATE INDEX IF NOT EXISTS ix_runs_created ON runs(created_at);
"""


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return c


def close_all():
    c = getattr(_local, "c", None)
    if c is not None:
        c.close()
        _local.c = None


def q(sql, args=(), one=False):
    cur = conn().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    c = conn()
    cur = c.execute(sql, args)
    c.commit()
    return cur


def rows(sql, args=()):
    return [dict(r) for r in q(sql, args)]


def row(sql, args=()):
    r = q(sql, args, one=True)
    return dict(r) if r else None


# ------------------------------------------------------------------ init & reference data
def init_schema():
    conn().executescript(SCHEMA)
    cols = {r["name"] for r in q("PRAGMA table_info(runs)")}
    if "whatif_of" not in cols:
        conn().execute("ALTER TABLE runs ADD COLUMN whatif_of INTEGER")
    conn().commit()


# runs whose ledger/consumption rows are ignored while evaluating (what-if re-runs must not collide with themselves)
_ctx = threading.local()


def set_exclude_runs(ids):
    _ctx.exclude = [int(i) for i in ids if i]


def excluded_runs():
    return getattr(_ctx, "exclude", [])


TABLES = ["vendors", "pos", "po_lines", "ledger", "consumption", "runs", "run_events", "audit", "policy"]


def restore_seed():
    """Reset all state to the pristine seed (reference data + seeded history). Safe while other threads hold connections."""
    init_schema()
    c = conn()
    seed_db = os.path.join(config.SEED_DIR, "clearance.db")
    c.execute("DELETE FROM sqlite_sequence")
    if os.path.exists(seed_db):
        c.execute("ATTACH DATABASE ? AS seed", (seed_db,))
        for t in TABLES:
            c.execute(f"DELETE FROM main.{t}")
            c.execute(f"INSERT INTO main.{t} SELECT * FROM seed.{t}")
        c.commit()
        c.execute("DETACH DATABASE seed")
        runs_dir = os.path.join(config.DATA_DIR, "runs")
        shutil.rmtree(runs_dir, ignore_errors=True)
        src = os.path.join(config.SEED_DIR, "runs")
        if os.path.isdir(src):
            shutil.copytree(src, runs_dir)
        else:
            os.makedirs(runs_dir, exist_ok=True)
        return True
    reset_runtime()
    return False


def _csv(name):
    with open(os.path.join(config.REF_DIR, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_reference():
    """(Re)load vendors / POs / lines / historical ledger from the CSVs and policy.json."""
    c = conn()
    for t in ("vendors", "pos", "po_lines", "ledger", "consumption", "policy"):
        c.execute(f"DELETE FROM {t}")
    for v in _csv("vendors.csv"):
        c.execute("INSERT INTO vendors VALUES (:vendor_id,:legal_name,:aliases,:tax_id,:status,:status_note,"
                  ":payment_terms,:currency,:bank_name,:bank_account,:bank_routing,:email,:phone,:address)", v)
    for p in _csv("purchase_orders.csv"):
        p["tax_rate_pct"] = float(p["tax_rate_pct"])
        c.execute("INSERT INTO pos VALUES (:po_number,:vendor_id,:issue_date,:currency,:tax_rate_pct,:status,:buyer,"
                  ":cost_center,:description)", p)
    for l in _csv("po_lines.csv"):
        c.execute("INSERT INTO po_lines VALUES (?,?,?,?,?,?,?,?)",
                  (l["po_number"], int(l["line_no"]), l["sku"], l["description"], float(l["qty_ordered"]),
                   float(l["unit_price"]), float(l["qty_received"]), int(l["needs_receipt"])))
    for h in _csv("historical_invoices.csv"):
        cur = c.execute(
            "INSERT INTO ledger (vendor_id,invoice_number,norm_number,core_number,invoice_date,po_number,net,tax,total,"
            "currency,status,paid_date,file_hash,run_id,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (h["vendor_id"], h["invoice_number"], norm_invoice_number(h["invoice_number"]),
             invoice_number_core(h["invoice_number"]), h["invoice_date"], h["po_number"], float(h["net"]),
             float(h["tax"]), float(h["total"]), h["currency"], "paid", h["paid_date"], None, None, "historical",
             h["paid_date"]))
        for ln, qty, amt in json.loads(h["lines_json"]):
            c.execute("INSERT INTO consumption (po_number,line_no,qty,amount,ledger_id,run_id) VALUES (?,?,?,?,?,NULL)",
                      (h["po_number"], int(ln), float(qty), float(amt), cur.lastrowid))
    with open(os.path.join(config.REF_DIR, "policy.json")) as f:
        for k, v in json.load(f).items():
            c.execute("INSERT INTO policy VALUES (?,?)", (k, json.dumps(v)))
    c.commit()


def reset_runtime(extra_refs=None):
    """Wipe runs/events/audit and reload reference data (pristine state, no seeded history)."""
    c = conn()
    for t in ("runs", "run_events", "audit"):
        c.execute(f"DELETE FROM {t}")
    c.commit()
    load_reference()
    runs_dir = os.path.join(config.DATA_DIR, "runs")
    shutil.rmtree(runs_dir, ignore_errors=True)
    os.makedirs(runs_dir, exist_ok=True)


def get_policy():
    return {r["key"]: json.loads(r["value"]) for r in q("SELECT key,value FROM policy")}


def set_policy(updates: dict):
    cur = get_policy()
    for k, v in updates.items():
        if k not in cur:
            continue
        t = type(cur[k])
        if t is bool:
            v = bool(v)
        elif t in (int, float):
            v = float(v)
        x("UPDATE policy SET value=? WHERE key=?", (json.dumps(v), k))
    return get_policy()


# ------------------------------------------------------------------ convenience lookups
def vendors():
    return rows("SELECT * FROM vendors ORDER BY vendor_id")


def vendor(vid):
    return row("SELECT * FROM vendors WHERE vendor_id=?", (vid,))


def po(po_number):
    return row("SELECT * FROM pos WHERE po_number=?", (po_number,))


def po_lines(po_number):
    return rows("SELECT * FROM po_lines WHERE po_number=? ORDER BY line_no", (po_number,))


def po_consumption(po_number):
    """{line_no: (qty, amount)} consumed by paid/approved invoices."""
    out = {}
    ex = excluded_runs()
    cond = f" AND (run_id IS NULL OR run_id NOT IN ({','.join('?' * len(ex))}))" if ex else ""
    for r in rows("SELECT line_no, SUM(qty) q, SUM(amount) a FROM consumption WHERE po_number=?" + cond + " GROUP BY line_no",
                  (po_number, *ex)):
        out[r["line_no"]] = (r["q"] or 0.0, r["a"] or 0.0)
    return out


def po_summary():
    """Every PO with ordered / billed / remaining (net)."""
    out = []
    for p in rows("SELECT * FROM pos ORDER BY po_number"):
        lines = po_lines(p["po_number"])
        cons = po_consumption(p["po_number"])
        ordered = round(sum(l["qty_ordered"] * l["unit_price"] for l in lines), 2)
        billed = round(sum(a for (_, a) in cons.values()), 2)
        v = vendor(p["vendor_id"])
        out.append(dict(p, vendor_name=v["legal_name"] if v else "?", ordered=ordered, billed=billed,
                        remaining=round(ordered - billed, 2), lines=lines))
    return out


def ledger_for_vendor(vendor_id):
    return rows("SELECT * FROM ledger WHERE vendor_id=?", (vendor_id,))

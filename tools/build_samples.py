"""Build reference CSVs, policy.json and the sample invoice files + manifest.

Usage: python tools/build_samples.py
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from dataset import (HISTORICAL, POLICY, PO_LINES, POS, SCENARIOS, VENDORS, VENDOR_BY_ID)  # noqa: E402
from render import render_pdf, scanify, totals, vendor_of  # noqa: E402
import seed_history  # noqa: E402


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def build_reference(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    bg_pos, bg_lines, _ = seed_history.build()
    write_csv(os.path.join(data_dir, "vendors.csv"), VENDORS,
              ["vendor_id", "legal_name", "aliases", "tax_id", "status", "status_note", "payment_terms",
               "currency", "bank_name", "bank_account", "bank_routing", "email", "phone", "address"])
    pos = [dict(po_number=p[0], vendor_id=p[1], issue_date=p[2], tax_rate_pct=p[3], status=p[4], buyer=p[5],
                cost_center=p[6], description=p[7], currency="USD") for p in list(POS) + list(bg_pos)]
    write_csv(os.path.join(data_dir, "purchase_orders.csv"), pos,
              ["po_number", "vendor_id", "issue_date", "currency", "tax_rate_pct", "status", "buyer",
               "cost_center", "description"])
    lines = [dict(po_number=l[0], line_no=l[1], sku=l[2], description=l[3], qty_ordered=l[4],
                  unit_price=l[5], qty_received=l[6], needs_receipt=l[7]) for l in list(PO_LINES) + list(bg_lines)]
    write_csv(os.path.join(data_dir, "po_lines.csv"), lines,
              ["po_number", "line_no", "sku", "description", "qty_ordered", "unit_price", "qty_received",
               "needs_receipt"])
    hist = []
    for h in HISTORICAL:
        hh = dict(h)
        hh["currency"] = "USD"
        hh["status"] = "paid"
        hh["lines_json"] = json.dumps(h["lines"])
        hist.append(hh)
    write_csv(os.path.join(data_dir, "historical_invoices.csv"), hist,
              ["invoice_id", "vendor_id", "invoice_number", "invoice_date", "po_number", "net", "tax", "total",
               "currency", "status", "paid_date", "lines_json"])
    with open(os.path.join(data_dir, "policy.json"), "w") as f:
        json.dump(POLICY, f, indent=2)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48]


def build_samples(sample_dir):
    os.makedirs(sample_dir, exist_ok=True)
    manifest = []
    for sp in SCENARIOS:
        base = f"{sp['id']}_{slug(sp['title'].split(' - ')[-1] if ' - ' in sp['title'] else sp['title'])}"
        digital = os.path.join(sample_dir, base + ".pdf")
        t = render_pdf(sp, digital)
        fname = os.path.basename(digital)
        if sp.get("scan"):
            sc = sp["scan"]
            ext = "jpg" if sc["kind"] == "jpg" else "pdf"
            out = os.path.join(sample_dir, f"{base}_scan.{ext}")
            scanify(digital, out, kind=sc["kind"], angle=sc["angle"], seed=sc["seed"])
            os.remove(digital)  # only the messy version ships as a sample
            fname = os.path.basename(out)
        v = vendor_of(sp)
        manifest.append(dict(
            id=sp["id"], title=sp["title"], group=sp["group"], file=fname, expect=sp["expect"],
            blurb=sp["blurb"], tags=sp["tags"], after=sp.get("after"),
            truth=dict(vendor=v["legal_name"], invoice_number=None if (sp.get("smudge_number") or sp["layout"] == "statement") else sp["number"],
                       invoice_date=sp["date"], po=sp.get("po") if sp.get("po_ref") in (True, "in_description") else None,
                       total=(None if sp["layout"] == "statement" else t["total"]), subtotal=t["subtotal"], tax=t["tax"], lines=len(sp["lines"]),
                       scanned=bool(sp.get("scan"))),
        ))
    with open(os.path.join(sample_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def build_seed_invoices(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    _, _, invs = seed_history.build()
    man = []
    for sp in invs:
        fname = f"{sp['id']}_{sp['scenario']}.pdf"
        t = render_pdf(sp, os.path.join(out_dir, fname))
        v = vendor_of(sp)
        man.append(dict(id=sp["id"], file=fname, created_at=sp["created_at"], scenario=sp["scenario"], vendor=v["legal_name"],
                        number=sp["number"], date=sp["date"], total=t["total"], po=sp.get("po")))
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(man, f, indent=1)
    return man


if __name__ == "__main__":
    build_reference(os.path.join(ROOT, "data"))
    m = build_samples(os.path.join(ROOT, "samples"))
    sm = build_seed_invoices(os.path.join(ROOT, "data", "seed_invoices"))
    print(f"built {len(sm)} background invoices for the history")
    print(f"built reference data + {len(m)} samples")
    for x in m:
        print(f"  {x['id']:4} {x['file']:55} expect={x['expect']:9} total={x['truth']['total']}")

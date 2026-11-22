import sys, json, glob
sys.path.insert(0, "/home/claude/clearance")
from app import docio
from app.extract_local import extract_local
man = {m["id"]: m for m in json.load(open("/home/claude/clearance/samples/manifest.json"))}
ids = [a for a in sys.argv[1:] if not a.startswith("-")] or list(man)
bad = 0
for sid in ids:
    m = man[sid]
    d = docio.load_document("/home/claude/clearance/samples/" + m["file"], f"/tmp/run_{sid}")
    ex = extract_local(d)
    f = {k: v["value"] for k, v in ex["fields"].items()}
    t = m["truth"]
    checks = [("invoice_number", f["invoice_number"], t["invoice_number"]),
              ("invoice_date", f["invoice_date"], t["invoice_date"]),
              ("po", f["po_reference"], t["po"]),
              ("total", f["total"], t["total"] if t["total"] else None),
              ("n_lines", len(ex["line_items"]), t["lines"])]
    res = []
    for k, got, exp in checks:
        ok = (got == exp)
        if not ok: bad += 1
        res.append(f"{'OK ' if ok else 'XX '}{k}={got!r}" + ("" if ok else f" (exp {exp!r})"))
    print(f"{sid:4} {ex['doc_type']['value']:9} vendor={f['vendor_name']!r} | " + " | ".join(res))
    if "-v" in sys.argv or any(r.startswith("XX") for r in res):
        print("     fields:", {k: v for k, v in f.items() if v not in (None, "") and k not in ("vendor_name","invoice_number","invoice_date","po_reference","total")})
        for li in ex["line_items"]: print("     line:", li["sku"], "|", li["description"], li["qty"], li["unit_price"], li["amount"], li["confidence"])
        print("     flags:", ex["flags"], ex["anomalies"])
print("MISMATCHES:", bad)

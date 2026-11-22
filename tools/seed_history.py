"""Background POs and ~36 back-dated invoices that give the dashboard a believable history.

They are run through the real pipeline at build time (local engine, no AI calls) and flagged source='seed'
in the UI, so nothing about the history is hand-written: every decision comes from the same rules.
"""
from __future__ import annotations

import datetime as dt
import random

R = random.Random(7)

CATALOG = {
    "V-1001": ("classic", 8.25, True, [("HIS-CPL-25", "Shaft coupling 25 mm", 58.00), ("HIS-SEAL-40", "Oil seal 40 mm", 6.75),
                                       ("HIS-FLT-HY", "Hydraulic filter element", 41.20), ("HIS-CHN-10", "Roller chain 10B-1, 10 ft", 37.50),
                                       ("HIS-SPR-22", "Drive sprocket 22T", 29.90), ("HIS-GRS-14", "Grease cartridge 14 oz (case)", 48.00)]),
    "V-1002": ("modern", 7.0, True, [("MOS-LMP-LED", "LED desk lamp", 24.90), ("MOS-CAB-4D", "4-drawer filing cabinet", 142.00),
                                     ("MOS-WBD-90", "Whiteboard 90x120 cm", 67.50), ("MOS-KBD-ERG", "Ergonomic keyboard", 79.00),
                                     ("MOS-HDS-USB", "USB headset", 52.00), ("MOS-STP-HD", "Heavy-duty stapler", 18.40)]),
    "V-1004": ("stacked", 0.0, False, [("", "Window cleaning - quarterly", 880.00), ("", "Pest control visit", 260.00),
                                       ("", "Carpet deep clean (per floor)", 540.00), ("", "Fire extinguisher inspection", 310.00)]),
    "V-1005": ("classic", 0.0, True, [("APX-CRT-L", "Corrugated carton, large (pack of 50)", 118.00), ("APX-PAD-F", "Foam padding sheet", 9.40),
                                      ("APX-TAPE-48", "Packing tape 48 mm (case of 36)", 52.00), ("APX-PAL-STD", "Standard pallet, wooden", 14.80)]),
}
PREFIX = {"V-1001": ("INV-", 20380), "V-1002": ("MO-", 55100), "V-1003": ("BCG-", 880), "V-1004": ("KFS-", 1165), "V-1005": ("APX-2026-", 32)}
BUYER = {"V-1001": "R. Whitfield", "V-1002": "L. Okafor", "V-1003": "A. Brandt", "V-1004": "L. Okafor", "V-1005": "D. Marchetti"}
CC = {"V-1001": "CC-210 Maintenance", "V-1002": "CC-115 Facilities", "V-1003": "CC-340 Finance Transformation",
      "V-1004": "CC-115 Facilities", "V-1005": "CC-520 Packaging Line 2"}
SVC_TITLES = ["Advisory services - Phase 1 (fixed fee)", "Process review workshop series", "Controls assessment - fixed fee", "Data migration support - fixed fee"]


def build():
    """Returns (pos, lines, invoices). pos/lines use the same tuple shapes as dataset.py. Deterministic."""
    R.seed(7)
    pos, lines = [], []
    pn = 4301
    po_state = {}      # po -> {line_no: dict(sku, desc, qty, price, recv)}
    by_vendor = {}
    issue = dt.date(2026, 10, 1)
    for vid, (layout, tax, goods, cat) in CATALOG.items():
        for k in range(3):
            po_no = f"PO-{pn}"
            pn += 1
            d = issue + dt.timedelta(days=R.randint(0, 18))
            items = R.sample(cat, 3 if goods else 3)
            pos.append((po_no, vid, d.isoformat(), tax, "open", BUYER[vid], CC[vid], "Background PO " + po_no[3:]))
            po_state[po_no] = {}
            for ln, (sku, desc, price) in enumerate(items, 1):
                q = R.choice([40, 60, 80, 120, 200, 250, 300]) if goods else R.choice([2, 3, 4, 6])
                lines.append((po_no, ln, sku, desc, q, price, q, 1 if goods else 0))
                po_state[po_no][ln] = dict(sku=sku, desc=desc, qty=q, price=price, rem=q, recv=q)
            by_vendor.setdefault(vid, []).append(po_no)
    # Brightline: fixed-fee services, bundled invoices
    for k in range(3):
        po_no = f"PO-{pn}"
        pn += 1
        fee = R.choice([9000.0, 12500.0, 6400.0])
        pos.append((po_no, "V-1003", (issue + dt.timedelta(days=R.randint(0, 15))).isoformat(), 8.25, "open", "A. Brandt", CC["V-1003"], SVC_TITLES[k]))
        lines.append((po_no, 1, "BCG-FEE", SVC_TITLES[k], 1, fee, 1, 0))
        po_state[po_no] = {1: dict(sku="BCG-FEE", desc=SVC_TITLES[k], qty=1, price=fee, rem=1, recv=1, lump=True, rem_amt=fee)}
        by_vendor.setdefault("V-1003", []).append(po_no)
    # one big lump-sum PO and one PO with short receipts, for specific scenarios
    big = f"PO-{pn}"; pn += 1
    pos.append((big, "V-1005", "2026-10-05", 0.0, "open", "D. Marchetti", CC["V-1005"], "Automated palletiser cell (milestone billing)"))
    lines.append((big, 1, "APX-PLT-CELL", "Automated palletiser cell - installation and commissioning (lump sum)", 1, 62000.00, 1, 0))
    short = f"PO-{pn}"; pn += 1
    pos.append((short, "V-1001", "2026-10-08", 8.25, "open", "R. Whitfield", CC["V-1001"], "Hydraulic spares (partial delivery)"))
    lines.append((short, 1, "HIS-HYD-PMP", "Hydraulic gear pump", 50, 214.00, 30, 1))
    lines.append((short, 2, "HIS-HYD-HOSE", "Hydraulic hose assembly 1 m", 120, 22.50, 120, 1))

    seq = {v: PREFIX[v][1] for v in PREFIX}

    def next_no(vid):
        seq[vid] += R.randint(1, 4)
        pre = PREFIX[vid][0]
        return f"{pre}{seq[vid]:0{4 if vid == 'V-1003' else 0}d}" if vid == "V-1003" else f"{pre}{seq[vid]}"

    invs = []
    approved = []                                  # specs eligible to be copied as duplicates

    def goods_invoice(vid, po_no, picks, var=0.0, over_qty=False, number="auto", po_ref=True, scenario="approve"):
        layout, tax, goods, _ = CATALOG[vid]
        ls = []
        for ln in picks:
            st = po_state[po_no][ln]
            qty = max(1, int(st["rem"] * R.uniform(0.15, 0.45)))
            if over_qty:
                qty = int(st["recv"] * 0.95) + 4
            if qty > st["rem"] and not over_qty:
                qty = st["rem"]
            if qty <= 0:
                continue
            ls.append((st["sku"], st["desc"], qty, round(st["price"] * (1 + var), 2)))
            if scenario == "approve":
                st["rem"] -= qty
        if not ls:
            return None
        return dict(vendor=vid, layout=layout, po=po_no, lines=ls, tax_rate=tax, number=next_no(vid) if number == "auto" else number,
                    po_ref=po_ref, scenario=scenario)

    def service_invoice(vid, po_no, frac, scenario="approve"):
        layout, tax, _, _ = CATALOG.get(vid, ("inclusive", 8.25, False, []))
        st = po_state[po_no][1]
        if vid == "V-1003":
            gross = round(st["price"] * frac * (1 + tax / 100), 2)
            title = st["desc"] + f" ({int(frac * 100)}% milestone, per {po_no})"
            ls = [("", title, 1, gross)]
            if scenario == "approve":
                st["rem_amt"] = st.get("rem_amt", st["price"]) - round(st["price"] * frac, 2)
            return dict(vendor=vid, layout="inclusive", po=po_no, lines=ls, tax_rate=tax, tax_mode="inclusive", number=next_no(vid),
                        po_ref="in_description", scenario=scenario)
        raise ValueError

    vendors_goods = ["V-1001", "V-1002", "V-1005"]
    # ---- 17 clean approvals
    for i in range(17):
        vid = R.choice(vendors_goods + ["V-1004", "V-1003"])
        if vid == "V-1003":
            po = R.choice([p for p in by_vendor[vid] if po_state[p][1].get("rem_amt", po_state[p][1]["price"]) > 4000])
            sp = service_invoice(vid, po, R.choice([0.25, 0.3, 0.4]))
        else:
            po = R.choice(by_vendor[vid])
            ln = list(po_state[po])
            sp = goods_invoice(vid, po, R.sample(ln, k=min(len(ln), R.choice([1, 2, 2, 3]))))
        if sp:
            invs.append(sp)
            approved.append(sp)
    # ---- 3 within tolerance
    for _ in range(3):
        vid = R.choice(["V-1001", "V-1002", "V-1005"])
        po = R.choice(by_vendor[vid])
        sp = goods_invoice(vid, po, [R.choice(list(po_state[po]))], var=R.choice([0.005, 0.011, 0.016]))
        if sp:
            invs.append(sp); approved.append(sp)
    # ---- 2 approvals with no PO printed (inferred)
    for vid in ("V-1002", "V-1005"):
        po = by_vendor[vid][-1]
        sp = goods_invoice(vid, po, list(po_state[po])[:2], po_ref=False)
        if sp:
            invs.append(sp); approved.append(sp)
    # ---- 3 price escalations
    for _ in range(3):
        vid = R.choice(["V-1001", "V-1002", "V-1005"])
        po = R.choice(by_vendor[vid])
        sp = goods_invoice(vid, po, [R.choice(list(po_state[po]))], var=R.choice([0.035, 0.05, 0.07]), scenario="escalate_price")
        if sp:
            invs.append(sp)
    # ---- over-limit lump sum (Apex palletiser, 45% milestone = $27,900)
    invs.append(dict(vendor="V-1005", layout="classic", po=big, lines=[("", "Automated palletiser cell - milestone 1: installation (45%)", 1, 27900.00)],
                     tax_rate=0.0, number=next_no("V-1005"), po_ref=True, scenario="escalate_limit"))
    # ---- billed more than received
    invs.append(dict(vendor="V-1001", layout="classic", po=short, lines=[("HIS-HYD-PMP", "Hydraulic gear pump", 42, 214.00)], tax_rate=8.25,
                     number=next_no("V-1001"), po_ref=True, scenario="escalate_receipt"))
    # ---- 2 holds: number missing
    for vid in ("V-1002", "V-1005"):
        po = by_vendor[vid][0]
        sp = goods_invoice(vid, po, [list(po_state[po])[0]], number=None, scenario="hold_number")
        if sp:
            invs.append(sp)
    # ---- 2 duplicates of earlier approvals (different number format)
    dups = []
    for src in R.sample([a for a in approved if a["vendor"] in ("V-1001", "V-1002", "V-1005")], 2):
        d = dict(src)
        n = src["number"]
        d["number"] = (n.split("-")[-1] + "-A") if n.startswith("INV") else n + "R"
        d["scenario"] = "reject_duplicate"
        d["dup_of"] = src["number"]
        dups.append(d)
    invs += dups
    # ---- 2 unknown vendors, 1 blocked vendor, 1 statement
    for name, tid in (("Brookfield Industrial Traders", "71-2290418"), ("Nimbus Office Wholesale", "66-5517209")):
        invs.append(dict(vendor=None, layout="classic", po=None, po_ref=False, tax_rate=0.0, number="BT-" + str(R.randint(1000, 9999)),
                         lines=[("", "Supplies - assorted", 1, round(R.uniform(900, 4200), 2))], scenario="reject_unknown",
                         vendor_override=dict(legal_name=name, address="1 Commerce Plaza|Dover, DE 19901", tax_id=tid,
                                              email="sales@" + name.split()[0].lower() + ".example", phone="+1 302 555 0150")))
    invs.append(dict(vendor="V-1006", layout="modern", po="PO-4580", po_ref=True, tax_rate=0.0, number="OFL-" + str(R.randint(90500, 90999)),
                     lines=[("", "Inbound freight services - November", 1, 3150.00)], scenario="reject_blocked"))
    invs.append(dict(vendor="V-1002", layout="statement", po=None, po_ref=False, tax_rate=0.0, number="STMT-1031", lines=[], scenario="reject_statement"))

    # ---- assign dates: spread across 26 Oct .. 18 Nov, processing order == chronological order
    R.shuffle(invs)
    # duplicates must be processed after their originals
    invs.sort(key=lambda s: 1 if s["scenario"] == "reject_duplicate" else 0)
    n = len(invs)
    start, end = dt.date(2026, 10, 26), dt.date(2026, 11, 18)
    span = (end - start).days
    pos_index = {p[0]: p for p in pos}
    for i, sp in enumerate(invs):
        day = start + dt.timedelta(days=int(span * i / max(n - 1, 1)))
        hh, mm = R.randint(8, 17), R.randint(0, 59)
        sp["created_at"] = f"{day.isoformat()}T{hh:02d}:{mm:02d}:00"
        po_issue = dt.date.fromisoformat(pos_index[sp["po"]][2]) if sp.get("po") and sp["po"] in pos_index else None
        inv_day = day - dt.timedelta(days=R.randint(1, 3))
        if po_issue and inv_day < po_issue + dt.timedelta(days=1):
            inv_day = po_issue + dt.timedelta(days=1)
        if inv_day > day:
            inv_day = day
        sp["date"] = inv_day.isoformat()
        sp.setdefault("tax_mode", "separate")
        sp.setdefault("shipping", 0.0)
        sp.setdefault("discount", 0.0)
        sp.setdefault("currency", "$")
        sp.setdefault("bank", "master")
        sp.setdefault("notes", [])
        sp.setdefault("smudge_number", False)
        sp.setdefault("hidden_text", None)
    # duplicates share the date/lines of their originals
    by_no = {s["number"]: s for s in invs}
    for s in invs:
        if s["scenario"] == "reject_duplicate":
            o = by_no[s["dup_of"]]
            s["date"] = o["date"]
            s["lines"] = o["lines"]
            # make sure a duplicate is created after its original
            if s["created_at"] <= o["created_at"]:
                s["created_at"] = (dt.datetime.fromisoformat(o["created_at"]) + dt.timedelta(days=2, hours=3)).isoformat()
    invs.sort(key=lambda s: s["created_at"])
    for i, s in enumerate(invs, 1):
        s["id"] = f"B{i:02d}"
    return pos, lines, invs


if __name__ == "__main__":
    p, l, i = build()
    print(len(p), "POs", len(l), "lines", len(i), "invoices")
    from collections import Counter
    print(Counter(x["scenario"] for x in i))

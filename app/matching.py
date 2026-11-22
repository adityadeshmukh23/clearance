"""Vendor resolution, duplicate detection and PO matching (explicit and inferred)."""
from __future__ import annotations

import datetime as dt
import re

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from . import db
from .normalize import (digits_only, email_domain, invoice_number_core, norm_company, norm_invoice_number, norm_po)

ACTIVE_LEDGER = ("paid", "approved", "pending_review")


def F(code, title, detail, severity="info", outcome=None, route=None, expected=None, found=None, stage=None):
    """A single check result. severity: pass | info | warn | fail. outcome hints REJECT/HOLD/ESCALATE for fails."""
    return dict(code=code, title=title, detail=detail, severity=severity, outcome=outcome, route=route,
                expected=expected, found=found, stage=stage)


def _days(a, b):
    try:
        return abs((dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days)
    except Exception:
        return 10 ** 6


# ================================================================= vendor
def resolve_vendor(inv, text_source):
    vendors = db.vendors()
    best, second = None, None
    scored = []
    for v in vendors:
        names = [v["legal_name"]] + [a for a in (v["aliases"] or "").split("|") if a]
        nm = 0.0
        if inv.get("vendor_name"):
            a = norm_company(inv["vendor_name"])
            nm = max(max(fuzz.token_sort_ratio(a, norm_company(n)), fuzz.token_set_ratio(a, norm_company(n)) * 0.96)
                     for n in names) / 100.0
        tax = bool(inv.get("vendor_tax_id")) and digits_only(inv["vendor_tax_id"]) == digits_only(v["tax_id"])
        score = max(nm, 0.97) if tax and nm >= 0.55 else (0.9 if tax else nm)
        scored.append((score, nm, tax, v))
    scored.sort(key=lambda t: -t[0])
    if scored:
        best = scored[0]
        second = scored[1] if len(scored) > 1 else None
    result = dict(vendor=None, score=best[0] if best else 0.0, name_score=best[1] if best else 0.0,
                  tax_id_match=best[2] if best else False, method="none",
                  runner_up=(second[3]["legal_name"], round(second[0], 2)) if second else None)
    if best and best[0] >= 0.75:
        result["vendor"] = best[3]
        result["method"] = "tax id + name" if best[2] else "name match"
    return result


def check_vendor(inv, rv, text_source):
    """Findings about who the vendor is and whether we can pay them."""
    out = []
    v = rv["vendor"]
    if v is None:
        out.append(F("VENDOR_UNKNOWN", "Vendor is not in the approved vendor master",
                     f"\"{inv.get('vendor_name') or 'unknown'}\" does not match any vendor on file "
                     f"(best name match {rv['name_score'] * 100:.0f}%). We cannot pay an unapproved vendor.",
                     "fail", "REJECT", "procurement", expected="approved vendor", found=inv.get("vendor_name"),
                     stage="vendor"))
        return out
    if rv["score"] < 0.9:
        out.append(F("VENDOR_UNCERTAIN", "Vendor identity only partly matches",
                     f"Matched to {v['legal_name']} at {rv['score'] * 100:.0f}% confidence via {rv['method']}.",
                     "fail", "ESCALATE", "ap", expected="name match >= 90%", found=f"{rv['score'] * 100:.0f}%", stage="vendor"))
    else:
        out.append(F("VENDOR_MATCH", "Vendor identified",
                     f"{v['legal_name']} ({v['vendor_id']}) via {rv['method']} at {rv['score'] * 100:.0f}%.", "pass", stage="vendor"))
    if v["status"] == "blocked":
        out.append(F("VENDOR_BLOCKED", "Vendor is blocked",
                     f"{v['legal_name']} is on hold: {v['status_note'] or 'blocked in vendor master'}. No payment may be released.",
                     "fail", "REJECT", "compliance", expected="status = approved", found=v["status"], stage="vendor"))
    elif v["status"] != "approved":
        out.append(F("VENDOR_NOT_APPROVED", "Vendor is not fully approved", f"Vendor status is '{v['status']}'.",
                     "fail", "ESCALATE", "procurement", expected="approved", found=v["status"], stage="vendor"))
    else:
        out.append(F("VENDOR_STATUS", "Vendor is approved and active", f"Status '{v['status']}' in the vendor master.", "pass", stage="vendor"))

    # tax id on the invoice vs master
    if inv.get("vendor_tax_id"):
        if digits_only(inv["vendor_tax_id"]) != digits_only(v["tax_id"]):
            out.append(F("TAXID_MISMATCH", "Tax ID differs from vendor master",
                         f"Invoice shows {inv['vendor_tax_id']}, master has {v['tax_id']}.", "fail", "ESCALATE", "ap",
                         expected=v["tax_id"], found=inv["vendor_tax_id"], stage="vendor"))
        else:
            out.append(F("TAXID_OK", "Tax ID matches vendor master", f"{v['tax_id']}", "pass", stage="vendor"))

    # bank details - the classic payment-fraud signal
    acct = digits_only(inv.get("bank_account"))
    master = digits_only(v["bank_account"])
    changed_hint = bool(inv.get("flags", {}).get("payment_instruction_change"))
    if acct:
        if acct == master:
            out.append(F("BANK_MATCH", "Bank account matches vendor master", f"Account ending {master[-4:]}.", "pass", stage="vendor"))
        elif text_source == "ocr" and Levenshtein.distance(acct, master) <= 1 and not changed_hint:
            out.append(F("BANK_OCR_DOUBT", "Bank account differs by one digit (likely OCR misread)",
                         f"Invoice reads ...{acct[-4:]}, master has ...{master[-4:]}. Scanned text is unreliable, so verify visually.",
                         "warn", stage="vendor"))
        else:
            extra = " The invoice also announces new remittance details." if changed_hint else ""
            out.append(F("BANK_MISMATCH", "Bank account differs from vendor master",
                         f"Invoice asks for payment to account ending {acct[-4:]}, master has ending {master[-4:]}.{extra} "
                         "A changed account is the most common payment-fraud pattern: do not pay until verified by callback.",
                         "fail", "ESCALATE", "treasury", expected=f"...{master[-4:]}", found=f"...{acct[-4:]}", stage="vendor"))
    elif changed_hint:
        out.append(F("BANK_CHANGE_NOTICE", "Invoice announces new payment details",
                     "The document says remittance details changed but no account could be read.", "fail", "ESCALATE",
                     "treasury", stage="vendor"))
    else:
        out.append(F("BANK_ABSENT", "No bank details on invoice", "Payment would use the account on file.", "info", stage="vendor"))

    # look-alike e-mail domain (only trust digital text, OCR can garble letters)
    dom, mdom = email_domain(inv.get("vendor_email")), email_domain(v["email"])
    if dom and mdom and dom != mdom:
        sim = fuzz.ratio(dom, mdom)
        if text_source == "ocr":
            out.append(F("EMAIL_DOMAIN_UNVERIFIED", "E-mail domain could not be verified on a scan",
                         f"Scanned text reads {dom}; master has {mdom}. OCR can garble letters so this is not treated as a finding.",
                         "info", stage="vendor"))
        elif sim >= 75:
            out.append(F("EMAIL_LOOKALIKE", "Contact e-mail uses a look-alike domain",
                         f"Invoice contact domain {dom} is very close to, but not the same as, the vendor's {mdom} ({sim:.0f}% similar).",
                         "fail", "ESCALATE", "treasury", expected=mdom, found=dom, stage="vendor"))
        else:
            out.append(F("EMAIL_DOMAIN_DIFF", "Contact e-mail domain differs from vendor master",
                         f"{dom} vs {mdom}.", "warn", stage="vendor"))
    elif dom and mdom:
        out.append(F("EMAIL_OK", "Contact e-mail domain matches vendor master", dom, "pass", stage="vendor"))
    return out


# ================================================================= duplicates
def check_duplicates(inv, vendor, file_hash, policy, run_id=None, as_of=None):
    """Compare against the ledger (paid / approved / awaiting review). Returns (findings, info)."""
    out, info = [], dict(matches=[], supersedes=None)
    num, norm, core = inv.get("number"), norm_invoice_number(inv.get("number")), invoice_number_core(inv.get("number"))
    total, date = inv.get("total"), inv.get("date")
    vid = vendor["vendor_id"] if vendor else None
    cond, args = ["file_hash = ?"], [file_hash]
    if vid:
        cond.append("vendor_id = ?")
        args.append(vid)
    ex = [run_id or -1] + db.excluded_runs()
    cand = db.rows(f"SELECT * FROM ledger WHERE ({' OR '.join(cond)}) AND (run_id IS NULL OR run_id NOT IN ({','.join('?' * len(ex))}))",
                   (*args, *ex))
    dup_hits, held = [], []
    for r in cand:
        same_vendor = vid is not None and r["vendor_id"] == vid
        sim = 0.0
        if core and r["core_number"]:
            sim = max(fuzz.ratio(core, r["core_number"]), fuzz.ratio(norm, r["norm_number"])) / 100.0
        amount_same = total is not None and r["total"] is not None and abs(total - r["total"]) <= 0.01
        date_close = bool(date and r["invoice_date"]) and _days(date, r["invoice_date"]) <= policy["duplicate_date_window_days"]
        kind = None
        if r["file_hash"] and r["file_hash"] == file_hash and r["status"] in ACTIVE_LEDGER:
            kind, sim = "identical file", 1.0
        elif same_vendor and core and r["core_number"] == core and amount_same:
            kind = "same invoice number and amount"
        elif same_vendor and sim >= policy["duplicate_similarity"] and amount_same and date_close:
            kind = "near-identical number, same amount and date"
        elif same_vendor and amount_same and date and r["invoice_date"] == date and not num:
            kind = "same amount and date (number missing)"
        elif same_vendor and core and r["core_number"] == core and not amount_same:
            kind = "number reused with different amount"
        elif same_vendor and r["status"] == "held" and amount_same and date_close:
            kind = "same amount and date as a held invoice"
        if not kind:
            continue
        m = dict(ledger_id=r["id"], run_id=r["run_id"], invoice_number=r["invoice_number"], total=r["total"],
                 invoice_date=r["invoice_date"], status=r["status"], paid_date=r["paid_date"], similarity=round(sim, 2),
                 kind=kind, source=r["source"])
        if r["status"] in ACTIVE_LEDGER:
            dup_hits.append(m)
        elif r["status"] == "held" and kind != "identical file":
            held.append(m)
    info["matches"] = dup_hits
    if dup_hits:
        top = sorted(dup_hits, key=lambda m: (m["kind"] == "number reused with different amount", -m["similarity"]))[0]
        when = f" and was paid on {top['paid_date']}" if top["status"] == "paid" and top["paid_date"] else \
               (" and is already approved" if top["status"] == "approved" else " and is awaiting review")
        if top["kind"] == "number reused with different amount":
            out.append(F("INVOICE_NUMBER_REUSED", "Invoice number was used before with a different amount",
                         f"{num} matches {top['invoice_number']} (${top['total']:,.2f}, {top['invoice_date']}) on file but the amount differs "
                         f"(${total:,.2f}). Could be a corrected invoice or a billing error.",
                         "fail", "ESCALATE", "ap", expected=f"${top['total']:,.2f}", found=f"${total:,.2f}", stage="duplicates"))
        else:
            out.append(F("DUPLICATE_INVOICE", "Duplicate of an invoice already on file",
                         f"Matches {top['invoice_number']} dated {top['invoice_date']} for ${top['total']:,.2f}{when}. "
                         f"Detected by: {top['kind']} (number similarity {top['similarity'] * 100:.0f}%). "
                         "The file hash and number format differ, but the vendor, amount and date are the same invoice.",
                         "fail", "REJECT", "ap", expected="no earlier copy", found=f"{top['invoice_number']} ({top['status']})",
                         stage="duplicates"))
    else:
        out.append(F("DUPLICATE_NONE", "No duplicate found",
                     "Checked file hash, normalised invoice number and (vendor, amount, date) fingerprint against the ledger.",
                     "pass", stage="duplicates"))
    if held and not dup_hits:
        h = held[0]
        info["supersedes"] = h
        out.append(F("RESUBMISSION", "Resubmission of an invoice that was on hold",
                     f"Matches held run RUN-{h['run_id']:04d} ({h['kind']}). The earlier hold is superseded rather than treated as a duplicate."
                     if h["run_id"] else "Matches an earlier held invoice; it is superseded.", "info", stage="duplicates"))
    return out, info


# ================================================================= PO matching
def _sim(a, b):
    a, b = norm_company(a) if False else (a or "").lower(), (b or "").lower()
    if not a or not b:
        return 0.0
    return max(fuzz.token_set_ratio(a, b) * 0.97, fuzz.partial_ratio(a, b) * 0.9, fuzz.token_sort_ratio(a, b)) / 100.0


def _sku_eq(a, b):
    a, b = re.sub(r"[^A-Z0-9]", "", (a or "").upper()), re.sub(r"[^A-Z0-9]", "", (b or "").upper())
    return bool(a) and bool(b) and a == b


def po_state(po_number):
    """PO lines enriched with consumed / remaining figures."""
    lines = db.po_lines(po_number)
    cons = db.po_consumption(po_number)
    out = []
    for l in lines:
        cq, ca = cons.get(l["line_no"], (0.0, 0.0))
        ordered_amt = round(l["qty_ordered"] * l["unit_price"], 2)
        out.append(dict(l, consumed_qty=cq, consumed_amt=ca, ordered_amt=ordered_amt,
                        remaining_qty=round(l["qty_ordered"] - cq, 4), remaining_amt=round(ordered_amt - ca, 2),
                        lump=l["qty_ordered"] <= 1.0))
    return out


def _line_score(il, pl):
    sku = _sku_eq(il.get("sku"), pl["sku"])
    ds = max(_sim(il.get("description"), pl["description"]), 1.0 if sku else 0.0)
    if pl["lump"]:
        # amount fit against what is left on the line
        amt = il.get("net_amount") or il.get("amount") or 0
        ps = 1.0 if pl["remaining_amt"] + 0.01 >= amt else max(0.0, 1 - (amt - pl["remaining_amt"]) / max(amt, 1))
    else:
        up = il.get("net_unit") or il.get("unit_price") or 0
        diff = abs(up - pl["unit_price"]) / pl["unit_price"] if pl["unit_price"] else 1
        ps = 1.0 if diff <= 0.05 else (0.5 if diff <= 0.15 else 0.0)
    return 0.6 * ds + 0.4 * ps, ds, sku


def rank_po_candidates(inv, vendor_id, policy):
    cands = []
    for p in db.rows("SELECT * FROM pos WHERE vendor_id=? AND status='open'", (vendor_id,)):
        st = po_state(p["po_number"])
        rem_total = sum(l["remaining_amt"] for l in st)
        if rem_total <= 0.5:
            continue
        lines = [l for l in inv.get("lines", [])] or []
        scores, weights = [], []
        for il in lines:
            best = 0.0
            for pl in st:
                if pl["remaining_amt"] <= 0.01:
                    continue
                s, _, _ = _line_score(il, pl)
                best = max(best, s)
            scores.append(best)
            weights.append(max(il.get("net_amount") or il.get("amount") or 1.0, 1.0))
        agg = (sum(s * w for s, w in zip(scores, weights)) / sum(weights)) if scores else 0.0
        net = inv.get("net") or 0.0
        tol = max(policy["total_tolerance_abs"], policy["total_tolerance_pct"] / 100 * rem_total)
        fit = 1.0 if net <= rem_total + tol else max(0.0, 1 - (net - rem_total) / rem_total)
        # header-level hint: the PO's own description vs the invoice lines (bundled invoices)
        hdr = max([_sim(il.get("description"), p["description"]) for il in lines] or [0.0])
        agg = max(agg, 0.85 * hdr * fit) if len(lines) <= 1 else agg
        score = 0.75 * agg + 0.25 * fit
        cands.append(dict(po_number=p["po_number"], score=round(score, 3), line_score=round(agg, 3),
                          amount_fit=round(fit, 3), remaining=round(rem_total, 2), description=p["description"]))
    cands.sort(key=lambda c: -c["score"])
    return cands


def match_po(inv, vendor, policy):
    """Find the PO (printed, fuzzy-printed or inferred) and run line-level + cumulative checks.
    Returns (findings, info)."""
    out, info = [], dict(method=None, po=None, printed=inv.get("po_ref"), candidates=[], lines=[], confidence=0.0,
                         remaining_before=None, remaining_after=None, billed_to_date=None)
    vid = vendor["vendor_id"] if vendor else None
    printed = norm_po(inv.get("po_ref"))
    po = None

    if printed:
        po = db.po(printed)
        if po:
            info["method"] = "explicit"
            info["confidence"] = 0.99
        elif vid:
            # printed ref not found: maybe one digit off (OCR / typo) among this vendor's POs
            for p in db.rows("SELECT * FROM pos WHERE vendor_id=?", (vid,)):
                if Levenshtein.distance(p["po_number"], printed) == 1:
                    po = p
                    info["method"] = "fuzzy"
                    info["confidence"] = 0.8
                    out.append(F("PO_REF_FUZZY", "Printed PO number not found; matched to a near-identical PO",
                                 f"Invoice says {printed}; closest PO for this vendor is {p['po_number']}.", "warn", stage="po_match"))
                    break
        if po is None:
            out.append(F("PO_NOT_FOUND", "Printed PO number does not exist",
                         f"The invoice cites {printed} but no such PO is on file.", "info", stage="po_match"))

    if po is None and vid:
        cands = rank_po_candidates(inv, vid, policy)
        info["candidates"] = cands[:3]
        if cands:
            top = cands[0]
            second = cands[1]["score"] if len(cands) > 1 else 0.0
            if top["score"] >= policy["implied_po_min_confidence"] and top["score"] - second >= policy["implied_po_min_margin"]:
                po = db.po(top["po_number"])
                info["method"] = "inferred"
                info["confidence"] = top["score"]
                out.append(F("PO_INFERRED", f"No usable PO printed: inferred {top['po_number']}",
                             f"Best match among {len(cands)} open PO(s) for this vendor at {top['score'] * 100:.0f}% "
                             f"(line items {top['line_score'] * 100:.0f}%, amount fit {top['amount_fit'] * 100:.0f}%)"
                             + (f", next best {second * 100:.0f}%." if second else "."),
                             "warn", stage="po_match"))
            else:
                names = ", ".join(f"{c['po_number']} ({c['score'] * 100:.0f}%)" for c in cands[:3])
                out.append(F("PO_AMBIGUOUS", "Could not confidently match a PO",
                             f"Candidates: {names}. Required: >= {policy['implied_po_min_confidence'] * 100:.0f}% and a clear lead.",
                             "fail", "ESCALATE" if printed is None and policy.get("require_po") else "HOLD", "buyer",
                             expected="a single clear PO", found=names, stage="po_match"))
                return out, info
        elif policy.get("require_po"):
            out.append(F("PO_NONE_AVAILABLE", "No open PO exists for this vendor",
                         "Invoice has no PO and the vendor has no open purchase orders. Non-PO spend needs a buyer's approval.",
                         "fail", "ESCALATE", "buyer", stage="po_match"))
            return out, info

    if po is None:
        if policy.get("require_po"):
            out.append(F("PO_MISSING", "No PO reference and the vendor is unknown",
                         "A PO match could not be attempted.", "fail", "HOLD", "vendor", stage="po_match"))
        return out, info

    info["po"] = po
    out.append(F("PO_FOUND", f"PO {po['po_number']} located ({info['method']})",
                 f"{po['description']} - buyer {po['buyer']}, cost centre {po['cost_center']}, issued {po['issue_date']}.",
                 "pass" if info["method"] == "explicit" else "info", stage="po_match"))
    if vid and po["vendor_id"] != vid:
        other = db.vendor(po["vendor_id"])
        out.append(F("PO_VENDOR_MISMATCH", "PO belongs to a different vendor",
                     f"{po['po_number']} was issued to {other['legal_name'] if other else po['vendor_id']}, not this vendor.",
                     "fail", "ESCALATE", "buyer", expected=other["legal_name"] if other else po["vendor_id"],
                     found=vendor["legal_name"], stage="po_match"))
    if po["status"] != "open":
        out.append(F("PO_NOT_OPEN", f"PO is {po['status']}",
                     f"{po['po_number']} is {po['status']}: it was fully invoiced or cancelled, so further billing is not authorised.",
                     "fail", "ESCALATE", "buyer", expected="open", found=po["status"], stage="po_match"))
    else:
        out.append(F("PO_OPEN", "PO is open", f"{po['po_number']} accepts further invoices.", "pass", stage="po_match"))

    # ---------------- line-level alignment
    st = po_state(po["po_number"])
    remaining_total = round(sum(l["remaining_amt"] for l in st), 2)
    ordered_total = round(sum(l["ordered_amt"] for l in st), 2)
    info["remaining_before"] = remaining_total
    info["ordered_total"] = ordered_total
    info["billed_to_date"] = round(ordered_total - remaining_total, 2)

    inv_lines = inv.get("lines", [])
    pairs, used = [], set()
    scored = []
    for i, il in enumerate(inv_lines):
        for pl in st:
            s, ds, sku = _line_score(il, pl)
            scored.append((s + (0.5 if sku else 0), i, pl["line_no"], ds, sku))
    scored.sort(reverse=True)
    taken_i = set()
    for s, i, ln, ds, sku in scored:
        if i in taken_i or ln in used:
            continue
        if sku or ds >= 0.55:
            pairs.append((i, ln, ds, sku))
            taken_i.add(i)
            used.add(ln)
    pairs.sort()
    unmatched = [i for i in range(len(inv_lines)) if i not in taken_i]
    tol_abs, tol_pct = policy["total_tolerance_abs"], policy["total_tolerance_pct"] / 100.0
    price_tol = policy["price_tolerance_pct"] / 100.0
    line_fail = False
    expected_net = 0.0
    for i, ln, ds, sku in pairs:
        il = inv_lines[i]
        pl = next(l for l in st if l["line_no"] == ln)
        row = dict(inv_line=i, po_line=ln, sku=pl["sku"], po_description=pl["description"], inv_description=il.get("description"),
                   match=round(max(ds, 1.0 if sku else 0), 2), checks=[])
        amt = il.get("net_amount") if il.get("net_amount") is not None else il.get("amount")
        if pl["lump"]:
            over = amt - pl["remaining_amt"]
            allowed = max(tol_abs, tol_pct * pl["remaining_amt"])
            pct_of_line = amt / pl["ordered_amt"] * 100 if pl["ordered_amt"] else 0
            row.update(kind="lump", billed=amt, remaining=pl["remaining_amt"], ordered=pl["ordered_amt"],
                       consumed=pl["consumed_amt"])
            expected_net += amt
            if over > allowed:
                line_fail = True
                out.append(F("PO_OVERBILLED", "Cumulative billing exceeds the PO",
                             f"This invoice bills ${amt:,.2f} against a PO line with ${pl['remaining_amt']:,.2f} left "
                             f"(PO ${pl['ordered_amt']:,.2f}, ${pl['consumed_amt']:,.2f} already billed). Over by ${over:,.2f} "
                             f"({over / max(pl['remaining_amt'], 1) * 100:.1f}%), beyond the ${allowed:,.2f} tolerance.",
                             "fail", "ESCALATE", "buyer", expected=f"<= ${pl['remaining_amt']:,.2f}", found=f"${amt:,.2f}",
                             stage="po_match"))
                row["status"] = "over"
            else:
                row["status"] = "ok"
                cum = (pl["consumed_amt"] + amt) / pl["ordered_amt"] * 100 if pl["ordered_amt"] else 0
                out.append(F("PO_PARTIAL_OK" if pl["consumed_amt"] or pct_of_line < 99 else "PO_FULL_OK",
                             "Within the PO balance" if pl["consumed_amt"] or pct_of_line < 99 else "Matches the fixed-price PO line",
                             f"Bills ${amt:,.2f} ({pct_of_line:.0f}% of the PO line); ${pl['consumed_amt']:,.2f} billed before, "
                             f"${max(pl['remaining_amt'] - amt, 0):,.2f} left afterwards ({cum:.0f}% billed to date).",
                             "pass", stage="po_match"))
        else:
            up = il.get("net_unit") if il.get("net_unit") is not None else il.get("unit_price")
            qty = il.get("qty") or 0
            diff = (up - pl["unit_price"]) / pl["unit_price"] if pl["unit_price"] else 0
            avail_recv = pl["qty_received"] - pl["consumed_qty"] if pl["needs_receipt"] else pl["remaining_qty"]
            row.update(kind="qty", billed_qty=qty, unit_price=up, po_unit_price=pl["unit_price"], price_var_pct=round(diff * 100, 2),
                       ordered_qty=pl["qty_ordered"], consumed_qty=pl["consumed_qty"], remaining_qty=pl["remaining_qty"],
                       received_qty=pl["qty_received"], available_to_bill=avail_recv)
            expected_net += qty * pl["unit_price"]
            ok = True
            if abs(up - pl["unit_price"]) > max(0.01, price_tol * pl["unit_price"]):
                ok = line_fail = True
                out.append(F("PRICE_VARIANCE", f"Unit price above PO on {pl['sku']}",
                             f"{il.get('description') or pl['sku']}: invoiced ${up:,.2f} vs PO ${pl['unit_price']:,.2f} "
                             f"({diff * 100:+.1f}%), outside the {policy['price_tolerance_pct']:g}% tolerance.",
                             "fail", "ESCALATE", "buyer", expected=f"${pl['unit_price']:,.2f} +/-{policy['price_tolerance_pct']:g}%",
                             found=f"${up:,.2f} ({diff * 100:+.1f}%)", stage="po_match"))
            elif abs(diff) > 1e-6:
                out.append(F("PRICE_WITHIN_TOL", f"Unit price differs slightly on {pl['sku']}",
                             f"{il.get('description') or pl['sku']}: ${up:,.2f} vs PO ${pl['unit_price']:,.2f} ({diff * 100:+.2f}%), "
                             f"inside the {policy['price_tolerance_pct']:g}% tolerance.", "warn", stage="po_match"))
            if qty > pl["remaining_qty"] * (1 + 1e-9) + 1e-9:
                ok = line_fail = True
                out.append(F("QTY_OVER_ORDER", f"Billed quantity exceeds what was ordered on {pl['sku']}",
                             f"Billing {qty:g}; only {pl['remaining_qty']:g} of {pl['qty_ordered']:g} remain unbilled.",
                             "fail", "ESCALATE", "buyer", expected=f"<= {pl['remaining_qty']:g}", found=f"{qty:g}", stage="po_match"))
            elif pl["needs_receipt"] and qty > avail_recv * (1 + policy["qty_over_receipt_tolerance_pct"] / 100) + 1e-9:
                ok = line_fail = True
                out.append(F("QTY_OVER_RECEIPT", f"3-way match failed on {pl['sku']}: billed more than received",
                             f"Invoice bills {qty:g} {pl['description'].lower()}; goods receipt shows only {avail_recv:g} received "
                             f"and not yet billed. Paying would settle goods that have not arrived.",
                             "fail", "ESCALATE", "receiving", expected=f"<= {avail_recv:g} received", found=f"{qty:g} billed",
                             stage="po_match"))
            if ok:
                out.append(F("LINE_OK", f"Line {pl['sku']} matches PO line {ln}",
                             f"{qty:g} x ${up:,.2f}; ordered {pl['qty_ordered']:g}, received {pl['qty_received']:g}, "
                             f"previously billed {pl['consumed_qty']:g}.", "pass", stage="po_match"))
            row["status"] = "ok" if ok else "fail"
        info["lines"].append(row)

    if unmatched:
        if len(inv_lines) >= 2 or (len(inv_lines) == 1 and not pairs and info["method"] != "inferred"):
            for i in unmatched:
                il = inv_lines[i]
                if len(inv_lines) == 1:
                    # a single bundled line: fall back to a header-level comparison below
                    continue
                line_fail = True
                out.append(F("LINE_NOT_ON_PO", "Invoice line is not on the PO",
                             f"\"{il.get('description')}\" (${il.get('amount', 0):,.2f}) could not be matched to any PO line.",
                             "fail", "ESCALATE", "buyer", found=il.get("description"), stage="po_match"))
    bundled = (len(inv_lines) <= 1 and not pairs) or not inv_lines
    if bundled:
        expected_net = None
        out.append(F("HEADER_MATCH", "Bundled invoice matched at header level",
                     "The invoice has no itemised lines that map to PO lines, so the net amount is compared with the PO balance.",
                     "info", stage="po_match"))

    # ---------------- header-level: net vs balance and vs expected
    net = inv.get("net")
    if net is not None:
        allowed_bal = max(tol_abs, tol_pct * max(remaining_total, 1))
        info["remaining_after"] = round(remaining_total - net, 2)
        if net > remaining_total + allowed_bal and not line_fail:
            out.append(F("PO_OVERBILLED", "Invoice exceeds the remaining PO balance",
                         f"Net ${net:,.2f} vs ${remaining_total:,.2f} remaining on {po['po_number']} "
                         f"(${info['billed_to_date']:,.2f} already billed of ${ordered_total:,.2f}).",
                         "fail", "ESCALATE", "buyer", expected=f"<= ${remaining_total:,.2f}", found=f"${net:,.2f}", stage="po_match"))
            line_fail = True
        if expected_net is not None and not line_fail:
            allowed = max(tol_abs, tol_pct * expected_net)
            if abs(net - expected_net) > allowed:
                out.append(F("TOTAL_VARIANCE", "Net amount differs from PO-priced total",
                             f"Net ${net:,.2f} vs ${expected_net:,.2f} when billed quantities are priced at PO rates.",
                             "fail", "ESCALATE", "buyer", expected=f"${expected_net:,.2f}", found=f"${net:,.2f}", stage="po_match"))
            else:
                diff = net - expected_net
                out.append(F("TOTAL_MATCH", "Invoice net matches PO-priced total",
                             f"Net ${net:,.2f} vs ${expected_net:,.2f} expected" + (f" (difference ${diff:+,.2f}, within tolerance)." if abs(diff) > 0.005 else "."),
                             "pass", stage="po_match"))
        elif expected_net is None and not line_fail:
            out.append(F("BALANCE_OK", "Within the PO balance",
                         f"Net ${net:,.2f} against ${remaining_total:,.2f} remaining; ${remaining_total - net:,.2f} left after this invoice.",
                         "pass", stage="po_match"))

    # ---------------- tax against the PO's rate
    tax, rate = inv.get("tax"), po["tax_rate_pct"]
    if tax is not None and net is not None:
        exp_tax = round(net * rate / 100, 2)
        if abs(tax - exp_tax) > max(0.10, 0.005 * max(exp_tax, 1)):
            out.append(F("TAX_MISMATCH", "Tax differs from the PO tax rate",
                         f"Invoice tax ${tax:,.2f}; {rate:g}% of net ${net:,.2f} is ${exp_tax:,.2f}.", "fail", "ESCALATE", "ap",
                         expected=f"${exp_tax:,.2f}", found=f"${tax:,.2f}", stage="po_match"))
        else:
            out.append(F("TAX_OK", f"Tax consistent with {rate:g}% PO rate", f"${tax:,.2f} on net ${net:,.2f}.", "pass", stage="po_match"))
    return out, info

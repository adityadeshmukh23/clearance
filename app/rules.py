"""Turn an extraction into a normalised invoice, then run data-quality and policy checks."""
from __future__ import annotations

import datetime as dt

from .matching import F
from .normalize import terms_days

CRITICAL = ("vendor_name", "invoice_number", "invoice_date", "total")


def _v(ex, k):
    return ex["fields"].get(k, {}).get("value")


def _c(ex, k):
    return ex["fields"].get(k, {}).get("confidence", 0.0) or 0.0


def build_invoice(ex, text_source):
    """Flatten the extraction, derive the net amount, detect tax-inclusive pricing."""
    lines = []
    for li in ex.get("line_items", []):
        lines.append(dict(sku=li.get("sku") or "", description=li.get("description") or "", qty=li.get("qty"),
                          unit_price=li.get("unit_price"), amount=li.get("amount"), confidence=li.get("confidence", 0.8),
                          boxes=li.get("boxes", [])))
    total, sub, tax = _v(ex, "total"), _v(ex, "subtotal"), _v(ex, "tax_amount")
    ship, disc = _v(ex, "shipping") or 0.0, _v(ex, "discount") or 0.0
    lines_sum = round(sum(l["amount"] or 0 for l in lines), 2)
    inclusive = bool(sub is None and tax is not None and total is not None and lines and
                     abs(lines_sum - total) <= max(0.05, 0.002 * total) and tax > 0)
    if sub is not None:
        net = round(sub - disc + ship, 2)
    elif inclusive:
        net = round(total - tax, 2)
    elif tax is not None and total is not None:
        net = round(total - tax, 2)
    elif lines:
        net = lines_sum
    else:
        net = None
    scale = (net / lines_sum) if (inclusive and lines_sum) else 1.0
    for l in lines:
        l["net_amount"] = round((l["amount"] or 0) * scale, 2) if l["amount"] is not None else None
        l["net_unit"] = round((l["unit_price"] or 0) * scale, 4) if l["unit_price"] is not None else None
    return dict(
        vendor_name=_v(ex, "vendor_name"), vendor_tax_id=_v(ex, "vendor_tax_id"), vendor_email=_v(ex, "vendor_email"),
        number=_v(ex, "invoice_number"), date=_v(ex, "invoice_date"), due_date=_v(ex, "due_date"),
        terms=_v(ex, "payment_terms"), po_ref=_v(ex, "po_reference"), currency=_v(ex, "currency") or "USD",
        subtotal=sub, tax=tax, tax_rate=_v(ex, "tax_rate"), shipping=ship, discount=disc, total=total, net=net,
        tax_inclusive=inclusive, lines=lines, lines_sum=lines_sum,
        bank_account=_v(ex, "bank_account"), bank_routing=_v(ex, "bank_routing"), bank_name=_v(ex, "bank_name"),
        flags=ex.get("flags", {}), conf={k: _c(ex, k) for k in ex["fields"]}, text_source=text_source)


def check_quality(inv, ex, policy, as_of, hidden_words=None):
    """Required fields, confidence, arithmetic, dates, hidden text."""
    out = []
    src = inv["text_source"]
    min_conf = policy["min_field_confidence"]
    labels = dict(vendor_name="vendor name", invoice_number="invoice number", invoice_date="invoice date", total="total amount")
    # Fields that move money or identify the invoice must be read confidently (hard stop). Vendor name and date are
    # cross-checked elsewhere (vendor master match, date vs terms/ledger), so a soft reading is a note, not a block.
    HARD = ("invoice_number", "total")
    for k in CRITICAL:
        val = _v(ex, k)
        if val in (None, ""):
            out.append(F(f"MISSING_{k.upper()}", f"No {labels[k]} found",
                         f"The {labels[k]} is not on the document (or is unreadable). Without it the invoice cannot be matched or paid.",
                         "fail", "HOLD", "vendor", expected=labels[k], found="missing", stage="verify"))
        elif _c(ex, k) < min_conf:
            hard = k in HARD
            out.append(F(f"LOWCONF_{k.upper()}", f"Low confidence reading the {labels[k]}",
                         f"Read as '{val}' at {_c(ex, k) * 100:.0f}% confidence (minimum {min_conf * 100:.0f}%). "
                         + ("A person should confirm the value before anything is paid." if hard else
                            "Not blocking: this field is cross-checked elsewhere in the process."),
                         "fail" if hard else "warn", "ESCALATE" if hard else None, "ap" if hard else None,
                         expected=f">= {min_conf * 100:.0f}%", found=f"{_c(ex, k) * 100:.0f}%", stage="verify"))
        else:
            out.append(F(f"FIELD_{k.upper()}", f"{labels[k].capitalize()} read", f"'{val}' at {_c(ex, k) * 100:.0f}% confidence.", "pass", stage="verify"))

    tol = policy["arithmetic_tolerance"]
    total, sub, tax = inv["total"], inv["subtotal"], inv["tax"]
    arith_outcome = ("ESCALATE", "ap") if src == "ocr" else ("HOLD", "vendor")
    if total is not None:
        if sub is not None:
            exp = round(sub - inv["discount"] + inv["shipping"] + (tax or 0), 2)
            if abs(exp - total) > tol:
                out.append(F("ARITH_TOTAL", "Totals do not add up",
                             f"Subtotal ${sub:,.2f}" + (f" - discount ${inv['discount']:,.2f}" if inv["discount"] else "") +
                             (f" + shipping ${inv['shipping']:,.2f}" if inv["shipping"] else "") + f" + tax ${tax or 0:,.2f} = ${exp:,.2f}, "
                             f"but the invoice total reads ${total:,.2f}.", "fail", *arith_outcome, expected=f"${exp:,.2f}",
                             found=f"${total:,.2f}", stage="verify"))
            else:
                out.append(F("ARITH_OK", "Subtotal + tax = total", f"${exp:,.2f} re-added in code, matches ${total:,.2f}.", "pass", stage="verify"))
        elif inv["tax_inclusive"]:
            out.append(F("ARITH_INCL", "Tax-inclusive pricing detected",
                         f"Line price ${inv['lines_sum']:,.2f} equals the total, with ${tax:,.2f} tax inside it. Net derived as ${inv['net']:,.2f}.",
                         "info", stage="verify"))
        if inv["lines"] and sub is not None and not inv["tax_inclusive"]:
            if abs(inv["lines_sum"] - sub) > tol + 0.005 * sub:
                out.append(F("ARITH_LINES", "Line items do not add up to the subtotal",
                             f"Lines sum to ${inv['lines_sum']:,.2f} but the subtotal is ${sub:,.2f}.", "fail", *arith_outcome,
                             expected=f"${sub:,.2f}", found=f"${inv['lines_sum']:,.2f}", stage="verify"))
            else:
                out.append(F("ARITH_LINES_OK", "Line items add up to the subtotal", f"{len(inv['lines'])} line(s), ${inv['lines_sum']:,.2f}.", "pass", stage="verify"))
        bad = [l for l in inv["lines"] if l["qty"] and l["unit_price"] and l["amount"] is not None
               and abs(l["qty"] * l["unit_price"] - l["amount"]) > max(0.05, 0.005 * l["amount"])]
        if bad:
            l = bad[0]
            out.append(F("ARITH_LINE_MATH", "A line's quantity x price does not equal its amount",
                         f"{l['description'][:48]}: {l['qty']:g} x ${l['unit_price']:,.2f} = ${l['qty'] * l['unit_price']:,.2f}, invoice says ${l['amount']:,.2f}.",
                         "fail", *arith_outcome, stage="verify"))

    # dates
    d = inv["date"]
    if d:
        try:
            dd = dt.date.fromisoformat(d)
            if dd > as_of + dt.timedelta(days=policy["future_date_grace_days"]):
                out.append(F("DATE_FUTURE", "Invoice is dated in the future", f"Dated {d}; today is {as_of.isoformat()}.", "fail", "HOLD", "vendor",
                             expected=f"<= {as_of.isoformat()}", found=d, stage="verify"))
            elif (as_of - dd).days > policy["stale_invoice_days"]:
                out.append(F("DATE_STALE", "Invoice is unusually old", f"Dated {d}, {(as_of - dd).days} days ago (limit {int(policy['stale_invoice_days'])}).",
                             "fail", "ESCALATE", "ap", stage="verify"))
            else:
                out.append(F("DATE_OK", "Invoice date is plausible", f"{d} ({(as_of - dd).days} days old).", "pass", stage="verify"))
            if inv["due_date"] and inv["due_date"] < d:
                out.append(F("DUE_BEFORE_INVOICE", "Due date is before the invoice date", f"{inv['due_date']} < {d}.", "warn", stage="verify"))
        except ValueError:
            pass
    if ex.get("flags", {}).get("date_ambiguous"):
        days = terms_days(inv.get("terms")) if inv.get("terms") else None
        resolved = False
        if d and inv.get("due_date") and days is not None:
            try:
                resolved = (dt.date.fromisoformat(d) + dt.timedelta(days=days)).isoformat() == inv["due_date"]
            except ValueError:
                pass
        if not resolved and d and inv.get("due_date"):
            try:    # no printed terms: a due date 0-90 days after the date also supports the reading
                gap = (dt.date.fromisoformat(inv["due_date"]) - dt.date.fromisoformat(d)).days
                resolved = gap in (7, 10, 14, 15, 20, 21, 30, 45, 60, 90)
            except ValueError:
                pass
        if resolved:
            out.append(F("DATE_DISAMBIGUATED", "Ambiguous date format resolved", f"The date could be read as day/month or month/day; due date {inv['due_date']} confirms {d}.", "pass", stage="verify"))
        else:
            out.append(F("DATE_AMBIGUOUS", "Date format is ambiguous", "Read as month/day (US). Confirm if the vendor is outside the US.", "warn", stage="verify"))

    # hostile content
    fl = ex.get("flags", {})
    if fl.get("hidden_text"):
        out.append(F("HIDDEN_TEXT", "Hidden text inside the document",
                     "White or microscopic text that a person cannot see was found in the file" +
                     (": \"" + " ".join(w.text for w in (hidden_words or []))[:150] + "\"" if hidden_words else "") +
                     ". Legitimate invoices do not contain it. The AI never takes instructions from documents, but this is "
                     "a classic prompt-injection attempt and must be reviewed.",
                     "fail", "ESCALATE", "security", stage="verify"))
    elif fl.get("injection_like_text"):
        out.append(F("INJECTION_TEXT", "Document contains text addressed to an AI reviewer",
                     "Instruction-like text was found on the page. It was ignored, and the document is flagged for review.",
                     "fail", "ESCALATE", "security", stage="verify"))
    else:
        out.append(F("CONTENT_CLEAN", "No hidden or instruction-like text", "Checked visible and hidden text for prompt-injection patterns.", "pass", stage="verify"))
    for a in ex.get("anomalies", []):
        if "Hidden text" in a or "changed payment" in a or "no readable label" in a:
            continue
        out.append(F("ANOMALY", "Reader noted an anomaly", a, "warn", stage="verify"))
    return out


def check_policy(inv, vendor, po, policy, as_of):
    out = []
    total = inv["total"] or 0
    limit = policy["auto_approve_limit"]
    if total > limit:
        out.append(F("OVER_LIMIT", "Above the auto-approval limit",
                     f"${total:,.2f} exceeds the ${limit:,.0f} limit; a budget owner must sign off.", "fail", "ESCALATE", "approver",
                     expected=f"<= ${limit:,.0f}", found=f"${total:,.2f}", stage="policy"))
    else:
        out.append(F("LIMIT_OK", "Within the auto-approval limit", f"${total:,.2f} <= ${limit:,.0f}.", "pass", stage="policy"))
    cur_expected = (po or {}).get("currency") or (vendor or {}).get("currency") or "USD"
    if inv["currency"] and inv["currency"] != cur_expected:
        out.append(F("CURRENCY_MISMATCH", "Currency differs from the PO", f"Invoice in {inv['currency']}, PO/vendor in {cur_expected}.",
                     "fail", "ESCALATE", "treasury", expected=cur_expected, found=inv["currency"], stage="policy"))
    else:
        out.append(F("CURRENCY_OK", "Currency matches", f"{inv['currency']}.", "pass", stage="policy"))
    if po and inv["date"] and po["issue_date"] > inv["date"]:
        out.append(F("PO_AFTER_INVOICE", "PO was raised after the invoice date",
                     f"PO {po['issue_date']} vs invoice {inv['date']}: possible retroactive PO.", "fail", "ESCALATE", "buyer", stage="policy"))
    if vendor and inv["terms"]:
        a, b = terms_days(inv["terms"]), terms_days(vendor["payment_terms"])
        if a is not None and b is not None and a != b:
            out.append(F("TERMS_DIFFER", "Payment terms differ from the vendor master",
                         f"Invoice says {inv['terms']}, contract terms are {vendor['payment_terms']}. We pay on contract terms.", "warn", stage="policy"))
    return out


def due_date_for(inv, vendor):
    days = terms_days(vendor["payment_terms"]) if vendor else terms_days(inv.get("terms"))
    if days is None or not inv.get("date"):
        return inv.get("due_date")
    return (dt.date.fromisoformat(inv["date"]) + dt.timedelta(days=days)).isoformat()

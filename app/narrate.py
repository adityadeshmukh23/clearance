"""Human-readable output: headline, plain-English reasons, next action, and the drafted artefacts
(ERP payload, vendor e-mail, internal escalation brief). Templates are deterministic; an AI polish
step may rewrite the prose, but it is never allowed to change the facts (see guard_numbers)."""
from __future__ import annotations

import re

from .rules import due_date_for

ROUTE_LABEL = dict(vendor="the vendor", ap="an AP specialist", buyer="the PO buyer", treasury="Treasury", receiving="Receiving and the buyer",
                   approver="the budget owner", security="the AP lead and Security", compliance="Compliance",
                   procurement="Procurement (vendor onboarding)", payment_run="the payment run")

NEXT_ACTION = dict(
    BANK_MISMATCH="Call the vendor on the phone number in the vendor master, not any number printed on the invoice, and get the account change confirmed in writing by a second approver before any payment is released.",
    BANK_CHANGE_NOTICE="Verify the change of remittance details with the vendor by callback to the number on file before any payment.",
    EMAIL_LOOKALIKE="Do not reply to the e-mail on the invoice. Contact the vendor through the details in the vendor master.",
    DUPLICATE_INVOICE="No payment is made. The vendor is told which earlier invoice this duplicates.",
    VENDOR_UNKNOWN="Refer the vendor to onboarding. Nothing is paid until they are verified and approved.",
    VENDOR_BLOCKED="No payment. Route to Compliance; the vendor must be cleared before any invoice is processed.",
    NOT_AN_INVOICE="Returned to the sender. Statements are reconciled separately; ask for individual invoices if anything is outstanding.",
    PO_OVERBILLED="Ask the buyer whether to raise the PO or ask the vendor for a credit/corrected invoice.",
    QTY_OVER_RECEIPT="Confirm what was physically received. Pay only the received quantity or hold until the rest arrives.",
    QTY_OVER_ORDER="Ask the buyer whether the extra quantity was authorised; otherwise request a corrected invoice.",
    PRICE_VARIANCE="Ask the buyer to confirm the new price or request a corrected invoice at PO price.",
    HIDDEN_TEXT="Do not process. Forward the file to Security. The hidden text tries to steer an automated reviewer.",
    INJECTION_TEXT="Do not process. Forward to Security; the document tries to steer an automated reviewer.",
    OVER_LIMIT="Route to the budget owner for sign-off, then release.",
    PO_AMBIGUOUS="Buyer to confirm which PO this invoice belongs to.",
    LOWCONF_TOTAL="Open the document, confirm the amount, then approve or correct.",
)


def money(x):
    return "n/a" if x is None else f"${x:,.2f}"


def rid(run_id):
    return f"RUN-{int(run_id):04d}"


def headline(decision, d, inv, vendor, po_info, supersedes=None):
    total = money(inv.get("total"))
    vname = (vendor or {}).get("legal_name") or inv.get("vendor_name") or "the vendor"
    p = d["primary"]
    if decision == "APPROVE":
        n = len(d["warnings"])
        po = (po_info.get("po") or {}).get("po_number")
        return f"Approved for payment: {total} to {vname}" + (f", matched to {po}" if po else "") + (f" ({n} note{'s' if n != 1 else ''})" if n else "")
    if decision == "REJECT":
        if p["code"] == "DUPLICATE_INVOICE":
            return f"Rejected: duplicate of an invoice already on file ({total})"
        if p["code"] == "VENDOR_UNKNOWN":
            return f"Rejected: {inv.get('vendor_name') or 'vendor'} is not an approved vendor"
        if p["code"] == "VENDOR_BLOCKED":
            return f"Rejected: {vname} is blocked (compliance hold)"
        if p["code"] == "NOT_AN_INVOICE":
            return "Returned: this document is not an invoice"
        return f"Rejected: {p['title'].lower()}"
    if decision == "ESCALATE":
        return f"Escalated to {ROUTE_LABEL.get(d['route'], d['route'])}: {p['title'][0].lower() + p['title'][1:]}"
    if decision == "HOLD":
        miss = [f["title"].replace("No ", "").replace(" found", "") for f in d["deciding"] if f["code"].startswith("MISSING_")]
        if miss:
            return "On hold, waiting on the vendor: missing " + ", ".join(miss[:3])
        return f"On hold, waiting on the vendor: {p['title'][0].lower() + p['title'][1:]}"
    return "Processed"


def explanation(decision, d, findings, inv, vendor, po_info, supersedes=None):
    """List of {tone, text} bullets in reading order."""
    out = []
    if decision == "APPROVE":
        passes = [f for f in findings if f["severity"] == "pass"]
        out.append(dict(tone="good", text=f"All {d['checks_passed']} required checks passed. Nothing in the invoice needs a person."))
        keyp = {"VENDOR_MATCH": 0, "BANK_MATCH": 1, "DUPLICATE_NONE": 2, "PO_FOUND": 3, "PO_PARTIAL_OK": 4, "PO_FULL_OK": 4,
                "TOTAL_MATCH": 5, "BALANCE_OK": 5, "TAX_OK": 6, "LIMIT_OK": 7}
        for f in sorted([p for p in passes if p["code"] in keyp], key=lambda p: keyp[p["code"]]):
            out.append(dict(tone="good", text=f["detail"] if f["code"] != "PO_FOUND" else f["title"]))
        for w in d["warnings"]:
            out.append(dict(tone="note", text=f"Note: {w['title']}. {w['detail']}"))
        if supersedes:
            out.append(dict(tone="note", text=f"Supersedes held run {rid(supersedes['run_id'])}; the earlier hold is closed, not treated as a duplicate."))
        return out
    for f in d["deciding"]:
        out.append(dict(tone="bad", text=f"{f['title']}. {f['detail']}"))
    others = d["other_issues"]
    if d["primary"] and d["primary"]["code"] == "DUPLICATE_INVOICE":
        # PO over-draw is just a consequence of paying the same invoice twice; say it once
        conseq = [f for f in others if f["code"] in ("QTY_OVER_ORDER", "PO_OVERBILLED", "PO_NOT_OPEN")]
        others = [f for f in others if f not in conseq]
        if conseq:
            out.append(dict(tone="warn", text="Also: paying it again would over-draw the PO it was billed against (it is already fully billed)."))
    for f in others[:4]:
        out.append(dict(tone="warn", text=f"Also: {f['title']}. {f['detail']}"))
    if supersedes:
        out.append(dict(tone="note", text=f"Matches held run {rid(supersedes['run_id'])}; recorded as a resubmission."))
    for w in d["warnings"][:3]:
        out.append(dict(tone="note", text=f"Note: {w['title']}. {w['detail']}"))
    ok = d["checks_passed"]
    if ok:
        out.append(dict(tone="good", text=f"{ok} other check{'s' if ok != 1 else ''} passed."))
    return out


def next_action(decision, d, vendor, po_info, inv):
    if decision == "APPROVE":
        return "Posted to the payment run. Due " + (inv.get("_due") or "per vendor terms") + ", paid to the bank account on file."
    p = d["primary"]
    base = NEXT_ACTION.get(p["code"])
    if p["code"] == "BANK_MISMATCH" and vendor:
        base = base.replace("the phone number in the vendor master", f"{vendor['phone']} (from the vendor master)")
    if base:
        return base
    if decision == "HOLD":
        return "A request for the missing information is drafted below. The payment clock is paused until a valid invoice arrives."
    return f"Routed to {ROUTE_LABEL.get(d['route'], d['route'])} with the evidence below."


# ----------------------------------------------------------------------------- artefacts
def erp_payload(run_id, inv, vendor, po_info, d, due, supersedes=None):
    po = po_info.get("po") or {}
    lines = []
    for r in po_info.get("lines", []):
        if r.get("kind") == "qty":
            lines.append(dict(po_line=r["po_line"], sku=r["sku"], qty=r["billed_qty"], unit_price=r["unit_price"],
                              amount=round(r["billed_qty"] * r["unit_price"], 2)))
        else:
            lines.append(dict(po_line=r["po_line"], sku=r["sku"], qty=1, unit_price=r["billed"], amount=r["billed"]))
    return {
        "type": "AP_INVOICE_POSTING", "run_id": rid(run_id), "vendor_id": vendor["vendor_id"], "vendor_name": vendor["legal_name"],
        "invoice_number": inv["number"], "invoice_date": inv["date"], "po_number": po.get("po_number"), "po_match_method": po_info.get("method"),
        "currency": inv["currency"], "amounts": {"net": inv["net"], "tax": inv["tax"], "total": inv["total"]},
        "payment": {"due_date": due, "terms": vendor["payment_terms"], "method": "ACH", "bank_account_last4": vendor["bank_account"][-4:],
                    "bank_source": "vendor_master"},
        "coding": {"cost_center": po.get("cost_center"), "buyer": po.get("buyer")},
        "lines": lines,
        "approval": {"mode": "auto", "checks_passed": d["checks_passed"], "risk_score": d["risk"], "confidence": d["confidence"],
                     "warnings": [w["title"] for w in d["warnings"]], "supersedes": rid(supersedes["run_id"]) if supersedes and supersedes.get("run_id") else None},
        "po_balance_after": po_info.get("remaining_after"),
    }


_VENDOR_ASK = dict(
    MISSING_INVOICE_NUMBER="the invoice number. On the copy we received this area is blotted out or unreadable, so we cannot register it",
    MISSING_INVOICE_DATE="the invoice date, which is missing or unreadable on our copy",
    MISSING_TOTAL="the invoice total, which is missing or unreadable on our copy",
    MISSING_VENDOR_NAME="your company name, which does not appear on the document",
    ARITH_TOTAL="a corrected invoice: the subtotal, tax and total on the copy we received do not add up",
    ARITH_LINES="a corrected invoice: the line items do not add up to the stated subtotal",
    ARITH_LINE_MATH="a corrected invoice: a line's quantity times price does not equal its amount",
    DATE_FUTURE="a corrected invoice: it is dated in the future",
    PO_MISSING="the purchase order number this invoice relates to",
    PO_AMBIGUOUS="the purchase order number this invoice relates to",
)


def vendor_email_hold(run_id, inv, vendor, d, po_info):
    asks = [(_VENDOR_ASK.get(f["code"]) or f["title"].lower()) for f in d["deciding"] if f["route"] == "vendor"]
    to = (vendor or {}).get("email") or inv.get("vendor_email") or ""
    num = inv.get("number")
    ref = f"invoice {num}" if num else f"your invoice dated {inv.get('date') or '(date unreadable)'} for {money(inv.get('total'))}"
    body = [f"Hello {(vendor or {}).get('legal_name', 'team')},", "",
            f"Thank you for {ref}. We could not process it yet because we need:", ""]
    body += [f"  {i}. {a[0].upper() + a[1:]}." for i, a in enumerate(asks, 1)]
    if po_info.get("method") == "inferred" and po_info.get("po"):
        body += ["", f"For reference, we believe this relates to {po_info['po']['po_number']} ({po_info['po']['description']}). "
                     "Please confirm the PO number on the corrected copy."]
    body += ["", "Please reply to this message with a clean copy. Payment terms run from the date we receive a valid invoice, "
                 "and we will process it the same day it arrives.", "", "Kind regards,", "Accounts Payable, Vantage Components Inc.",
            f"Ref: {rid(run_id)}"]
    return dict(kind="vendor_email", to=to, subject=f"Action needed: {num or 'invoice dated ' + str(inv.get('date'))} cannot be processed yet", body="\n".join(body))


def vendor_email_reject(run_id, inv, vendor, d, dup=None):
    p = d["primary"]
    to = (vendor or {}).get("email") or inv.get("vendor_email") or ""
    num = inv.get("number") or "(number unreadable)"
    if p["code"] == "DUPLICATE_INVOICE" and dup:
        paid = f", which we paid on {dup['paid_date']}" if dup.get("paid_date") else ""
        body = (f"Hello {(vendor or {}).get('legal_name', 'team')},\n\nWe received invoice {num} dated {inv.get('date')} for {money(inv.get('total'))}. "
                f"It is a duplicate of {dup['invoice_number']} ({dup['invoice_date']}, {money(dup['total'])}){paid}.\n\n"
                "No further action is needed and nothing additional will be paid. Please do not resend it; if you believe this is a "
                "different charge, reply with the PO number and what differs.\n\nKind regards,\nAccounts Payable, Vantage Components Inc.\n"
                f"Ref: {rid(run_id)}")
        return dict(kind="vendor_email", to=to, subject=f"Duplicate invoice {num}: already {'paid' if dup.get('paid_date') else 'processed'}", body=body)
    if p["code"] == "VENDOR_UNKNOWN":
        body = (f"Hello,\n\nWe received invoice {num} for {money(inv.get('total'))}, but your company is not set up as an approved supplier, "
                "so we cannot pay it yet. To be onboarded, please send: a completed W-9, a bank letter on company letterhead, "
                "and the name of your contact at Vantage Components.\n\nOnce onboarding is complete we will process the invoice.\n\n"
                f"Kind regards,\nAccounts Payable, Vantage Components Inc.\nRef: {rid(run_id)}")
        return dict(kind="vendor_email", to=to, subject="We cannot pay your invoice yet: supplier onboarding needed", body=body)
    if p["code"] == "NOT_AN_INVOICE":
        body = ("Hello,\n\nThe document we received is a statement of account, not an invoice, so nothing can be paid from it. "
                "If anything is outstanding, please send each invoice separately with its PO number.\n\nKind regards,\n"
                f"Accounts Payable, Vantage Components Inc.\nRef: {rid(run_id)}")
        return dict(kind="vendor_email", to=to, subject="Statement received: please send individual invoices", body=body)
    return None


def internal_brief(run_id, inv, vendor, d, po_info, decision):
    p = d["primary"]
    route = d["route"]
    po = po_info.get("po") or {}
    owner = {"buyer": po.get("buyer"), "receiving": po.get("buyer")}.get(route) or ROUTE_LABEL.get(route, route).title()
    vname = (vendor or {}).get("legal_name") or inv.get("vendor_name") or "unknown vendor"
    rl = ROUTE_LABEL.get(route, route)
    lines = [f"To: {owner}" + ("" if owner.lower() == rl.lower() or owner.lower() == rl.title().lower() else f" ({rl})"), f"Subject: [{decision}] {inv.get('number') or 'invoice'} from {vname}: {p['title']}", "",
             f"Invoice {inv.get('number') or '(number unreadable)'} dated {inv.get('date')} for {money(inv.get('total'))} "
             f"has been {'blocked' if decision == 'REJECT' else 'held'} by the AP process ({rid(run_id)}).", "", "Why:"]
    for f in d["deciding"]:
        lines.append(f"  - {f['title']}: {f['detail']}")
    for f in d["other_issues"][:3]:
        lines.append(f"  - Also: {f['title']}")
    lines += ["", "What to do:", f"  {next_action(decision, d, vendor, po_info, inv)}", "",
              "Payment is blocked until this is resolved. Approve or reject it from the AP dashboard review queue; your decision is recorded in the audit log."]
    return dict(kind="internal_brief", to=owner, subject=f"[{decision}] {inv.get('number') or 'Invoice'} from {vname}: {p['title']}", body="\n".join(lines))


# ----------------------------------------------------------------------------- AI polish guard
_NUM = re.compile(r"\d[\d,]*\.?\d*")


def _nums(s):
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(s or "")}


def guard_numbers(original, rewritten):
    """The rewritten text may not contain any number that the deterministic draft did not contain."""
    return _nums(rewritten) <= _nums(original)

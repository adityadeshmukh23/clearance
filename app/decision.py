"""Deterministic decision engine. The model never decides: findings do."""
from __future__ import annotations

OUTCOME_RANK = {"REJECT": 3, "ESCALATE": 2, "HOLD": 1}

# which finding is the headline when several fail (lower = more important)
PRIORITY = [
    "VENDOR_BLOCKED", "NOT_AN_INVOICE", "DUPLICATE_INVOICE", "VENDOR_UNKNOWN", "BANK_MISMATCH", "BANK_CHANGE_NOTICE", "EMAIL_LOOKALIKE",
    "HIDDEN_TEXT", "INJECTION_TEXT", "TAXID_MISMATCH", "PO_VENDOR_MISMATCH", "INVOICE_NUMBER_REUSED", "PO_OVERBILLED",
    "QTY_OVER_RECEIPT", "QTY_OVER_ORDER", "PRICE_VARIANCE", "LINE_NOT_ON_PO", "TOTAL_VARIANCE", "TAX_MISMATCH", "PO_NOT_OPEN",
    "OVER_LIMIT", "CURRENCY_MISMATCH", "VENDOR_UNCERTAIN", "VENDOR_NOT_APPROVED", "PO_AMBIGUOUS", "PO_NONE_AVAILABLE",
    "XCHECK_MISMATCH", "DATE_STALE", "PO_AFTER_INVOICE",
    "MISSING_INVOICE_NUMBER", "MISSING_INVOICE_DATE", "MISSING_TOTAL", "MISSING_VENDOR_NAME", "ARITH_TOTAL", "ARITH_LINES",
    "ARITH_LINE_MATH", "DATE_FUTURE", "PO_MISSING",
]
WEIGHT = {"fail": 25, "warn": 5, "info": 0, "pass": 0}
BOOST = {"BANK_MISMATCH": 25, "EMAIL_LOOKALIKE": 15, "HIDDEN_TEXT": 30, "INJECTION_TEXT": 25, "DUPLICATE_INVOICE": 20,
         "VENDOR_BLOCKED": 35, "VENDOR_UNKNOWN": 20}


def _prio(code):
    return PRIORITY.index(code) if code in PRIORITY else 999


def decide(findings, trust):
    """findings: list of F() dicts. trust: dict(extraction=0..1, match=0..1)."""
    fails = [f for f in findings if f["severity"] == "fail" and f["outcome"]]
    decision = "APPROVE"
    if fails:
        decision = max((f["outcome"] for f in fails), key=lambda o: OUTCOME_RANK[o])
    deciding = sorted([f for f in fails if f["outcome"] == decision], key=lambda f: _prio(f["code"])) if fails else []
    others = sorted([f for f in fails if f["outcome"] != decision], key=lambda f: (-OUTCOME_RANK[f["outcome"]], _prio(f["code"])))
    risk = 0
    for f in findings:
        risk += WEIGHT.get(f["severity"], 0) + (BOOST.get(f["code"], 0) if f["severity"] == "fail" else 0)
    risk = min(100, risk)
    conf = min(trust.get("extraction", 1.0), trust.get("match", 1.0))
    passes = sum(1 for f in findings if f["severity"] == "pass")
    return dict(decision=decision, primary=deciding[0] if deciding else None, deciding=deciding, other_issues=others,
                risk=risk, confidence=round(conf, 3), checks_passed=passes, checks_total=len([f for f in findings if f["severity"] != "info"]),
                route=(deciding[0]["route"] if deciding else "payment_run"),
                warnings=[f for f in findings if f["severity"] == "warn"])

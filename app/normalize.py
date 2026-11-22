"""Small, well-tested normalisers: money, dates, ids, company names."""
from __future__ import annotations

import datetime as dt
import re

MONEY_RE = re.compile(r"(?<![\w.])[-(]?\s*[$€£₹]?\s*\d[\d,. ]*\d(?:[.,]\d{1,2})?\)?|(?<![\w.])[-(]?\s*[$€£₹]\s*\d\)?")
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_MONTHS_FULL = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], 1)}


def parse_money(s):
    """'$1,234.56' -> 1234.56 ; '1.234,56' -> 1234.56 ; '(12.00)' -> -12.0 ; None if no number."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return round(float(s), 2)
    t = str(s).strip()
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-") or t.upper().endswith("CR")
    t = re.sub(r"[^\d.,]", "", t)
    if not t or not re.search(r"\d", t):
        return None
    last_dot, last_com = t.rfind("."), t.rfind(",")
    if last_dot != -1 and last_com != -1:
        if last_com > last_dot:       # 1.234,56
            t = t.replace(".", "").replace(",", ".")
        else:                         # 1,234.56
            t = t.replace(",", "")
    elif last_com != -1:
        # "1,234" (thousands) vs "12,34" (decimal comma)
        frac = t[last_com + 1:]
        t = t.replace(",", ".") if len(frac) in (1, 2) and t.count(",") == 1 else t.replace(",", "")
    else:
        if t.count(".") > 1:          # OCR: 3.200.00 -> treat last dot as decimal
            head, _, tail = t.rpartition(".")
            t = head.replace(".", "") + "." + tail
    try:
        v = float(t)
    except ValueError:
        return None
    return round(-v if neg else v, 2)


def money_tokens(text):
    """All money-looking tokens in text, as (raw, value) in order."""
    out = []
    for m in re.finditer(r"[-(]?\s*[$€£₹]?\s*\d{1,3}(?:[, ]\d{3})*(?:\.\d{2})\)?|[-(]?\s*[$€£₹]\s*\d+(?:\.\d{1,2})?\)?|\b\d+\.\d{2}\b", text):
        if text[m.end():m.end() + 1] == "%":
            continue
        raw = m.group(0).strip().lstrip("(")
        v = parse_money(raw)
        if v is not None:
            out.append((raw, v))
    return out


def detect_currency(text):
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in text:
            return code
    m = re.search(r"\b(USD|EUR|GBP|INR|CAD|AUD)\b", text.upper())
    return m.group(1) if m else None


def parse_date(s, prefer_us=True):
    """Return (iso or None, ambiguous: bool)."""
    if not s:
        return None, False
    t = str(s).strip().replace(",", " ").replace(".", " ")
    t = re.sub(r"\s+", " ", t)
    # ISO
    m = re.search(r"\b(20\d{2})[-/ ](\d{1,2})[-/ ](\d{1,2})\b", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _mk(y, mo, d), False
    # 04-Nov-2026 | 4 November 2026 | Nov 4 2026
    m = re.search(r"\b(\d{1,2})[ -]([A-Za-z]{3,9})[ -](\d{4})\b", t)
    if m:
        mo = _month(m.group(2))
        if mo:
            return _mk(int(m.group(3)), mo, int(m.group(1))), False
    m = re.search(r"\b([A-Za-z]{3,9}) (\d{1,2}) (\d{4})\b", t)
    if m:
        mo = _month(m.group(1))
        if mo:
            return _mk(int(m.group(3)), mo, int(m.group(2))), False
    # 11/04/2026 or 04/11/2026
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", t)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if a > 12 and b <= 12:
            return _mk(y, b, a), False
        if b > 12 and a <= 12:
            return _mk(y, a, b), False
        amb = a != b
        return (_mk(y, a, b) if prefer_us else _mk(y, b, a)), amb
    return None, False


def _month(tok):
    k = tok.lower()
    return _MONTHS_FULL.get(k) or _MONTHS.get(k[:3])


def _mk(y, m, d):
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return None


# ------------------------------------------------------------------ ids
_PREFIXES = ("INVOICE", "INV", "BILLNO", "BILL", "NO")


def norm_invoice_number(s):
    """Canonical key for comparing invoice numbers across formats / OCR noise."""
    if not s:
        return ""
    t = re.sub(r"[^A-Z0-9]", "", str(s).upper())
    for p in _PREFIXES:
        if t.startswith(p) and len(t) > len(p) + 2:
            t = t[len(p):]
            break
    return t


def invoice_number_core(s):
    """Like norm_invoice_number but also drops a trailing resubmission marker (A, R, REV, COPY, DUP)."""
    t = norm_invoice_number(s)
    t = re.sub(r"(COPY|DUP|REV|RESUB|R|A)$", "", t) if len(t) > 5 else t
    if t.isdigit():
        t = t.lstrip("0") or "0"
    return t


def norm_po(s):
    """'po 4501' / 'PO-4501' / 'P0-4501' -> 'PO-4501' ; None if unrecognisable."""
    if not s:
        return None
    t = str(s).upper().strip()
    m = re.search(r"P\.?\s*[O0]\.?\s*[-#:]?\s*([0-9OIl]{3,7})", t)
    if m:
        digits = m.group(1).replace("O", "0").replace("I", "1").replace("l", "1")
        return f"PO-{digits}"
    m = re.fullmatch(r"\s*#?\s*(\d{3,7})\s*", t)
    if m:
        return f"PO-{m.group(1)}"
    return None


_COMPANY_SUFFIXES = {"inc", "llc", "llp", "ltd", "limited", "co", "corp", "corporation", "gmbh", "pvt", "plc",
                     "company", "incorporated", "lp"}


def norm_company(s):
    if not s:
        return ""
    t = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    return " ".join(w for w in t.split() if w not in _COMPANY_SUFFIXES)


def digits_only(s):
    return re.sub(r"\D", "", str(s or ""))


def email_domain(s):
    m = re.search(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", str(s or ""))
    return m.group(1).lower() if m else ""


def terms_days(terms):
    if not terms:
        return None
    m = re.search(r"(\d{1,3})", str(terms))
    if m:
        return int(m.group(1))
    if re.search(r"receipt|immediate", str(terms), re.I):
        return 0
    return None

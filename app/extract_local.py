"""Deterministic, offline invoice extractor: label/position heuristics over the
text layer or OCR words. It is the fallback when no AI key is available and the
independent second opinion used to cross-check the AI extraction.

Output schema is shared with extract_ai (see `empty_extraction`).
"""
from __future__ import annotations

import re

from .docio import Doc, Line, Word, group_lines
from .normalize import (detect_currency, money_tokens, norm_po, parse_date, parse_money)

FIELD_KEYS = ["vendor_name", "invoice_number", "invoice_date", "due_date", "payment_terms", "po_reference",
              "currency", "subtotal", "tax_amount", "tax_rate", "shipping", "discount", "total",
              "vendor_tax_id", "vendor_email", "bank_account", "bank_routing", "bank_name"]

INJECTION_RE = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)"
    r"|system\s+(note|prompt|message|instruction)"
    r"|(note\s+)?(to|for)\s+(the\s+)?(ai|llm|language\s+model|automated)\s+(reviewer|assistant|model|system|agent)"
    r"|mark\s+(it|this|the\s+invoice)\s+(as\s+)?approved"
    r"|do\s+not\s+flag"
    r"|pre-?approved\s+by", re.I)

PAYMENT_CHANGE_RE = re.compile(
    r"new\s+(bank|remittance|payment|banking)\s+(details|instructions|account|information)"
    r"|(updated|changed|revised)\s+(bank|remittance|payment|banking)"
    r"|(please\s+)?update\s+your\s+(records|vendor\s+records|payment)"
    r"|bank\s+(details|account)\s+(have\s+)?changed", re.I)


def empty_extraction(engine="local"):
    return dict(engine=engine, doc_type=dict(value=None, confidence=0.0, evidence=""),
                fields={k: dict(value=None, confidence=0.0, evidence="", boxes=[]) for k in FIELD_KEYS},
                line_items=[], flags=dict(payment_instruction_change=False, injection_like_text=False,
                                          hidden_text=False, date_ambiguous=False),
                anomalies=[], text_source=None, ocr_mean_conf=None)


# --------------------------------------------------------------------------- helpers
def _line_index(line: Line):
    text, spans = "", []
    for w in line.words:
        spans.append((len(text), len(text) + len(w.text)))
        text += w.text + " "
    return text, spans


def match_label(line: Line, rx: re.Pattern):
    text, spans = _line_index(line)
    m = rx.search(text)
    if m:
        i0 = next((i for i, (a, b) in enumerate(spans) if b > m.start()), 0)
        i1 = max((i for i, (a, b) in enumerate(spans) if a < m.end()), default=i0)
        return i0, i1
    if "^" in rx.pattern or "$" in rx.pattern:
        # anchored patterns describe a whole cell (a label in its own column), so try each cell
        idx = 0
        for cell in line.cells():
            ctext = " ".join(w.text for w in cell)
            if rx.search(ctext):
                return idx, idx + len(cell) - 1
            idx += len(cell)
    return None


def _strip_sep(words):
    ws = list(words)
    while ws and re.fullmatch(r"[:#.\-–|]+", ws[0].text):
        ws.pop(0)
    # a word that starts with ':' like ':INV-1'
    if ws and ws[0].text.startswith(":") and len(ws[0].text) > 1:
        w = ws[0]
        ws[0] = Word(w.text[1:], w.x0, w.y0, w.x1, w.y1, w.page, w.conf, w.h)
    return ws


def _wtext(ws):
    return " ".join(w.text for w in ws)


def _boxes(ws):
    if not ws:
        return []
    pg = ws[0].page
    return [[pg, round(min(w.x0 for w in ws), 4), round(min(w.y0 for w in ws), 4),
             round(max(w.x1 for w in ws), 4), round(max(w.y1 for w in ws), 4)]]


def _conf(base, ws, doc: Doc):
    if doc.text_source == "ocr" and ws:
        mc = sum(w.conf for w in ws) / len(ws)
        return round(base * (0.55 + 0.45 * min(1.0, mc)), 3)
    return base


# value validators: return (clean_value, words_used) or None
def v_id(ws):
    for i, w in enumerate(ws):
        t = w.text.strip(" ,;")
        if len(t) >= 3 and re.search(r"\d", t) and not re.search(r"[$€£]|\.\d{2}$", t) and not parse_date(t)[0] \
                and not re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", t) and not t.startswith("+"):
            return t, [w]
    return None


def v_date(ws):
    # try 1..4 word windows
    for n in (4, 3, 2, 1):
        for i in range(0, max(1, len(ws) - n + 1)):
            seg = ws[i:i + n]
            iso, amb = parse_date(_wtext(seg))
            if iso:
                return (iso, amb), seg
    return None


def v_money(ws):
    best = None
    for i, w in enumerate(ws):
        for raw, v in money_tokens(w.text):
            best = (v, [w])
    return best


def v_po(ws):
    txt = _wtext(ws)
    m = re.search(r"\bP\.?\s*[O0]\.?\s*[-#:]?\s*([0-9OIl]{3,7})\b", txt, re.I)
    if m:
        return norm_po(m.group(0)), ws
    if ws:
        m = re.fullmatch(r"#?(\d{3,7})", ws[0].text.strip())
        if m:
            return f"PO-{m.group(1)}", ws[:1]
    return None


def v_terms(ws):
    txt = _wtext(ws)
    m = re.search(r"net\s*\d{1,3}|due\s+on\s+receipt|\d{1,3}\s*days", txt, re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(0)).title().replace("Net", "Net"), ws
    return None


VALIDATORS = dict(id=v_id, date=v_date, money=v_money, po=v_po, terms=v_terms,
                  text=lambda ws: (_wtext(ws), ws) if ws else None)


def find_labeled(lines, patterns, kind, doc, *, below=True, pick="first", exclude=None):
    """Find 'Label: value' (same line, to the right) or label-above-value (next line, aligned)."""
    rxs = [re.compile(p, re.I) for p in patterns]
    cands = []
    for rank, rx in enumerate(rxs):
        for li, line in enumerate(lines):
            mm = match_label(line, rx)
            if not mm:
                continue
            i0, i1 = mm
            if exclude:
                # judge the label itself plus the word before it ("Due" + "Date"), not the whole line
                ctx = " ".join(w.text for w in line.words[max(0, i0 - 1):i1 + 1])
                if re.search(exclude, ctx, re.I):
                    continue
            label_ws = line.words[i0:i1 + 1]
            right = _strip_sep(line.words[i1 + 1:])
            hit = VALIDATORS[kind](right) if right else None
            where, base = "same-line", 0.93
            if not hit and below:
                for nxt in lines[li + 1:li + 3]:
                    if nxt.page != line.page or nxt.y - line.y > max(0.06, line.h * 4.5):
                        continue
                    lx0, lx1 = label_ws[0].x0, label_ws[-1].x1
                    aligned = [w for w in nxt.words if w.x0 >= lx0 - 0.045 and w.x0 <= lx1 + 0.06]
                    # keep words from the cell that starts nearest the label
                    if aligned:
                        start = aligned[0]
                        cell = []
                        for c in nxt.cells():
                            if c and c[0].x0 >= start.x0 - 1e-6:
                                cell = c
                                break
                        hit = VALIDATORS[kind](cell or aligned)
                        if hit:
                            where, base = "below-label", 0.86
                            break
            if hit:
                val, ws = hit
                cands.append(dict(value=val, words=ws, label=label_ws, rank=rank, line=li, y=line.y, base=base,
                                  where=where))
        if cands and pick == "first":
            break
    if not cands:
        return None
    c = sorted(cands, key=lambda c: (c["rank"], c["line"] if pick == "first" else -c["line"]))[0]
    return c


def _field(ex, key, value, conf, ws=None, evidence=None, label_ws=None):
    boxes = _boxes(ws) if ws else []
    if label_ws and boxes:
        pass
    ex["fields"][key] = dict(value=value, confidence=round(float(conf), 3),
                             evidence=evidence if evidence is not None else (_wtext(ws) if ws else ""),
                             boxes=boxes)


# --------------------------------------------------------------------------- main
TOTAL_PATTERNS = [
    r"\btotal\s*(?:amount\s*)?due\b", r"\bamount\s*due\b", r"\bbalance\s*due\b", r"\btotal\s*payable\b",
    r"\bamount\s*payable\b", r"\bgrand\s*total\b", r"\binvoice\s*total\b", r"\btotal\s*amount\b",
    r"(?<!sub)(?<!sub-)(?<!sub )\btotal\b",
]
TOTAL_ANY = re.compile("|".join(TOTAL_PATTERNS), re.I)


def _last_money_on(line: Line, after_idx=0):
    best = None
    for w in line.words[after_idx:]:
        for raw, v in money_tokens(w.text):
            best = (v, w, raw)
    return best


def extract_local(doc: Doc) -> dict:
    ex = empty_extraction("local")
    ex["text_source"] = doc.text_source
    ex["ocr_mean_conf"] = doc.ocr_conf
    lines = doc.lines()
    full = "\n".join(l.text for l in lines)
    full_lower = full.lower()
    page0 = [l for l in lines if l.page == 0]

    # ---- flags that look at hidden + visible text
    all_text = full + "\n" + " ".join(w.text for w in doc.hidden_words)
    ex["flags"]["hidden_text"] = len(doc.hidden_words) > 0
    ex["flags"]["injection_like_text"] = bool(INJECTION_RE.search(all_text))
    ex["flags"]["payment_instruction_change"] = bool(PAYMENT_CHANGE_RE.search(full))
    if ex["flags"]["hidden_text"]:
        ex["anomalies"].append("Hidden text found in the file: " + " ".join(w.text for w in doc.hidden_words)[:140])
    if ex["flags"]["payment_instruction_change"]:
        m = PAYMENT_CHANGE_RE.search(full)
        ex["anomalies"].append(f"Document announces changed payment details: \"{m.group(0)}\"")

    # ---- document type
    total_hit = None
    if re.search(r"statement\s+of\s+account|account\s+statement", full_lower):
        ex["doc_type"] = dict(value="statement", confidence=0.9, evidence="statement of account")
    elif re.search(r"credit\s+(memo|note)", full_lower):
        ex["doc_type"] = dict(value="credit_note", confidence=0.9, evidence="credit memo/note")

    # ---- invoice number
    inv_pats = [r"\binvoice\s*(?:no|number|num|#)\b\.?", r"\binv\.?\s*(?:no|number|#)\b\.?", r"\bbill\s*(?:no|number|#)\b\.?",
                r"\bdocument\s*(?:no|number)\b\.?", r"\binvoice\s*#", r"\bstatement\s*(?:no|number)\b\.?"]
    c = find_labeled(lines, inv_pats, "id", doc)
    if c:
        _field(ex, "invoice_number", c["value"], _conf(c["base"], c["words"], doc), c["words"])

    # ---- dates
    c = find_labeled(lines, [r"\binvoice\s*date\b", r"\bdate\s*of\s*issue\b", r"\bissue\s*date\b", r"\bbill\s*date\b",
                             r"\binvoiced\b", r"^date$", r"\bdate\b(?!\s*due)"], "date", doc,
                     exclude=r"due\s*date|statement\s*date")
    if c:
        (iso, amb), ws = c["value"], c["words"]
        _field(ex, "invoice_date", iso, _conf(c["base"], ws, doc) * (0.92 if amb else 1.0), ws)
        ex["flags"]["date_ambiguous"] = bool(amb)
    c2 = find_labeled(lines, [r"\bdue\s*date\b", r"\bpayment\s*due\b", r"\bdue\b"], "date", doc)
    if c2:
        (iso, amb), ws = c2["value"], c2["words"]
        _field(ex, "due_date", iso, _conf(c2["base"], ws, doc), ws)

    if ex["fields"]["invoice_date"]["value"] is None:
        # positional fallback: dates printed in the header zone, earliest = invoice date
        dates = []
        for l in page0:
            if l.y > 0.42:
                continue
            for cell in l.cells():
                hit = v_date(cell)
                if hit:
                    dates.append((hit[0][0], hit[1]))
        if dates:
            dates.sort(key=lambda d: d[0])
            _field(ex, "invoice_date", dates[0][0], _conf(0.6, dates[0][1], doc), dates[0][1])
            ex["anomalies"].append("Invoice date had no readable label; taken as the earliest date in the header")
            if len(dates) > 1 and ex["fields"]["due_date"]["value"] is None:
                _field(ex, "due_date", dates[-1][0], _conf(0.55, dates[-1][1], doc), dates[-1][1])

    c = find_labeled(lines, [r"\b(?:payment\s*)?terms\b"], "terms", doc, below=False)
    if c:
        _field(ex, "payment_terms", c["value"], _conf(0.9, c["words"], doc), c["words"])

    # ---- PO reference
    c = find_labeled(lines, [r"\bp\.?\s*o\.?\s*(?:number|no\.?|#|ref\.?|reference)?(?=[\s:#]|$)",
                             r"\bpurchase\s*order(?:\s*(?:no|number|#))?\b", r"\byour\s*ref(?:erence)?\b",
                             r"\bcustomer\s*po\b", r"\border\s*(?:no|number|ref)\b"], "po", doc)
    if c and c["value"]:
        _field(ex, "po_reference", c["value"], _conf(c["base"], c["words"], doc), c["words"])
    else:
        for l in lines:
            m = re.search(r"\bP\.?\s*[O0]\.?\s*[-#]\s*\d{3,6}\b|\bPO\s?\d{3,6}\b", l.text)
            if m:
                po = norm_po(m.group(0))
                ws = [w for w in l.words if re.search(r"PO|P\.O|\d{3,6}", w.text)]
                ws = [w for w in l.words if w.text in m.group(0).split() or m.group(0) in w.text] or l.words
                _field(ex, "po_reference", po, _conf(0.7, ws, doc), ws, evidence=l.text[:90])
                break

    # ---- totals
    best = None
    for rank, rx in enumerate([re.compile(p, re.I) for p in TOTAL_PATTERNS]):
        for li, l in enumerate(lines):
            if re.search(r"subtotal|sub-total|sub total", l.text, re.I):
                continue
            mm = match_label(l, rx)
            if mm:
                got = _last_money_on(l, mm[1] + 1)
                if got and got[0] is not None:
                    cand = (rank, -l.y, got, l)
                    if best is None or cand[:2] < best[:2]:
                        best = cand
        if best:
            break
    total_line = None
    if best:
        v, w, raw = best[2]
        total_line = best[3]
        _field(ex, "total", v, _conf(0.93, [w], doc), [w], evidence=raw)
        cur = detect_currency(total_line.text) or detect_currency(full)
        if cur:
            ex["fields"]["currency"] = dict(value=cur, confidence=0.9, evidence=cur, boxes=[])

    in_table = table_line_ids(lines)
    for key, pats in (("subtotal", [r"\bsub\s*-?\s*total\b", r"\bnet\s*(?:amount|total)\b", r"\btotal\s*before\s*tax\b"]),
                      ("shipping", [r"\b(?:shipping|freight|delivery)(?:\s*(?:&|and)\s*handling)?\b"]),
                      ("discount", [r"\bdiscount\b"])):
        for p in pats:
            hit = None
            for l in lines:
                if id(l) in in_table:
                    continue
                mm = match_label(l, re.compile(p, re.I))
                if mm and mm[0] <= 2:
                    got = _last_money_on(l, mm[1] + 1)
                    if got:
                        hit = got
            if hit:
                v, w, raw = hit
                _field(ex, key, abs(v), _conf(0.9, [w], doc), [w], evidence=raw)
                break

    # tax: last line with tax wording + money, not the total line, not 'tax id'
    tax_hit, rate = None, None
    for l in lines:
        t = l.text
        if id(l) in in_table:
            continue
        if re.search(r"tax\s*id|\bein\b|tax\s*invoice|taxable", t, re.I):
            continue
        if total_line is not None and l is total_line and not re.search(r"of which", t, re.I):
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
            if m and rate is None:
                rate = float(m.group(1))
            continue
        mm = match_label(l, re.compile(r"\b(?:sales\s*)?(?:tax|vat|gst)\b", re.I))
        if mm:
            got = _last_money_on(l, mm[1] + 1)
            if got:
                tax_hit = (got, l)
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
                if m:
                    rate = float(m.group(1))
                elif re.search(r"exempt", t, re.I):
                    rate = 0.0
    if tax_hit:
        (v, w, raw), l = tax_hit
        _field(ex, "tax_amount", v, _conf(0.9, [w], doc), [w], evidence=raw)
    if rate is not None:
        ex["fields"]["tax_rate"] = dict(value=rate, confidence=0.85, evidence=f"{rate:g}%", boxes=[])

    # ---- vendor header
    head = [l for l in page0 if l.y < 0.22][:10]
    skip = re.compile(r"^(tax\s+|sales\s+)?invoice$|^statement|^bill\s*to|^page\s*\d", re.I)
    best_cell, best_h = None, 0
    for l in head:
        for cell in l.cells():
            txt = _wtext(cell).strip()
            if len(txt) < 4 or skip.search(txt) or re.search(r"@|\d{3}[ -]\d{3,4}|www\.", txt):
                continue
            h = sum((w.h or (w.y1 - w.y0)) for w in cell) / len(cell)
            if h > best_h * 1.04 or (best_cell is None):
                best_cell, best_h = cell, h
    if best_cell:
        # attach neighbouring line fragment for wrapped names ("KESTREL" / "FACILITIES SERVICES INC.")
        txt = _wtext(best_cell)
        _field(ex, "vendor_name", txt, _conf(0.85, best_cell, doc), best_cell)

    m = re.search(r"\b(\d{2}-\d{7})\b", full)
    if m:
        ws = [w for l in lines for w in l.words if m.group(1) in w.text][:1]
        _field(ex, "vendor_tax_id", m.group(1), _conf(0.9, ws, doc), ws, evidence=m.group(1))
    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", full)
    if m:
        ws = [w for l in lines for w in l.words if m.group(0) in w.text][:1]
        _field(ex, "vendor_email", m.group(0), _conf(0.9, ws, doc), ws, evidence=m.group(0))

    # ---- bank details
    m = re.search(r"(?:account\s*(?:no\.?|number|#)?|acct\.?|a/c)\s*[:#]?\s*(\d[\d \-]{6,}\d)", full, re.I)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        ws = [w for l in lines for w in l.words if digits[:6] in re.sub(r"\D", "", w.text)][:1]
        _field(ex, "bank_account", digits, _conf(0.9, ws, doc), ws, evidence=m.group(1))
    m = re.search(r"(?:routing|aba)(?:\s*\(aba\))?\s*(?:no\.?|number|#)?[:\s]*(\d{9})", full, re.I)
    if m:
        ws = [w for l in lines for w in l.words if m.group(1) in w.text][:1]
        _field(ex, "bank_routing", m.group(1), _conf(0.9, ws, doc), ws, evidence=m.group(1))
    m = re.search(r"\bbank\s*:\s*([A-Z][A-Za-z&' ]{3,40}?)(?=\s{2,}|\n|$|,|\s+(?:Account|Acct))", full, re.I) \
        or re.search(r"(?:ACH\s+to)\s+([A-Z][A-Za-z&' ]{3,40}?)(?:,|\s+-\s)", full) \
        or re.search(r"([A-Z][A-Za-z&']+(?:[ \t][A-Z][A-Za-z&']+){0,3}[ \t](?:Bank|Trust|Capital|Savings))", full)
    if m:
        _field(ex, "bank_name", m.group(1).strip(), 0.7, None, evidence=m.group(1).strip())

    # ---- line items
    ex["line_items"] = parse_line_items(lines, doc)

    # ---- doc type default
    if ex["doc_type"]["value"] is None:
        has_inv_word = bool(re.search(r"\binvoice\b", full_lower))
        if ex["fields"]["total"]["value"] is not None and (has_inv_word or ex["fields"]["invoice_number"]["value"]):
            ex["doc_type"] = dict(value="invoice", confidence=0.9 if has_inv_word else 0.7, evidence="invoice + total found")
        elif ex["fields"]["total"]["value"] is not None:
            ex["doc_type"] = dict(value="invoice", confidence=0.55, evidence="total found but no invoice wording")
        else:
            ex["doc_type"] = dict(value="other", confidence=0.6, evidence="no invoice markers")
    return ex


# --------------------------------------------------------------------------- line items
SKU_RE = re.compile(r"^[A-Z]{2,6}(?:-[A-Za-z0-9]{1,8}){1,3}$")
_END_RE = re.compile(r"^\s*(sub\s*-?\s*total|total|tax|sales\s*tax|vat|gst|amount\s*due|balance\s*due|remit|bank|"
                     r"pay\s*by|payment\s*details|thank|notes|services\s*performed|shipping|freight|discount)\b", re.I)


def _is_decimal_money(t):
    return bool(re.fullmatch(r"[$€£₹]?\(?\d{1,3}(?:[,.]\d{3})*[.,]\d{2}\)?|[$€£₹]?\d+[.,]\d{2}", t.strip()))


def _is_qty(t):
    return bool(re.fullmatch(r"\d{1,5}(?:\.\d{1,3})?", t.strip()))


def table_line_ids(lines):
    """ids of lines that belong to the line-item table (header + rows), so totals scanning can skip them."""
    ids = set()
    hdr = None
    for i, l in enumerate(lines):
        t = l.text.lower()
        score = sum(k in t for k in ("description", "qty", "quantity", "price", "rate", "amount", "unit", "item"))
        if score >= 3 or ("description" in t and score >= 2):
            hdr = i
            break
    if hdr is None:
        return ids
    ids.add(id(lines[hdr]))
    for l in lines[hdr + 1:]:
        if _END_RE.search(l.text.strip()):
            break
        ids.add(id(l))
    return ids


def parse_line_items(lines, doc):
    hdr = None
    for i, l in enumerate(lines):
        t = l.text.lower()
        score = sum(k in t for k in ("description", "qty", "quantity", "price", "rate", "amount", "unit", "item"))
        if score >= 3 or ("description" in t and score >= 2):
            hdr = i
            break
    if hdr is None:
        return []
    rows = []
    for l in lines[hdr + 1:]:
        txt = l.text.strip()
        if not txt:
            continue
        if _END_RE.search(txt):
            break
        ws = l.words
        # trailing numeric tokens
        j = len(ws)
        nums = []
        while j > 0 and len(nums) < 3:
            tok = ws[j - 1].text.strip(" ,")
            if (not nums and _is_decimal_money(tok)) or (nums and (_is_decimal_money(tok) or (_is_qty(tok) and len(nums) >= 1))):
                nums.insert(0, ws[j - 1])
                j -= 1
            else:
                break
        if nums and _is_decimal_money(nums[-1].text.strip(" ,")):
            # a leading qty must be followed by at least one money value
            desc_ws = ws[:j]
            vals = [parse_money(n.text) if _is_decimal_money(n.text.strip(" ,")) else float(n.text) for n in nums]
            if len(nums) == 3:
                qty, unit, amt = vals
                if abs(qty * unit - amt) > max(0.05, 0.01 * amt) and abs(unit - amt) <= max(0.05, 0.01 * amt):
                    # first "number" is really part of the description (e.g. a year) - the real qty was lost
                    desc_ws = desc_ws + [nums[0]]
                    qty = round(amt / unit, 3) if unit else 1.0
            elif len(nums) == 2:
                if _is_qty(nums[0].text) and not _is_decimal_money(nums[0].text):
                    qty, amt = vals[0], vals[1]
                    unit = round(amt / qty, 4) if qty else amt
                else:
                    unit, amt = vals
                    qty = round(amt / unit, 3) if unit else 1.0
            else:
                amt = vals[0]
                qty, unit = 1.0, amt
            sku = ""
            if desc_ws and SKU_RE.match(desc_ws[0].text.strip(",")):
                sku = desc_ws[0].text.strip(",")
                desc_ws = desc_ws[1:]
            desc = _wtext(desc_ws).strip()
            conf = 0.88
            if qty and unit and abs(qty * unit - amt) > max(0.05, 0.01 * amt):
                conf = 0.6
            rows.append(dict(sku=sku, description=desc, qty=qty, unit_price=unit, amount=amt,
                             confidence=_conf(conf, l.words, doc), boxes=_boxes(l.words), evidence=txt[:100]))
        elif rows and not nums:
            # wrapped description continuation
            rows[-1]["description"] = (rows[-1]["description"] + " " + txt).strip()
            rows[-1]["boxes"] = _union(rows[-1]["boxes"], _boxes(l.words))
    return rows


def _union(a, b):
    if not a:
        return b
    if not b:
        return a
    x = a[0]
    y = b[0]
    return [[x[0], min(x[1], y[1]), min(x[2], y[2]), max(x[3], y[3]), max(x[4], y[4])]]

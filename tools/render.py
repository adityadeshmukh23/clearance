"""Render synthetic invoice PDFs (4 vendor layouts + statement) and simulate scans."""
from __future__ import annotations

import datetime as dt
import io
import os
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from dataset import CUSTOMER, VENDOR_BY_ID

W, H = letter


# ----------------------------------------------------------------------------- helpers
def money(x, cur="$"):
    return f"{cur}{x:,.2f}"


def fmt_date(iso, layout):
    d = dt.date.fromisoformat(iso)
    return {
        "classic": d.strftime("%m/%d/%Y"),
        "modern": d.strftime("%d-%b-%Y"),
        "inclusive": f"{d.day} {d.strftime('%B %Y')}",
        "stacked": d.strftime("%d %b %Y"),
        "statement": d.strftime("%B %d, %Y"),
    }[layout]


def terms_days(terms):
    digits = "".join(ch for ch in terms if ch.isdigit())
    return int(digits) if digits else 30


def totals(sp):
    lines = sp["lines"]
    gross_or_net = round(sum(q * p for _, _, q, p in lines), 2)
    rate = sp.get("tax_rate", 0.0)
    ship, disc = sp.get("shipping", 0.0), sp.get("discount", 0.0)
    if sp.get("tax_mode") == "inclusive":
        total = gross_or_net
        tax = round(total - total / (1 + rate / 100), 2)
        net = round(total - tax, 2)
        return dict(subtotal=net, tax=tax, shipping=ship, discount=disc, total=total, rate=rate)
    net = gross_or_net
    tax = round((net - disc) * rate / 100, 2)
    total = round(net - disc + ship + tax, 2)
    return dict(subtotal=net, tax=tax, shipping=ship, discount=disc, total=total, rate=rate)


def vendor_of(sp):
    if sp.get("vendor"):
        v = dict(VENDOR_BY_ID[sp["vendor"]])
    else:
        v = dict(sp["vendor_override"])
        v.setdefault("payment_terms", "Net 30")
    if sp.get("email_override"):
        v["email"] = sp["email_override"]
    return v


def bank_of(sp, v):
    if sp.get("bank_override"):
        b = sp["bank_override"]
        return dict(bank=b["bank_name"], account=b["account"], routing=b["routing"])
    if sp.get("bank") == "changed":
        return dict(bank="Cedarpoint Capital", account="7710553920", routing="026009593")
    return dict(bank=v.get("bank_name", ""), account=v.get("bank_account", ""), routing=v.get("bank_routing", ""))


def wrap(c, text, font, size, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if c.stringWidth(t, font, size) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def smudge(c, x, y, w, h, seed=5):
    """Ink blot / coffee smear over a field."""
    rng = np.random.default_rng(seed)
    c.saveState()
    for i in range(9):
        c.setFillColorRGB(0.05, 0.05, 0.05)
        cx = x + rng.uniform(0, w)
        cy = y + rng.uniform(0, h)
        c.ellipse(cx - rng.uniform(10, 26), cy - rng.uniform(4, 9), cx + rng.uniform(10, 26), cy + rng.uniform(4, 9), stroke=0, fill=1)
    c.restoreState()


def hidden(c, sp):
    if sp.get("hidden_text"):
        c.saveState()
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica", 6)
        c.drawString(48, 28, sp["hidden_text"])
        c.restoreState()


def due_date(sp, v):
    d = dt.date.fromisoformat(sp["date"]) + dt.timedelta(days=terms_days(v.get("payment_terms", "Net 30")))
    return d.isoformat()


def notes_block(c, sp, x, y, color=(0.7, 0.05, 0.05), width=300):
    for n in sp.get("notes", []):
        c.saveState()
        c.setFillColorRGB(*color)
        c.setFont("Helvetica-Bold", 9)
        for ln in wrap(c, n, "Helvetica-Bold", 9, width):
            c.drawString(x, y, ln)
            y -= 12
        c.restoreState()
    return y


# ----------------------------------------------------------------------------- layouts
def layout_classic(c, sp, v, t):
    cur = sp["currency"]
    c.setFont("Helvetica-Bold", 17)
    c.drawString(48, H - 60, v["legal_name"])
    c.setFont("Helvetica", 9)
    y = H - 76
    for ln in v["address"].split("|"):
        c.drawString(48, y, ln)
        y -= 12
    c.drawString(48, y, f"Tax ID (EIN): {v['tax_id']}")
    y -= 12
    c.drawString(48, y, f"{v['email']}  |  {v['phone']}")
    c.setFont("Helvetica-Bold", 28)
    c.drawRightString(W - 48, H - 62, "INVOICE")

    # info box
    bx, by, bw = 362, H - 190, 202
    c.setLineWidth(0.8)
    rows = [("Invoice No:", sp["number"]), ("Invoice Date:", fmt_date(sp["date"], "classic")),
            ("Due Date:", fmt_date(due_date(sp, v), "classic")), ("Terms:", v.get("payment_terms", "Net 30"))]
    if sp["po_ref"] is True:
        rows.insert(3, ("PO Number:", sp["po"]))
    c.rect(bx, by, bw, 18 * len(rows) + 8)
    yy = by + 18 * len(rows) - 8
    for lab, val in rows:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(bx + 8, yy, lab)
        c.setFont("Helvetica", 9.5)
        c.drawString(bx + 88, yy, str(val))
        yy -= 18

    # bill to
    c.setFont("Helvetica-Bold", 9)
    c.drawString(48, H - 190 + 52, "BILL TO")
    c.setFont("Helvetica", 9.5)
    yy = H - 190 + 38
    for ln in [CUSTOMER["name"]] + CUSTOMER["lines"]:
        c.drawString(48, yy, ln)
        yy -= 12

    # table
    ty = H - 270
    c.setFillColorRGB(0.9, 0.9, 0.9)
    c.rect(48, ty - 4, W - 96, 18, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(54, ty + 2, "Item #")
    c.drawString(128, ty + 2, "Description")
    c.drawRightString(400, ty + 2, "Qty")
    c.drawRightString(480, ty + 2, "Unit Price")
    c.drawRightString(W - 54, ty + 2, "Amount")
    ty -= 20
    c.setFont("Helvetica", 9)
    for sku, desc, q, p in sp["lines"]:
        c.drawString(54, ty, sku)
        dl = wrap(c, desc, "Helvetica", 9, 240)
        c.drawString(128, ty, dl[0])
        c.drawRightString(400, ty, f"{q:g}")
        c.drawRightString(480, ty, f"{p:,.2f}")
        c.drawRightString(W - 54, ty, f"{q * p:,.2f}")
        for extra in dl[1:]:
            ty -= 11
            c.drawString(128, ty, extra)
        ty -= 18
    c.line(48, ty + 8, W - 48, ty + 8)

    # totals
    ty -= 8
    lab_x, val_x = 400, W - 54
    def trow(label, val, bold=False):
        nonlocal ty
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10 if bold else 9.5)
        c.drawString(lab_x - 40, ty, label)
        c.drawRightString(val_x, ty, val)
        ty -= 16
    trow("Subtotal", money(t["subtotal"], cur))
    if t["discount"]:
        trow("Discount", "-" + money(t["discount"], cur))
    if t["shipping"]:
        trow("Shipping", money(t["shipping"], cur))
    trow(f"Sales Tax ({t['rate']:g}%)", money(t["tax"], cur))
    c.line(lab_x - 40, ty + 12, W - 48, ty + 12)
    trow("TOTAL DUE", money(t["total"], cur), bold=True)

    # remit
    b = bank_of(sp, v)
    ry = 190
    c.setFont("Helvetica-Bold", 9)
    c.drawString(48, ry, "REMIT PAYMENT TO")
    c.setFont("Helvetica", 9)
    c.drawString(48, ry - 13, f"Bank: {b['bank']}")
    c.drawString(48, ry - 25, f"Account No: {b['account']}   Routing (ABA): {b['routing']}")
    c.drawString(48, ry - 37, f"Account name: {v['legal_name']}")
    notes_block(c, sp, 48, ry - 55)
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(48, 70, "Thank you for your business. Late payments subject to 1.5% monthly service charge.")


def layout_modern(c, sp, v, t):
    cur = sp["currency"]
    c.setFillColorRGB(0.05, 0.32, 0.36)
    c.rect(0, H - 96, W, 96, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(44, H - 52, v["legal_name"])
    c.setFont("Helvetica", 8.5)
    c.drawString(44, H - 68, " , ".join(v["address"].split("|")).replace(" , ", ", "))
    c.drawString(44, H - 80, f"{v['email']}   {v['phone']}")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 44, H - 52, "SALES INVOICE")
    c.setFillColorRGB(0, 0, 0)

    # key facts, two columns of label: value
    y = H - 128
    pairs_l = [("Bill No", sp["number"]), ("Date", fmt_date(sp["date"], "modern")),
               ("Terms", v.get("payment_terms", "Net 30"))]
    if sp["po_ref"] is True:
        pairs_l.append(("Your Ref", sp["po"]))
    for lab, val in pairs_l:
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawString(44, y, lab)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(110, y, str(val))
        y -= 15
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(330, H - 128, "Billed to")
    c.setFillColorRGB(0, 0, 0)
    yy = H - 128
    for ln in [CUSTOMER["name"]] + CUSTOMER["lines"]:
        c.setFont("Helvetica", 9.5)
        c.drawString(385, yy, ln)
        yy -= 13
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(330, yy - 4, "Payment due")
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(385, yy - 4, fmt_date(due_date(sp, v), "modern"))

    ty = H - 262
    c.setFillColorRGB(0.05, 0.32, 0.36)
    c.rect(44, ty - 5, W - 88, 20, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(52, ty + 1, "Description")
    c.drawRightString(390, ty + 1, "Qty")
    c.drawRightString(470, ty + 1, "Rate")
    c.drawRightString(W - 52, ty + 1, "Total")
    c.setFillColorRGB(0, 0, 0)
    ty -= 22
    for sku, desc, q, p in sp["lines"]:
        label = f"{sku}  {desc}".strip()
        c.setFont("Helvetica", 9)
        dl = wrap(c, label, "Helvetica", 9, 300)
        c.drawString(52, ty, dl[0])
        c.drawRightString(390, ty, f"{q:g}")
        c.drawRightString(470, ty, f"{p:,.2f}")
        c.drawRightString(W - 52, ty, f"{q * p:,.2f}")
        for extra in dl[1:]:
            ty -= 11
            c.drawString(52, ty, extra)
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.line(44, ty - 6, W - 44, ty - 6)
        c.setStrokeColorRGB(0, 0, 0)
        ty -= 20

    ty -= 6
    def trow(label, val, bold=False):
        nonlocal ty
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 9.5)
        c.drawString(380, ty, label)
        c.drawRightString(W - 52, ty, val)
        ty -= 16
    trow("Subtotal", money(t["subtotal"], cur))
    trow(f"Tax ({t['rate']:g}%)", money(t["tax"], cur))
    trow("Amount Due", money(t["total"], cur), bold=True)

    # payment box
    b = bank_of(sp, v)
    c.setLineWidth(0.8)
    c.rect(44, 150, 300, 86)
    c.setFont("Helvetica-Bold", 9.5)
    title = "NEW REMITTANCE DETAILS" if sp.get("bank") == "changed" else "Payment details"
    c.drawString(52, 222, title)
    c.setFont("Helvetica", 9)
    c.drawString(52, 208, f"Bank: {b['bank']}")
    c.drawString(52, 195, f"Account number: {b['account']}")
    c.drawString(52, 182, f"ABA routing: {b['routing']}")
    c.drawString(52, 169, f"Beneficiary: {v['legal_name']}")
    notes_block(c, sp, 44, 134, width=420)
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(44, 60, "Questions about this invoice? Contact accounts receivable at the address above.")


def layout_inclusive(c, sp, v, t):
    cur = sp["currency"]
    c.setFont("Helvetica-Bold", 15)
    c.drawCentredString(W / 2, H - 56, "TAX INVOICE")
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(W / 2, H - 76, v["legal_name"])
    c.setFont("Helvetica", 8.5)
    c.drawCentredString(W / 2, H - 90, " - ".join(v["address"].split("|")) + f"  |  EIN {v['tax_id']}")
    c.line(48, H - 100, W - 48, H - 100)

    y = H - 126
    for lab, val in [("Invoice Number", sp["number"]), ("Invoice date", fmt_date(sp["date"], "inclusive")),
                     ("Payment terms", v.get("payment_terms", "Net 30"))]:
        c.setFont("Helvetica", 9.5)
        c.drawString(48, y, lab)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(160, y, str(val))
        y -= 15
    c.setFont("Helvetica", 9.5)
    c.drawString(340, H - 126, "Customer")
    yy = H - 126
    for ln in [CUSTOMER["name"]] + CUSTOMER["lines"][:2]:
        c.setFont("Helvetica-Bold" if ln == CUSTOMER["name"] else "Helvetica", 9.5)
        c.drawString(400, yy, ln)
        yy -= 13

    ty = H - 232
    c.setFillColorRGB(0.93, 0.93, 0.93)
    c.rect(48, ty - 5, W - 96, 20, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(54, ty + 1, "Description of services")
    c.drawRightString(W - 54, ty + 1, "Amount (tax incl.)")
    ty -= 24
    for _, desc, q, p in sp["lines"]:
        c.setFont("Helvetica", 9.5)
        for i, ln in enumerate(wrap(c, desc, "Helvetica", 9.5, 360)):
            c.drawString(54, ty - 12 * i, ln)
        c.drawRightString(W - 54, ty, f"{q * p:,.2f}")
        ty -= 34
    c.line(48, ty + 8, W - 48, ty + 8)
    ty -= 10
    c.setFont("Helvetica-Bold", 11)
    c.drawString(250, ty, f"Total payable (inclusive of {t['rate']:g}% sales tax)")
    c.drawRightString(W - 54, ty, money(t["total"], cur))
    ty -= 16
    c.setFont("Helvetica", 9)
    c.drawString(250, ty, "of which sales tax")
    c.drawRightString(W - 54, ty, money(t["tax"], cur))

    b = bank_of(sp, v)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(48, 190, "Bank transfer")
    c.setFont("Helvetica", 9)
    c.drawString(48, 177, f"{b['bank']}  -  A/C {b['account']}  -  ABA {b['routing']}")
    c.drawString(48, 164, f"Account holder: {v['legal_name']}")
    notes_block(c, sp, 48, 146)


def layout_stacked(c, sp, v, t):
    cur = sp["currency"]
    c.setFont("Helvetica-Bold", 20)
    c.drawString(48, H - 62, v["legal_name"].upper())
    c.setFont("Helvetica", 9)
    y = H - 78
    for ln in v["address"].split("|"):
        c.drawString(48, y, ln)
        y -= 12
    c.drawString(48, y, f"{v['phone']}  |  {v['email']}")
    c.setFont("Helvetica-Bold", 22)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.drawRightString(W - 48, H - 62, "Invoice")
    c.setFillColorRGB(0, 0, 0)

    # label above value
    cols = [("INVOICE NO.", 48, sp["number"]), ("DATE", 190, fmt_date(sp["date"], "stacked")),
            ("DUE DATE", 320, fmt_date(due_date(sp, v), "stacked")),
            ("P.O. REF.", 450, sp["po"] if sp["po_ref"] is True else "")]
    ly = H - 150
    for lab, x, val in cols:
        c.setFont("Helvetica", 7.5)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawString(x, ly, lab)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 11)
        if lab == "INVOICE NO." and sp.get("smudge_number"):
            smudge(c, x - 4, ly - 20, 100, 14, seed=sp.get("seed", 5))
        else:
            c.drawString(x, ly - 15, val)
    c.setFont("Helvetica", 7.5)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(48, H - 190, "BILLED TO")
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 9.5)
    yy = H - 203
    for ln in [CUSTOMER["name"]] + CUSTOMER["lines"][:2]:
        c.drawString(48, yy, ln)
        yy -= 12

    ty = H - 270
    c.setFillColorRGB(0.15, 0.15, 0.15)
    c.rect(48, ty - 5, W - 96, 20, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(54, ty + 1, "DESCRIPTION")
    c.drawRightString(400, ty + 1, "QTY")
    c.drawRightString(480, ty + 1, "PRICE")
    c.drawRightString(W - 54, ty + 1, "AMOUNT")
    c.setFillColorRGB(0, 0, 0)
    ty -= 22
    c.setFont("Helvetica", 9.5)
    for _, desc, q, p in sp["lines"]:
        dl = wrap(c, desc, "Helvetica", 9.5, 300)
        c.drawString(54, ty, dl[0])
        c.drawRightString(400, ty, f"{q:g}")
        c.drawRightString(480, ty, f"{p:,.2f}")
        c.drawRightString(W - 54, ty, f"{q * p:,.2f}")
        for extra in dl[1:]:
            ty -= 11
            c.drawString(54, ty, extra)
        ty -= 20
    c.line(48, ty + 8, W - 48, ty + 8)
    ty -= 10
    c.setFont("Helvetica", 9)
    c.drawString(400, ty, "Tax (exempt)")
    c.drawRightString(W - 54, ty, money(t["tax"], cur))
    ty -= 18
    c.setFont("Helvetica-Bold", 12)
    c.drawString(400, ty, "TOTAL")
    c.drawRightString(W - 54, ty, money(t["total"], cur))

    b = bank_of(sp, v)
    c.setFont("Helvetica", 9)
    c.drawString(48, 180, f"Pay by ACH to {b['bank']}, acct {b['account']}, routing {b['routing']}")
    notes_block(c, sp, 48, 164)
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(48, 80, "Services performed per agreed scope. Net terms apply from invoice date.")


def layout_statement(c, sp, v, t):
    c.setFont("Helvetica-Bold", 18)
    c.drawString(48, H - 60, v["legal_name"])
    c.setFont("Helvetica", 9)
    y = H - 76
    for ln in v["address"].split("|"):
        c.drawString(48, y, ln)
        y -= 12
    c.setFont("Helvetica-Bold", 20)
    c.drawString(48, H - 112, "STATEMENT OF ACCOUNT")
    c.setFont("Helvetica", 10)
    c.drawString(48, H - 140, f"Statement date: {fmt_date(sp['date'], 'statement')}")
    c.drawString(48, H - 154, f"Customer: {CUSTOMER['name']}")
    ty = H - 200
    c.setFillColorRGB(0.9, 0.9, 0.9)
    c.rect(48, ty - 5, W - 96, 18, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 9)
    for x, lab in [(54, "Date"), (140, "Document"), (260, "Description")]:
        c.drawString(x, ty, lab)
    c.drawRightString(470, ty, "Charge")
    c.drawRightString(W - 54, ty, "Balance")
    rows = [("Oct 06, 2026", "Invoice MO-55102", "Office supplies Q4", 3180.40, 3180.40),
            ("Oct 29, 2026", "Invoice MO-55240", "Desk accessories", 1244.00, 4424.40),
            ("Nov 06, 2026", "Invoice MO-55318", "Workstation furniture", 9945.22, 14369.62)]
    ty -= 22
    c.setFont("Helvetica", 9.5)
    for d, doc, desc, ch, bal in rows:
        c.drawString(54, ty, d)
        c.drawString(140, ty, doc)
        c.drawString(260, ty, desc)
        c.drawRightString(470, ty, f"{ch:,.2f}")
        c.drawRightString(W - 54, ty, f"{bal:,.2f}")
        ty -= 18
    c.line(48, ty + 8, W - 48, ty + 8)
    ty -= 14
    c.setFont("Helvetica-Bold", 11)
    c.drawString(380, ty, "Total outstanding")
    c.drawRightString(W - 54, ty, "$14,369.62")
    c.setFont("Helvetica", 9)
    c.drawString(48, ty - 40, "This statement is a summary of open items and is not a request for payment of a new invoice.")


LAYOUTS = dict(classic=layout_classic, modern=layout_modern, inclusive=layout_inclusive,
               stacked=layout_stacked, statement=layout_statement)


def render_pdf(sp, out_path):
    sp = dict(sp, number=sp.get("number") or "")
    v = vendor_of(sp)
    t = totals(sp)
    c = canvas.Canvas(out_path, pagesize=letter)
    c.setTitle(f"Invoice {sp['number']}")
    LAYOUTS[sp["layout"]](c, sp, v, t)
    hidden(c, sp)
    c.showPage()
    c.save()
    return t


# ----------------------------------------------------------------------------- scan simulation
def rasterize(pdf_path, dpi=150):
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "p")
        subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", pdf_path, base], check=True)
        return Image.open(base + ".png").convert("L").copy()


def scanify(pdf_path, out_path, kind="pdf", angle=1.5, seed=1):
    """Turn a digital PDF into a messy scan: skew, blur, uneven light, noise, JPEG artefacts."""
    rng = np.random.default_rng(seed)
    dpi = 150 if kind == "pdf" else 135
    img = rasterize(pdf_path, dpi).filter(ImageFilter.GaussianBlur(0.8 if kind == "pdf" else 1.05))
    arr = np.asarray(img).astype(np.float32)
    h, w = arr.shape
    yy, xx = np.mgrid[0:h, 0:w]
    grad = 0.90 + 0.10 * (xx / w) - 0.05 * (yy / h) + 0.03 * np.sin(yy / h * 6.0)
    arr = arr * grad + rng.normal(0, 6.5, arr.shape)
    for _ in range(140):  # speckles
        x, y = rng.integers(0, w), rng.integers(0, h)
        r = rng.integers(1, 3)
        arr[max(0, y - r):y + r, max(0, x - r):x + r] -= rng.uniform(40, 110)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr)
    img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=228)
    # scanner-bed border
    bordered = Image.new("L", (img.width + 24, img.height + 24), 160)
    bordered.paste(img, (12, 12))
    rgb = bordered.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=58 if kind == "pdf" else 52)
    buf.seek(0)
    jpg = Image.open(buf)
    if kind == "jpg":
        jpg.save(out_path, "JPEG", quality=52)
    else:
        jpg.save(out_path, "PDF", resolution=dpi)

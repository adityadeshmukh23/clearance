"""Document intake: hash, classify digital-vs-scanned, render page images,
read the text layer (with positions) or OCR it (Tesseract), flag hidden text.

All coordinates are normalised to 0..1 of the page image so the UI can overlay
evidence boxes at any zoom level.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import re
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict

import numpy as np
from PIL import Image, ImageFilter, ImageOps

try:
    import pdfplumber
except Exception:  # pragma: no cover
    pdfplumber = None
try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None

MAX_PAGES = 6
VIEW_WIDTH = 1000          # px width of the page image shown in the UI
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int = 0
    conf: float = 1.0
    h: float = 0.0   # word height, normalised

    def box(self):
        return [self.page, round(self.x0, 4), round(self.y0, 4), round(self.x1, 4), round(self.y1, 4)]


@dataclass
class Line:
    words: list
    page: int
    y: float
    h: float

    @property
    def text(self):
        return " ".join(w.text for w in self.words)

    @property
    def x0(self):
        return self.words[0].x0

    @property
    def x1(self):
        return self.words[-1].x1

    def cells(self, gap=0.018):
        """Split a line into cells wherever there is a wide horizontal gap (table columns, label/value pairs)."""
        out, cur = [], [self.words[0]]
        for a, b in zip(self.words, self.words[1:]):
            if b.x0 - a.x1 > gap:
                out.append(cur)
                cur = [b]
            else:
                cur.append(b)
        out.append(cur)
        return out


@dataclass
class Doc:
    path: str
    name: str
    sha256: str
    size: int
    mime: str
    pages: list = field(default_factory=list)       # [{"w":..,"h":..,"image":relpath}]
    words: list = field(default_factory=list)       # visible words
    hidden_words: list = field(default_factory=list)
    text_source: str = "text-layer"                  # text-layer | ocr
    ocr_conf: float | None = None
    notes: list = field(default_factory=list)
    ocr_seconds: float = 0.0
    skew_deg: float = 0.0
    ocr_cached: bool = False

    def lines(self):
        return group_lines(self.words)

    def text(self):
        return "\n".join(l.text for l in self.lines())


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def group_lines(words):
    out = []
    by_page = {}
    for w in words:
        by_page.setdefault(w.page, []).append(w)
    for pg in sorted(by_page):
        ws = sorted(by_page[pg], key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
        hs = [w.h or (w.y1 - w.y0) for w in ws] or [0.01]
        tol = max(0.004, statistics.median(hs) * 0.55)
        cur, cy = [], None
        for w in ws:
            yc = (w.y0 + w.y1) / 2
            if cy is None or abs(yc - cy) <= tol:
                cur.append(w)
                cy = yc if cy is None else (cy * (len(cur) - 1) + yc) / len(cur)
            else:
                out.append(_mkline(cur, pg))
                cur, cy = [w], yc
        if cur:
            out.append(_mkline(cur, pg))
    return out


def _mkline(ws, pg):
    ws = sorted(ws, key=lambda w: w.x0)
    return Line(words=ws, page=pg, y=sum((w.y0 + w.y1) / 2 for w in ws) / len(ws),
                h=statistics.median([(w.h or (w.y1 - w.y0)) for w in ws]))


# --------------------------------------------------------------------------- rendering
def _save_view(img: Image.Image, out_path: str):
    img = img.convert("RGB")
    if img.width != VIEW_WIDTH:
        img = img.resize((VIEW_WIDTH, round(img.height * VIEW_WIDTH / img.width)), Image.LANCZOS)
    img.save(out_path, "JPEG", quality=82)
    return img.size


def render_pdf_page(pdf_path, page_no, dpi=130):
    """Rasterise one page. Poppler's pdftoppm when present; otherwise pdfium (ships with pdfplumber), so no extra install on Windows."""
    if os.environ.get("PDF_RENDERER") != "pdfium" and shutil.which("pdftoppm"):
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, "p")
            subprocess.run(["pdftoppm", "-r", str(dpi), "-f", str(page_no), "-l", str(page_no), "-png", "-singlefile",
                            pdf_path, base], check=True, capture_output=True)
            return Image.open(base + ".png").convert("RGB").copy()
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        return pdf[page_no - 1].render(scale=dpi / 72).to_pil().convert("RGB")
    finally:
        pdf.close()


# --------------------------------------------------------------------------- OCR
def estimate_skew(gray: Image.Image, limit=6.0, step=0.2):
    """Projection-profile deskew: angle (deg, ccw positive) that makes text rows sharpest.
    Ignores a margin (scanner-bed borders) and only scores genuinely dark ink."""
    small = gray.copy()
    small.thumbnail((800, 1000))
    w, h = small.size
    small = small.crop((int(w * 0.07), int(h * 0.07), int(w * 0.93), int(h * 0.93)))
    arr = np.asarray(small).astype(np.float32)
    # ink = clearly darker than the (possibly tinted) paper
    paper = np.percentile(arr, 70)
    bw = Image.fromarray(((arr < paper * 0.55) * 255).astype(np.uint8))
    best, best_a = -1.0, 0.0
    a = -limit
    while a <= limit + 1e-6:
        r = np.asarray(bw.rotate(a, resample=Image.BILINEAR, fillcolor=0)).astype(np.float32)
        score = float(np.var(r.sum(axis=1)))
        if score > best:
            best, best_a = score, a
        a += step
    return best_a


def preprocess_for_ocr(img: Image.Image):
    """Greyscale, flatten uneven lighting, deskew, upscale. Returns (processed_gray, color_for_view, angle)."""
    gray = ImageOps.grayscale(img)
    angle = estimate_skew(gray)
    rotated = gray.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)
    # flatten background illumination
    bg = rotated.filter(ImageFilter.GaussianBlur(28))
    a = np.asarray(rotated).astype(np.float32)
    b = np.asarray(bg).astype(np.float32) + 1.0
    flat = np.clip(a / b * 245.0, 0, 255).astype(np.uint8)
    flat = Image.fromarray(flat)
    flat = ImageOps.autocontrast(flat, cutoff=1)
    return flat, angle


def ocr_image(img: Image.Image, page_idx: int):
    """Returns (words, mean_conf 0..1, angle, view_image)."""
    if pytesseract is None:
        raise RuntimeError("pytesseract is not installed")
    cmd = os.environ.get("TESSERACT_CMD")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    if not shutil.which(pytesseract.pytesseract.tesseract_cmd) and not os.path.exists(pytesseract.pytesseract.tesseract_cmd):
        raise RuntimeError("the Tesseract OCR program is not installed, so scans and photos cannot be read. Digital PDFs still work. "
                           "Install Tesseract (or set TESSERACT_CMD) to read scans")
    flat, angle = preprocess_for_ocr(img)
    # Tesseract likes ~200-300 DPI-equivalent: make the long side ~2400px
    scale = 2400 / max(flat.size)
    if scale > 1.05 or scale < 0.8:
        flat_ocr = flat.resize((round(flat.width * scale), round(flat.height * scale)), Image.LANCZOS)
    else:
        flat_ocr = flat
    data = pytesseract.image_to_data(flat_ocr, lang="eng", config="--oem 1 --psm 11", output_type=pytesseract.Output.DICT)
    W, H = flat_ocr.size
    words, confs = [], []
    for i, t in enumerate(data["text"]):
        t = (t or "").strip()
        if not t:
            continue
        try:
            c = float(data["conf"][i])
        except (TypeError, ValueError):
            c = -1
        if c < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        # drop scanner-bed border junk and 1-char specks with low confidence
        if len(t) == 1 and c < 60 and not t.isalnum():
            continue
        words.append(Word(text=t, x0=x / W, y0=y / H, x1=(x + w) / W, y1=(y + h) / H, page=page_idx, conf=c / 100.0,
                          h=h / H))
        confs.append(c / 100.0)
    # tesseract (psm 11) can emit a word per token; merge nothing here, parsing is line-based
    mean = sum(confs) / len(confs) if confs else 0.0
    return words, mean, angle, flat


# --------------------------------------------------------------------------- PDF text layer
def _is_hidden(ch):
    col = ch.get("non_stroking_color")
    size = ch.get("size") or 10
    white = False
    if col is not None:
        try:
            vals = list(col) if isinstance(col, (list, tuple)) else [col]
            white = len(vals) in (1, 3, 4) and (
                (len(vals) == 3 and all(v >= 0.97 for v in vals)) or (len(vals) == 1 and vals[0] >= 0.97))
        except Exception:
            white = False
    return white or size < 2.5


def read_pdf_words(path):
    """Visible vs hidden words. White text is only 'hidden' if the pixels under it are light
    (white-on-a-dark-header is perfectly normal); microscopic text is always hidden."""
    pages, visible, hidden = [], [], []
    with pdfplumber.open(path) as pdf:
        n = min(len(pdf.pages), MAX_PAGES)
        total_pages = len(pdf.pages)
        for i in range(n):
            pg = pdf.pages[i]
            W, H = float(pg.width), float(pg.height)
            pages.append((W, H))
            raw = pg.extract_words(x_tolerance=1.5, y_tolerance=2, extra_attrs=["size", "non_stroking_color"],
                                   keep_blank_chars=False, use_text_flow=False)
            gray = None
            for w in raw:
                tiny = (w.get("size") or 10) < 2.5
                white = _is_hidden({"non_stroking_color": w.get("non_stroking_color"), "size": 10})
                is_h = tiny
                if white and not tiny:
                    if gray is None:
                        gray = np.asarray(ImageOps.grayscale(render_pdf_page(path, i + 1, dpi=72))).astype(np.float32)
                    x0, x1 = max(0, int(w["x0"])), min(gray.shape[1], int(w["x1"]) + 1)
                    y0, y1 = max(0, int(w["top"])), min(gray.shape[0], int(w["bottom"]) + 1)
                    region = gray[y0:y1, x0:x1]
                    is_h = bool(region.size and float(region.mean()) > 235)
                word = Word(text=w["text"], x0=w["x0"] / W, y0=w["top"] / H, x1=w["x1"] / W, y1=w["bottom"] / H,
                            page=i, conf=1.0, h=(w["bottom"] - w["top"]) / H)
                (hidden if is_h else visible).append(word)
    return pages, visible, hidden, total_pages


# --------------------------------------------------------------------------- OCR cache
# OCR is deterministic, so the result for a given file (by SHA-256) is stored and reused: the prepared samples ship pre-warmed
# (matters on a slow free host) and a re-upload of the same scan is instant. A cache miss simply runs OCR as usual.
def _cache_roots():
    from . import config
    return [os.path.join(config.REF_DIR, "ocr_cache"), os.path.join(config.DATA_DIR, "ocr_cache")]


def _cache_get(sha, workdir):
    from . import config
    if not config.OCR_CACHE:
        return None
    for base in _cache_roots():
        d = os.path.join(base, sha)
        f = os.path.join(d, "ocr.json")
        if not os.path.exists(f):
            continue
        try:
            meta = json.load(open(f))
            for pg in meta["pages"]:
                shutil.copy(os.path.join(d, pg["image"]), os.path.join(workdir, pg["image"]))
            meta["words"] = [Word(**w) for w in meta["words"]]
            return meta
        except Exception:
            continue
    return None


def _cache_put(sha, pages, words, mean, angle, workdir):
    from . import config
    if not config.OCR_CACHE:
        return
    try:
        d = os.path.join(_cache_roots()[1], sha)
        os.makedirs(d, exist_ok=True)
        for pg in pages:
            shutil.copy(os.path.join(workdir, pg["image"]), os.path.join(d, pg["image"]))
        json.dump(dict(pages=pages, words=[asdict(w) for w in words], mean=mean, angle=angle), open(os.path.join(d, "ocr.json"), "w"))
    except Exception:
        pass


# --------------------------------------------------------------------------- main entry
def load_document(path, workdir, force_ocr=False, name=None):
    """Read a PDF/image into a Doc. Page view images are written to workdir/page-N.jpg."""
    import time
    os.makedirs(workdir, exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    doc = Doc(path=path, name=name or os.path.basename(path), sha256=sha256_file(path), size=os.path.getsize(path),
              mime="application/pdf" if ext == ".pdf" else "image/" + ext.strip(".").replace("jpg", "jpeg"))

    if ext in IMAGE_EXT:
        hit = _cache_get(doc.sha256, workdir)
        if hit:
            doc.pages, doc.words, doc.ocr_conf, doc.skew_deg, doc.ocr_cached = hit["pages"], hit["words"], hit["mean"], hit["angle"], True
            doc.text_source = "ocr"
            doc.notes.append(f"Image file, OCR at {hit['mean'] * 100:.0f}% mean confidence, deskewed {hit['angle']:+.2f} deg (OCR result reused from cache)")
            return doc
        img = Image.open(path)
        img = ImageOps.exif_transpose(img).convert("RGB")
        t0 = time.time()
        words, mean, angle, flat = ocr_image(img, 0)
        doc.ocr_seconds = time.time() - t0
        doc.skew_deg = angle
        size = _save_view(flat, os.path.join(workdir, "page-1.jpg"))
        doc.pages = [{"w": size[0], "h": size[1], "image": "page-1.jpg"}]
        doc.words, doc.text_source, doc.ocr_conf = words, "ocr", mean
        doc.notes.append(f"Image file, OCR at {mean * 100:.0f}% mean confidence, deskewed {angle:+.2f} deg")
        _cache_put(doc.sha256, doc.pages, words, mean, angle, workdir)
        return doc

    if ext != ".pdf":
        raise ValueError(f"Unsupported file type: {ext or 'unknown'}. Upload a PDF or an image (PNG/JPG/TIFF).")
    if pdfplumber is None:
        raise RuntimeError("pdfplumber not installed")

    sizes, visible, hidden, total_pages = read_pdf_words(path)
    if total_pages > MAX_PAGES:
        doc.notes.append(f"Document has {total_pages} pages; only the first {MAX_PAGES} were processed.")
    scanned = force_ocr or len(visible) < 12
    hit = _cache_get(doc.sha256, workdir) if scanned and not force_ocr else None
    if hit:
        doc.pages, doc.words, doc.ocr_conf, doc.skew_deg, doc.ocr_cached = hit["pages"], hit["words"], hit["mean"], hit["angle"], True
        doc.text_source = "ocr"
        doc.notes.append(f"No usable text layer ({len(visible)} words): scanned image. OCR at {hit['mean'] * 100:.0f}% mean confidence (OCR result reused from cache)")
        scanned = None
    if scanned:
        words_all, confs = [], []
        t0 = time.time()
        for i in range(len(sizes)):
            img = render_pdf_page(path, i + 1, dpi=170)
            words, mean, angle, flat = ocr_image(img, i)
            words_all += words
            confs.append(mean)
            doc.skew_deg = angle if i == 0 else doc.skew_deg
            size = _save_view(flat, os.path.join(workdir, f"page-{i + 1}.jpg"))
            doc.pages.append({"w": size[0], "h": size[1], "image": f"page-{i + 1}.jpg"})
        doc.ocr_seconds = time.time() - t0
        doc.words, doc.text_source = words_all, "ocr"
        doc.ocr_conf = sum(confs) / len(confs) if confs else 0.0
        doc.notes.append(f"No usable text layer ({len(visible)} words): scanned image. OCR at "
                         f"{doc.ocr_conf * 100:.0f}% mean confidence, deskewed {doc.skew_deg:+.2f} deg")
        _cache_put(doc.sha256, doc.pages, words_all, doc.ocr_conf, doc.skew_deg, workdir)
    elif scanned is None:
        pass
    else:
        for i in range(len(sizes)):
            img = render_pdf_page(path, i + 1, dpi=130)
            size = _save_view(img, os.path.join(workdir, f"page-{i + 1}.jpg"))
            doc.pages.append({"w": size[0], "h": size[1], "image": f"page-{i + 1}.jpg"})
        doc.words, doc.text_source = visible, "text-layer"
        doc.notes.append(f"Digital PDF with text layer ({len(visible)} words)")
    doc.hidden_words = hidden
    if hidden:
        doc.notes.append(f"{len(hidden)} hidden words found (white or microscopic text)")
    return doc


def doc_to_public(doc: Doc):
    return dict(name=doc.name, sha256=doc.sha256, size=doc.size, mime=doc.mime, pages=doc.pages,
                text_source=doc.text_source, ocr_conf=doc.ocr_conf, notes=doc.notes,
                n_words=len(doc.words), n_hidden=len(doc.hidden_words), skew_deg=round(doc.skew_deg, 2),
                ocr_seconds=round(doc.ocr_seconds, 2), ocr_cached=doc.ocr_cached)

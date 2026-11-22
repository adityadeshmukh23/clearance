"""Pre-compute OCR for the prepared samples so they run instantly on a slow free host. Safe to skip: a cache miss just runs OCR."""
import glob, os, shutil, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
os.environ["OCR_CACHE"] = "1"
sys.path.insert(0, ROOT)
from app import docio, config  # noqa: E402

n = 0
for f in sorted(glob.glob(os.path.join(config.SAMPLES_DIR, "*"))):
    if f.endswith(".json"):
        continue
    d = docio.load_document(f, tempfile.mkdtemp())
    if d.text_source == "ocr":
        n += 1
        print("OCR cached:", os.path.basename(f), f"{len(d.words)} words")
dst = os.path.join(config.REF_DIR, "ocr_cache")
shutil.rmtree(dst, ignore_errors=True)
if os.path.isdir(os.path.join(tmp, "ocr_cache")):
    shutil.copytree(os.path.join(tmp, "ocr_cache"), dst)
print(f"{n} scanned samples warmed -> {dst}")

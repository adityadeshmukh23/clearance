"""Runtime configuration (all overridable with environment variables)."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "runtime"))   # writable state (db, uploads, page images)
SEED_DIR = os.environ.get("SEED_DIR", os.path.join(ROOT, "seed"))      # pristine state shipped with the app
REF_DIR = os.path.join(ROOT, "data")                                    # reference CSVs + policy.json
SAMPLES_DIR = os.path.join(ROOT, "samples")
STATIC_DIR = os.path.join(ROOT, "static")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# Free-tier Gemini models, tried in order (each model has its own free quota). Override with GEMINI_MODELS.
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",") if m.strip()]
GEMINI_BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_TIMEOUT = float(os.environ.get("GEMINI_TIMEOUT", "40"))

# Minimum time each stage stays on screen in the live run view (ms). Pure UX pacing, 0 for batch/seed runs.
STAGE_MIN_MS = int(os.environ.get("STAGE_MIN_MS", "550"))
AS_OF_DATE = os.environ.get("AS_OF_DATE", "")       # pin "today" for tests (YYYY-MM-DD)
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "15"))

# Reuse stored OCR results by file hash. Off locally so the live demo shows real OCR; the Docker image turns it on for slow free hosts.
OCR_CACHE = os.environ.get("OCR_CACHE", "0") == "1"

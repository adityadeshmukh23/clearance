#!/usr/bin/env sh
# Local run on macOS / Linux. Needs Python 3.10+ and (for scans) Tesseract: brew install tesseract  |  sudo apt install tesseract-ocr
set -e
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
echo "Open http://localhost:8000"
exec python -m uvicorn app.main:app --port "${PORT:-8000}"

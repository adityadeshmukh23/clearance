FROM python:3.12-slim

# Tesseract reads scans and photos; Poppler rasterises PDFs (pdfium is the built-in fallback).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/runtime \
    OCR_CACHE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app app
COPY static static
COPY data data
COPY samples samples
COPY seed seed

# Free hosts give the container a PORT; default 8000 for local use.
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

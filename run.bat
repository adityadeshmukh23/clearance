@echo off
rem Local run on Windows. Needs Python 3.10+ and, for scans, Tesseract (https://github.com/UB-Mannheim/tesseract/wiki).
cd /d "%~dp0"
if exist .env for /f "usebackq tokens=1,* delims==" %%a in (".env") do if not "%%a"=="" if not "%%a:~0,1%"=="#" set "%%a=%%b"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
echo Open http://localhost:8000
python -m uvicorn app.main:app --port 8000

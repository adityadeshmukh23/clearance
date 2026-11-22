# Your submission guide (do these in order)

Deadline: **Monday 23 Nov 2026**. Aim to send everything by **Sunday 22 Nov** so a hiccup does not cost you the submission.

## Today (about 20 minutes)

1. **Email your coordinator your pick now.** Subject `ASA Case Study — [Your Name] — PS-1`. Text is in `docs/EMAILS.md`.
2. **Get a free Gemini key**: https://aistudio.google.com > "Get API key". No card needed.
3. **Get the app running on your machine**, whichever is easiest:
   - Docker Desktop installed: `docker build -t clearance .` then `docker run -p 8000:8000 -e GEMINI_API_KEY=YOURKEY clearance`
   - Otherwise Python 3.10+: run `run.bat` (Windows) or `./run.sh`. Put `GEMINI_API_KEY=...` in a file called `.env` first (copy `.env.example`). For scanned samples (E1a, E3) install Tesseract; on Windows use the UB-Mannheim installer and set `TESSERACT_CMD` in `.env`.
4. Open http://localhost:8000. The chip in the top bar says "AI reader configured". **Click it**: it must turn green and say "AI reader ready: <model>". If it fails, the message says why (bad key, quota, network). The app still works on the local reader either way.

## Next two days

5. **Check the AI path once yourself.** Run S01 with the reader on "AI + local". Open the Data tab: the fields should match the document. If the extract stage notes a fallback ("rate-limited, trying the next model"), that is the free tier doing its job; it will still finish. If the free quota is annoying while you rehearse, choose "Local only", which uses the same pipeline.
6. **Publish the live link** (the guide asks for a link to the process). Best option: Render free, steps in the README. Test the URL from your phone. If you cannot use Render, a temporary link works for the video only: install `cloudflared` and run `cloudflared tunnel --url http://localhost:8000`; it dies when your laptop sleeps, so say so in the email and offer to run it live.
7. **Rehearse the demo twice** using `docs/DEMO_SCRIPT.md`. Press **Reset demo** before every take.
8. **Record the video** (Loom or any screen recorder, 5 minutes max, your own voice, no AI voice). Use the script as a guide and say it in your own words; people can tell when it is read.
9. **Send the submission** (`docs/EMAILS.md`): the live link and the video link.

## Before the interview

- Open the live link a few minutes early so a sleeping free server is awake.
- Press Reset demo, then do one full pass, then Reset demo again.
- Read `docs/INTERVIEW_PREP.md` out loud once. The questions there are the ones an interviewer is likely to ask about this build.
- Have a plan B: if the network or AI is slow, switch the reader to "Local only" and the pace to "Instant". The decisions are identical.

## What was and was not verified here

Verified: all 14 prepared invoices reach the intended decision with the intended reasoning; 35 seeded runs build the dashboard; the live stage stream, review override, what-if, replay and reset work in a real browser; the app installs from a clean environment; the AI path passes 21 checks against a mock of the Gemini service (rate limits, bad key, malformed answer, misread total, a "model" that obeys a hidden instruction, a rewrite that changes a number).

Not verified: a call to the real Gemini service (there was no key in the build environment) and a Docker build (no Docker daemon there). Both are covered by the steps above: the reader chip tests your key, and `docker build` is a standard image from `python:3.12-slim` plus Tesseract.

Only you can: record the video in your own voice, create the Render and GitHub accounts, and send the emails.

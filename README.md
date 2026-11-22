# Clearance: an invoice desk that explains its decisions

Case study PS-1 (Invoice processing, from PDF to decision). Upload an invoice, watch ten stages run live, and get one of four decisions with the evidence behind it: **approve**, **hold** (the vendor must supply something), **escalate** (a named person must look), or **reject**.

The principle: **the AI reads and writes; deterministic rules decide; arithmetic is re-checked in code.** Two independent readers (an AI model and a local PDF/OCR/rules reader) are compared before anything is trusted. Nothing the AI says can approve an invoice.

## Run it

You need one of these. All data is synthetic.

**1. Docker (any OS, nothing else to install)**
```
docker build -t clearance .
docker run -p 8000:8000 -e GEMINI_API_KEY=your_key clearance      # key is optional
```
Open http://localhost:8000.

**2. Local Python (macOS / Linux: `./run.sh`, Windows: `run.bat`)**
Python 3.10+. For scanned files install Tesseract (`brew install tesseract`, `sudo apt install tesseract-ocr`, or the Windows installer from UB-Mannheim and set `TESSERACT_CMD`). Digital PDFs work without it.

**3. Hosted, free (Render)**
Push this folder to a GitHub repo, then Render > New > Blueprint > pick the repo (`render.yaml` is included, plan `free`). Add `GEMINI_API_KEY` in the dashboard. Free services sleep after 15 minutes idle and wake in about a minute; open the page before a demo. The filesystem is ephemeral, so every restart returns to the seeded state, which is what you want for a demo.

### AI reader (optional, free)
Create a key at https://aistudio.google.com (no card), set `GEMINI_API_KEY`, then click the reader chip in the top bar to test the connection. Without a key the local reader does the extraction and the app says so on every run. Free-tier prompts may be used by Google, so use only synthetic invoices. Model names are listed in `app/config.py`; each model has its own free quota and the app moves to the next on a 429.

## What happens in a run

| Stage | What it does | Who decides |
|---|---|---|
| Receive and read | Digital PDFs give text and positions; scans and photos are deskewed and read with OCR | code |
| Is this an invoice? | Statements and other documents are turned away before any AI call | rules |
| Extract the data | AI returns fields with confidence and the evidence it read; the local reader does the same independently | AI and code |
| Verify the numbers | Subtotal, tax, total re-added; missing or low-confidence fields stop the run | rules |
| Vendor and bank | Match by tax ID and name; compare bank account and e-mail domain with the vendor master | rules |
| Duplicates | Same file, same or re-formatted number, same vendor + amount + date; a resubmission of a held invoice replaces it | rules |
| PO match | Printed PO, near match, or inferred from items and prices; price, quantity received (3-way), cumulative balance | rules |
| Policy | Approval limit, stale or future dates, tolerances (editable in the app) | rules |
| Decide | Reject beats escalate beats hold; each decision lists the checks that caused it | rules |
| Draft and update | Vendor e-mail, escalation brief or ERP payload (drafts only); reserves the PO balance | code, AI wording |

Every field is boxed on the document as evidence, every check shows expected versus found, and failures turn the evidence red.

## Edge cases designed for this case study

| Id | Scenario | Why it is hard | Outcome |
|---|---|---|---|
| E1a | Skewed, smudged scan; invoice number blotted out; no PO printed | OCR; the PO must be inferred from vendor, items and prices (99% match); missing data must not be guessed | Hold, vendor e-mail drafted |
| E1b | The vendor resubmits with the number | Must **not** be flagged as a duplicate; it supersedes the held run | Approve |
| E2a, E2b | Milestone 2 then 3 of 3 against one PO | The second only fails cumulatively; needs memory of earlier runs | Approve, then escalate: over by $5,400 |
| E3 | A re-scanned JPG of an already-paid invoice, number reformatted `20377-A` | New file hash and new number string, same invoice | Reject as duplicate |
| E4 | Numbers match the PO perfectly, but bank details changed and the contact e-mail is a look-alike domain | Only the vendor-master comparison catches it; payment fraud pattern | Escalate to Treasury, callback to the number on file |

Also covered: unknown vendor, blocked vendor, 3-way match failure (billed 100, received 80, price +4%), a statement instead of an invoice, hidden prompt-injection text in the PDF, a price within tolerance (+1.1%), and a bundled tax-inclusive services invoice.

**Demo order:** Reset demo, then S01, E1a, E1b, E2a, E2b, E3, E4, then the Ledger. E1b must follow E1a and E2b must follow E2a (the samples say so).

## The screens

- **Desk**: upload or pick a sample; choose reader (AI + local or local only) and the pace of the stages.
- **Run view**: stage rail, the document with evidence boxes and a decision stamp, and tabs for Result, Data (what was read, vendor and PO comparison, two readers compared), Checks, Drafts, and Audit and raw JSON. Replay any run.
- **Ledger**: dashboard (straight-through rate, mix of decisions, value approved and held back, runs per day, reasons for non-approval), review queue with approve/reject plus a required reason, and the full run history with filters.
- **Reference data**: vendor master, purchase orders with balances, editable payment policy. "Re-run with current policy" on any run is a what-if that saves nothing.
- **How it works**: the pipeline, assumptions, limits and a system check.

## Tests

```
python3 tests/run_all.py          # all 14 scenarios, fresh database, local reader
python3 tests/probe_extract.py    # extractor against ground truth
python3 tests/test_ai_path.py     # AI path against a mock Gemini service (21 checks)
python3 tools/build_samples.py && python3 tools/make_seed.py   # regenerate samples and the seeded history (needs requirements-dev.txt)
```
The AI-path test uses a mock of the Gemini REST API (success, rate limits, bad key, non-JSON, a misread total, a model that "obeys" a hidden instruction, a polish that changes a number). The real Gemini service has not been called from the development environment, so use the reader chip to confirm your own key.

## Layout

```
app/       pipeline, extractors (local + Gemini), matching rules, decisions, narration, FastAPI app
static/    single-page UI (plain HTML, CSS, JS; self-hosted font; no build step)
data/      vendor master, POs, policy, seed invoices, OCR cache for the samples
samples/   the 14 prepared invoices + manifest      seed/   35 prior runs for the dashboard
tools/     generators for the synthetic documents    tests/  scenario, extractor and AI-path tests
docs/      submission guide, demo script, interview prep, e-mails
```

## Assumptions and limits

- The vendor master and PO list are the source of truth; an invoice never changes them.
- POs are priced net of tax, so the net amount is matched. A different currency is escalated, not converted.
- A person spends about 7 minutes on an invoice; this only drives the hours-saved estimate.
- Drafts are not sent and payloads are not posted; the output is the artifact.
- There is no login. It is a demo: anyone with the link can upload, review and press Reset demo.
- Heavily degraded photos can fall under the confidence floor and go to a person, by design.
- Next: a real ERP and mailbox connection, vendor-specific layout learning from corrections, multi-currency, and measuring field-level accuracy on real volume.

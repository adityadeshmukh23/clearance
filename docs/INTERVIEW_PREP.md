# Interview prep: questions this build invites

Know the answers in your own words. The facts in brackets are what the app actually does.

**Why not let an LLM decide everything?**
Decisions need to be consistent, explainable and safe against manipulation. Policy (tolerances, limits, duplicate rules) is code the business can read and change. The model does the part it is good at, reading messy documents. [Hidden text in a PDF that tells an AI to approve is treated as a finding; the "obey injection" test shows a model that complies still cannot change the outcome.]

**How do you trust the extraction?**
Three layers: each field carries a confidence and the evidence it was read from; subtotal, tax and total are re-added in code; and two independent readers (AI and local PDF/OCR/rules) are compared on invoice number, date, total and PO. A disagreement or a low-confidence invoice number or total stops the run. [Policy: minimum field confidence 0.80.]

**Why four outcomes, not two?**
Because the next action differs. Hold means the vendor can fix it (an e-mail is drafted asking only for what is missing). Escalate means a person with authority must look and the brief says who and why. Reject means no payment (duplicate, unknown or blocked vendor, not an invoice). Approve posts. [Precedence: reject, then escalate, then hold.]

**What if there are two problems at once?**
All findings are kept and listed. The decision is the most severe outcome, and "deciding" findings are separated from the rest. [X3 has two independent failures: billed more than received and a price 4% over.]

**How does PO matching work when no PO is printed?**
It scores open POs for that vendor on line items and prices, and adopts one only if the match is high and clearly ahead of the runner-up. [E1a: PO-4540 at 99%, next best 60%.] Otherwise it escalates as ambiguous rather than guessing.

**Why is E2b interesting?**
It is stateful. Each invoice alone fits the PO; the cumulative total does not. The PO balance is reserved when an invoice is approved, so later invoices see it. A reviewer's override also reserves the balance.

**How are duplicates caught when the number changes?**
The file hash catches identical files. The normalised number (prefix and suffix stripped) plus vendor catches `20377-A`. Vendor, amount and date within a window catches re-numbered copies. [E3 matches INV-20377 paid on 8 Nov.] A held invoice that is resubmitted is not a duplicate; it supersedes the held run.

**What about prompt injection?**
The prompt calls the document untrusted data; the model only returns fields; local rules scan for instructions aimed at AI and for hidden text (white or microscopic); and nothing the model outputs reaches the decision logic except as extracted values that go through the same checks. [X5.]

**What if the AI is down or rate-limited?**
Models are tried in order (each has its own free quota); on failure the local reader takes over, the run view says so, and the decision logic is the same. The only loss is the second reader's cross-check.

**How would you measure accuracy in production?**
Field-level accuracy against human-corrected values, straight-through rate, the rate at which humans overturn escalations, and false approvals (the number that matters most). Reviewer corrections become labelled data for vendor-specific layouts.

**How would this scale?**
The pipeline is a function of one run. Put runs on a queue, keep the database as the system of record (Postgres), cache OCR by file hash (already done for samples), and run extraction concurrently. The decision stage is cheap; extraction cost dominates.

**What did you deliberately leave out?**
Sending the e-mail or posting to an ERP (drafts only), multi-currency conversion (escalated), learning from corrections, and authentication. Each is a real next step, not a hidden gap.

**What are the weaknesses?**
Heavily degraded photos, handwriting and unusual layouts can fall below the confidence floor and go to a person. The 7-minutes-per-invoice figure for hours saved is an assumption and is labelled as one. The data is synthetic.

**Walk me through a decision.**
Open E4, Checks tab: bank account differs from the vendor master (ends 3920 versus 7788), the invoice announces new remittance details, the contact domain is a look-alike (83% similar). Route: Treasury. Next step: call the number in the vendor master, not the one on the invoice, and have a second approver confirm before any payment.

**Why this problem statement?**
It has judgment, state and a real cost of being wrong, and it lets the interface show reasoning rather than just a result.

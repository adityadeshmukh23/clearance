# Demo script: 5-minute video and the live interview

Say it in your own words. The lines below are what to get across, not a text to read. Speak slowly; the screen is busy enough.

**Setup:** click **Reset demo**. Reader: "AI + local" (or "Local only" if the key is not set up). Pace: **Demo**. Browser zoom at 100%, window wide (the run view has three columns). Close other tabs and notifications.

Run order is fixed: **S01, E1a, E1b, E2a, E2b, E3, E4, Ledger.** E1b must follow E1a and E2b must follow E2a; the Desk shows an amber "Run after..." hint on both.

## The 5-minute video

| Time | On screen | What to say |
|---|---|---|
| 0:00 | Desk | "This is Clearance, my answer to the invoice problem. An invoice goes in, and a reasoned decision comes out: approve, hold, escalate or reject. The idea I built around is that the AI reads and writes, but plain rules decide. An invoice can't talk its way into being paid." |
| 0:25 | Run **S01** (happy path). Watch the stage rail fill in. | "This is a clean digital invoice from Halden. On the left are the ten stages, running live. It reads the document, confirms it's an invoice, extracts the fields, re-adds the numbers in code, checks the vendor and bank details against the vendor master, checks for duplicates, and matches it to PO-4501." |
| 0:50 | Stamp lands. Click **Data**, hover a few fields. | "Approved. Every value is boxed on the document where it was read, so you can check the evidence in a second. Here are the vendor and bank details compared to the master, and the PO with the balance left after this invoice." Click **Drafts**: "This is the ERP posting that would be sent. It isn't posted. I treat the output as an artifact." |
| 1:20 | Run **E1a** (scanned, smudged). | "Edge case one. A skewed, smudged scan, the invoice number is blotted out and no PO is printed. It's read with OCR. It doesn't guess the number." |
| 1:40 | Result tab, then **Drafts**. | "It holds the invoice and waits on the vendor. And it didn't give up on the PO: it inferred PO-4540 from the vendor, the line items and the prices, at 99%, and says so. It drafted the request to the vendor, asking only for what's missing." |
| 2:00 | Run **E1b**. | "The vendor resubmits with a clean copy. A naive system would call this a duplicate. Here it's recognised as the answer to the held invoice, replaces it, and gets approved. The process remembers what it has seen." |
| 2:25 | Run **E2a**, then **E2b** (open the Data tab: the PO balance bar is at the top). | "Edge case two. One PO billed in three milestones. The second milestone passes. The third looks fine on its own, but cumulatively it's over the PO by $5,400. Only a process with memory catches that, so it escalates to the buyer." |
| 3:05 | Run **E3**. | "Edge case three. This is a re-scanned photo of an invoice we already paid. New file, new hash, and the vendor even changed the number to 20377-A. It still matches on the normalised number, vendor, amount and date, and rejects it as a duplicate." |
| 3:35 | Run **E4**. Hover the red checks so the boxes light up. | "Edge case four, and the one I worry about most. The numbers match the PO perfectly. But the bank account differs from the vendor master and the contact email is a look-alike domain. This is the classic payment-fraud pattern. It stops the payment, routes to Treasury, and says: call the number on file, not the number on the invoice." |
| 4:10 | **Ledger** | "Everything lands here. Straight-through rate, the split of decisions, how much value was held back for risk, and what's waiting for a person. Open an escalated one: a reviewer can approve or reject, but must give a reason, and it's written to the audit trail." |
| 4:40 | Reference data (policy), then back to a run | "The rules are code with settings I can change, like the tolerance or the approval limit. I can re-run any invoice with the current policy as a what-if that saves nothing." |
| 4:55 | Desk | "Next I'd connect it to a real ERP and mailbox and measure field accuracy on real volume. Thanks for watching." |

If you are running long, drop E2 or the policy page. Keep the happy path, E1 and E4.

## The live interview

Same order, but let them steer. Offer: "Do you want to start with the happy path, or with something that breaks?"

Good moments to slow down and let them look:
- **Stage rail and stamp**: "Every stage reports what it checked and how long it took."
- **Checks tab**: expected versus found, and hovering a check lights up the evidence on the document.
- **Two readers compared** (Data tab, AI mode): the AI reader and the independent local reader are compared on number, date, total and PO. If they disagree, the invoice is not trusted.
- **X5, hidden instruction**: "There's white text in this PDF telling an AI reviewer to approve it. The model only extracts. Rules decide, and the hidden text is itself a finding."
- **Upload** something they bring: drag any PDF or image onto the Desk. A non-invoice is turned away; a blurry photo goes to a person.

If something goes wrong:
- AI slow or rate-limited: it falls back on its own and says so. Or set the reader to "Local only".
- Page looks stuck: the run view reads from the saved event log. Press Replay, or open the run from the Ledger.
- Free server asleep: wait about a minute and reload.
- Anything odd with state: **Reset demo** restores the seeded history and PO balances in a second.

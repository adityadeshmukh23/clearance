"""The invoice process: ten visible stages from file to decision.

    intake -> classify -> extract -> verify -> vendor -> duplicates -> po_match -> policy -> decide -> output

Each stage emits events (stored in run_events, streamed over SSE and replayable), and every check is a finding with
a severity, an outcome hint and a route. The decision engine only looks at findings: models read, rules decide.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import traceback

from . import config, db, decision as dec, extract_ai, narrate
from . import matching, rules
from .docio import doc_to_public, load_document
from .extract_local import extract_local
from .matching import F
from .normalize import norm_po

STAGES = [
    ("intake", "Receive and read the document"),
    ("classify", "Is this an invoice?"),
    ("extract", "Extract the data"),
    ("verify", "Verify the numbers"),
    ("vendor", "Verify vendor and bank details"),
    ("duplicates", "Check for duplicates"),
    ("po_match", "Match to a purchase order"),
    ("policy", "Apply payment policy"),
    ("decide", "Decide"),
    ("output", "Draft the output and update the books"),
]
LABELS = dict(STAGES)

FIELD_LABELS = dict(vendor_name="Vendor", invoice_number="Invoice number", invoice_date="Invoice date", due_date="Due date",
                    payment_terms="Payment terms", po_reference="PO reference", currency="Currency", subtotal="Subtotal",
                    tax_amount="Tax", tax_rate="Tax rate %", shipping="Shipping", discount="Discount", total="Total",
                    vendor_tax_id="Vendor tax ID", vendor_email="Vendor e-mail", bank_account="Bank account",
                    bank_routing="Routing number", bank_name="Bank")


class Emitter:
    def __init__(self, run_id, pace_ms):
        self.run_id, self.pace = run_id, pace_ms / 1000.0
        r = db.row("SELECT COALESCE(MAX(seq),0) m FROM run_events WHERE run_id=?", (run_id,))
        self.seq = r["m"]
        self.t = {}
        self.slept = 0.0

    def _write(self, stage, status, payload):
        self.seq += 1
        db.x("INSERT INTO run_events (run_id,seq,ts,stage,status,payload) VALUES (?,?,?,?,?,?)",
             (self.run_id, self.seq, time.time(), stage, status, json.dumps(payload, default=str)))

    def start(self, stage, note=""):
        self.t[stage] = time.time()
        self._write(stage, "running", dict(title=LABELS.get(stage, stage), note=note))

    def progress(self, stage, msg):
        self._write(stage, "progress", dict(message=msg))

    def done(self, stage, status, summary, details=None, checks=None):
        spent = time.time() - self.t.get(stage, time.time())
        work_ms = int(spent * 1000)            # real work; the pause below is only there so a person can follow along
        if spent < self.pace:
            self.slept += self.pace - spent
            time.sleep(self.pace - spent)
        self._write(stage, status, dict(title=LABELS.get(stage, stage), summary=summary, details=details or {},
                                        checks=checks or [], ms=work_ms))

    def skip(self, stage, why):
        self._write(stage, "skipped", dict(title=LABELS.get(stage, stage), summary=why, details={}, checks=[], ms=0))


def _status_of(checks):
    if any(c["severity"] == "fail" for c in checks):
        return "fail"
    if any(c["severity"] == "warn" for c in checks):
        return "warn"
    return "done"


def _eq(a, b):
    if a is None or b is None:
        return None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 0.011
    return re.sub(r"[^a-z0-9]", "", str(a).lower()) == re.sub(r"[^a-z0-9]", "", str(b).lower())


def _crosscheck(ai_ex, local_ex):
    rows = []
    for k, lab in (("invoice_number", "Invoice number"), ("invoice_date", "Invoice date"), ("total", "Total"), ("po_reference", "PO reference")):
        a, b = ai_ex["fields"][k]["value"], local_ex["fields"][k]["value"]
        agree = _eq(a, b)
        rows.append(dict(field=lab, ai=a, local=b, agree=agree))
    return rows


def _fields_table(ex):
    out = []
    for k, lab in FIELD_LABELS.items():
        f = ex["fields"][k]
        if f["value"] is None and f["confidence"] == 0 and k not in ("invoice_number", "po_reference"):
            continue
        out.append(dict(key=k, label=lab, value=f["value"], confidence=f["confidence"], evidence=f.get("evidence", ""), boxes=f.get("boxes", [])))
    return out


def _as_of(r):
    if r.get("as_of"):
        return dt.date.fromisoformat(r["as_of"])
    if config.AS_OF_DATE:
        return dt.date.fromisoformat(config.AS_OF_DATE)
    return dt.date.today()


# =============================================================================
def process(run_id, mode="auto", dry_run=False, pace_ms=None):
    """Run the full pipeline for an existing runs row. Never raises: failures become a decision or an error state."""
    r = db.row("SELECT * FROM runs WHERE id=?", (run_id,))
    db.set_exclude_runs([r.get("whatif_of")] if r.get("whatif_of") else [])
    pace = config.STAGE_MIN_MS if pace_ms is None else pace_ms
    em = Emitter(run_id, pace)
    t_run = time.time()
    policy = db.get_policy()
    as_of = _as_of(r)
    workdir = os.path.join(config.DATA_DIR, "runs", str(run_id))
    findings, result = [], {}
    cur_stage = "intake"
    try:
        # ------------------------------------------------------------- 1 intake
        em.start("intake")
        doc = None
        try:
            doc = load_document(r["file_path"], workdir, name=r["filename"])
        except Exception as e:  # corrupt / encrypted / unreadable
            findings.append(F("UNREADABLE_FILE", "The file could not be opened",
                              f"Reading failed: {str(e)[:140]}. It may be corrupt, password-protected or not a real invoice file.",
                              "fail", "HOLD", "vendor", stage="intake"))
            em.done("intake", "fail", "Could not open or read the file", dict(error=str(e)[:200]), [findings[-1]])
        if doc is not None:
            info = doc_to_public(doc)
            summ = (f"{'Scanned image' if doc.text_source == 'ocr' else 'Digital PDF'}, {len(doc.pages)} page{'s' if len(doc.pages) != 1 else ''}"
                    + ((f", OCR {doc.ocr_conf * 100:.0f}% confidence" + (" (cached result)" if doc.ocr_cached else f" in {doc.ocr_seconds:.1f}s")) if doc.text_source == "ocr" else f", {len(doc.words)} words of text"))
            em.done("intake", "done", summ, dict(doc=info, notes=doc.notes, sha=doc.sha256[:16]))
            db.x("UPDATE runs SET file_hash=? WHERE id=?", (doc.sha256, run_id))

        if doc is None:
            for s, _ in STAGES[1:8]:
                em.skip(s, "Skipped: the file could not be read")
            return _finish(run_id, r, em, findings, None, None, dict(), dict(), dict(), None, None, t_run, mode, doc, dry_run, policy, as_of)

        # ------------------------------------------------------------- 2 classify
        cur_stage = "classify"
        em.start("classify")
        local = extract_local(doc)
        dtv = local["doc_type"]["value"]
        classify_checks = []
        short = None
        if dtv in ("statement", "credit_note"):
            label = "an account statement" if dtv == "statement" else "a credit note"
            f = F("NOT_AN_INVOICE", f"This document is {label}, not an invoice",
                  f"Detected '{local['doc_type']['evidence']}'. Nothing payable can be extracted from it, so no money moves.",
                  "fail", "REJECT" if dtv == "statement" else "ESCALATE", "ap" if dtv == "credit_note" else "vendor", stage="classify")
            classify_checks.append(f)
            findings.append(f)
            short = f
        else:
            classify_checks.append(F("DOC_TYPE", "Document is an invoice", f"Invoice markers found ({local['doc_type']['evidence']}).", "pass", stage="classify"))
            findings.append(classify_checks[-1])
        em.done("classify", "fail" if short else "done",
                ("Not an invoice: " + dtv.replace("_", " ")) if short else "Invoice",
                dict(doc_type=dtv, confidence=local["doc_type"]["confidence"], method="keyword rules before any AI call: no tokens are spent on non-invoices"),
                classify_checks)
        if short:
            for s, _ in STAGES[2:8]:
                em.skip(s, "Skipped: not an invoice")
            return _finish(run_id, r, em, findings, None, None, dict(), dict(), dict(), None, local, t_run, mode, doc, dry_run, policy, as_of)

        # ------------------------------------------------------------- 3 extract
        cur_stage = "extract"
        em.start("extract")
        ai_ex, ai_meta, ai_error = None, None, None
        want_ai = mode != "local" and extract_ai.available()
        if mode != "local" and not extract_ai.available():
            ai_error = "No Gemini API key configured"
        if want_ai:
            try:
                ai_ex, ai_meta = extract_ai.extract_with_gemini(doc, r["file_path"], on_event=lambda m: em.progress("extract", m))
            except extract_ai.AIError as e:
                ai_error = f"{e.kind}: {e.detail}"
                em.progress("extract", "AI unavailable, falling back to the local reader (PDF text + OCR + rules)")
        if ai_ex is not None:
            ex = ai_ex
            ex["flags"]["hidden_text"] = local["flags"]["hidden_text"]
            ex["flags"]["injection_like_text"] = local["flags"]["injection_like_text"] or ex["flags"].get("injection_like_text", False)
            ex["flags"]["payment_instruction_change"] = ex["flags"]["payment_instruction_change"] or local["flags"]["payment_instruction_change"]
            ex["anomalies"] = [a for a in local["anomalies"] if "Hidden text" in a or "changed payment" in a] + ex["anomalies"]
            ex["text_source_doc"] = doc.text_source
            engine = ai_meta["model"]
        else:
            ex = local
            engine = "local reader (PDF text layer + OCR + rules)"
        cross = _crosscheck(ai_ex, local) if ai_ex is not None else None
        n_found = sum(1 for k, f in ex["fields"].items() if f["value"] is not None)
        summ = f"{n_found} fields and {len(ex['line_items'])} line item{'s' if len(ex['line_items']) != 1 else ''} read by {engine}"
        no_key = ai_error == "No Gemini API key configured"
        if ai_error and mode != "local":
            summ += " (no AI key configured)" if no_key else f" (AI failed: {ai_error[:70]})"
        em.done("extract", "warn" if (ai_error and mode != "local" and not no_key) else "done", summ,
                dict(engine=engine, ai=ai_meta, ai_error=ai_error, fields=_fields_table(ex), line_items=ex["line_items"], crosscheck=cross,
                     local_fields=_fields_table(local) if ai_ex is not None else None, flags=ex["flags"], anomalies=ex["anomalies"],
                     text_source=doc.text_source))

        src = doc.text_source if doc.text_source == "ocr" else ("text-layer" if ai_ex is None else "text-layer")
        inv = rules.build_invoice(ex, src)
        inv["_engine"] = engine

        # ------------------------------------------------------------- 4 verify
        cur_stage = "verify"
        em.start("verify")
        vchecks = rules.check_quality(inv, ex, policy, as_of, doc.hidden_words)
        if cross:
            for row in cross:
                if row["agree"] is False:
                    sev_out = ("fail", "ESCALATE") if src == "text-layer" else ("warn", None)
                    vchecks.append(F("XCHECK_MISMATCH", f"Two readers disagree on the {row['field'].lower()}",
                                     f"AI read '{row['ai']}', the independent local reader got '{row['local']}'. "
                                     + ("Both read a clean text layer, so one of them is wrong: a person should confirm." if src == "text-layer"
                                        else "The local reader works from OCR, which is less reliable on scans, so this is noted, not blocking."),
                                     sev_out[0], sev_out[1], "ap" if sev_out[1] else None, expected=row["ai"], found=row["local"], stage="verify"))
            agree_n = sum(1 for r_ in cross if r_["agree"] is True)
            if not any(r_["agree"] is False for r_ in cross):
                vchecks.append(F("XCHECK_OK", "Two independent readers agree", f"AI and local reader agree on {agree_n} of {len(cross)} key fields (others not read by both).", "pass", stage="verify"))
        findings += vchecks
        trust_fields = [inv["conf"].get(k, 0) for k in rules.CRITICAL if inv["conf"].get(k, 0) > 0]
        trust = min(trust_fields) if trust_fields else 0.0
        summ = f"{sum(1 for c in vchecks if c['severity'] == 'pass')} checks passed" + (f", {sum(1 for c in vchecks if c['severity'] == 'fail')} failed" if any(c["severity"] == "fail" for c in vchecks) else "")
        em.done("verify", _status_of(vchecks), summ, dict(net=inv["net"], tax_inclusive=inv["tax_inclusive"], extraction_trust=round(trust, 3)), vchecks)

        # ------------------------------------------------------------- 5 vendor
        cur_stage = "vendor"
        em.start("vendor")
        rv = matching.resolve_vendor(inv, src)
        vendor = rv["vendor"]
        vch = matching.check_vendor(inv, rv, src)
        findings += vch
        em.done("vendor", _status_of(vch),
                (f"{vendor['legal_name']} ({vendor['vendor_id']}), {rv['score'] * 100:.0f}% match" if vendor else "Vendor not found in the master"),
                dict(resolved=vendor, score=round(rv["score"], 3), method=rv["method"], runner_up=rv["runner_up"],
                     invoice=dict(name=inv["vendor_name"], tax_id=inv["vendor_tax_id"], email=inv["vendor_email"], bank_account=inv["bank_account"])), vch)

        # ------------------------------------------------------------- 6 duplicates
        cur_stage = "duplicates"
        em.start("duplicates")
        dch, dinfo = matching.check_duplicates(inv, vendor, doc.sha256, policy, run_id=run_id, as_of=as_of)
        findings += dch
        em.done("duplicates", _status_of(dch),
                ("Duplicate found" if dinfo["matches"] else ("Resubmission of a held invoice" if dinfo["supersedes"] else "No duplicate")),
                dict(matches=dinfo["matches"], supersedes=dinfo["supersedes"],
                     fingerprint=dict(number=inv["number"], normalised=matching.invoice_number_core(inv["number"]), total=inv["total"], date=inv["date"], file=doc.sha256[:12])), dch)

        # ------------------------------------------------------------- 7 PO match
        cur_stage = "po_match"
        po_info = dict(method=None, po=None, candidates=[], lines=[])
        if vendor is None:
            em.skip("po_match", "Skipped: the vendor could not be identified, so there are no purchase orders to search")
        else:
            em.start("po_match")
            pch, po_info = matching.match_po(inv, vendor, policy)
            findings += pch
            po = po_info.get("po")
            summ = (f"{po['po_number']} ({po_info['method']})" + (f", ${po_info['remaining_before']:,.2f} open before this invoice" if po_info.get("remaining_before") is not None else "")) if po else "No PO matched"
            em.done("po_match", _status_of(pch), summ, dict(po=po, method=po_info["method"], printed=po_info["printed"], candidates=po_info["candidates"],
                                                         lines=po_info["lines"], remaining_before=po_info.get("remaining_before"),
                                                         remaining_after=po_info.get("remaining_after"), billed_to_date=po_info.get("billed_to_date"),
                                                         ordered_total=po_info.get("ordered_total"), confidence=po_info.get("confidence")), pch)

        # ------------------------------------------------------------- 8 policy
        cur_stage = "policy"
        em.start("policy")
        poch = rules.check_policy(inv, vendor, po_info.get("po"), policy, as_of)
        findings += poch
        due = rules.due_date_for(inv, vendor)
        inv["_due"] = due
        em.done("policy", _status_of(poch), f"{sum(1 for c in poch if c['severity'] == 'pass')} of {len(poch)} policy checks passed", dict(policy=policy, due_date=due), poch)

        mc = [rv["score"]] if vendor else [0.5]
        if po_info.get("po"):
            mc.append(po_info.get("confidence") or 0.9)
        trust_d = dict(extraction=trust or 0.5, match=min(mc))
        return _finish(run_id, r, em, findings, inv, vendor, po_info, dinfo, trust_d, ex, local, t_run, mode, doc, dry_run, policy, as_of, ai_meta=ai_meta, engine=engine, ai_error=ai_error)
    except Exception as e:  # last line of defence: never leave a run hanging
        traceback.print_exc()
        db.x("UPDATE runs SET status='error', error=?, finished_at=? WHERE id=?", (f"{cur_stage}: {e}"[:300], db.now_iso(), run_id))
        em._write(cur_stage, "fail", dict(title=LABELS.get(cur_stage, cur_stage), summary=f"Unexpected error: {e}"[:200], details={}, checks=[], ms=0))
        em._write("run", "error", dict(error=str(e)[:300], stage=cur_stage))
        return None


# =============================================================================
def _finish(run_id, r, em, findings, inv, vendor, po_info, dinfo, trust, ex, local, t_run, mode, doc, dry_run, policy, as_of,
            ai_meta=None, engine=None, ai_error=None):
    inv = inv or dict(number=None, date=None, total=None, currency=None, vendor_name=None, lines=[], net=None, tax=None, _due=None, flags={}, bank_account=None)
    trust = trust or dict(extraction=0.9, match=0.9)
    # ------------------------------------------------------------- 9 decide
    em.start("decide")
    d = dec.decide(findings, trust)
    decision = d["decision"]
    supersedes = dinfo.get("supersedes") if dinfo else None
    head = narrate.headline(decision, d, inv, vendor, po_info or {}, supersedes)
    expl = narrate.explanation(decision, d, findings, inv, vendor, po_info or {}, supersedes)
    nxt = narrate.next_action(decision, d, vendor, po_info or {}, inv)
    em.done("decide", {"APPROVE": "done", "HOLD": "warn", "ESCALATE": "warn", "REJECT": "fail"}[decision], head,
            dict(decision=decision, headline=head, risk=d["risk"], confidence=d["confidence"], route=d["route"],
                 checks_passed=d["checks_passed"], checks_total=d["checks_total"],
                 deciding=[dict(code=f["code"], title=f["title"]) for f in d["deciding"]]))

    # ------------------------------------------------------------- 10 output
    em.start("output")
    artifacts = []
    dup0 = (dinfo or {}).get("matches", [None])[0] if dinfo and dinfo.get("matches") else None
    if decision == "APPROVE":
        artifacts.append(dict(kind="erp_payload", to="ERP / payment run", subject="AP invoice posting",
                              body=json.dumps(narrate.erp_payload(run_id, inv, vendor, po_info, d, inv.get("_due"), supersedes), indent=2)))
    elif decision == "HOLD":
        artifacts.append(narrate.vendor_email_hold(run_id, inv, vendor, d, po_info or {}))
    elif decision == "ESCALATE":
        artifacts.append(narrate.internal_brief(run_id, inv, vendor, d, po_info or {}, decision))
    else:
        mail = narrate.vendor_email_reject(run_id, inv, vendor, d, dup0)
        artifacts.append(mail if mail else narrate.internal_brief(run_id, inv, vendor, d, po_info or {}, decision))
    polished = False
    if mode != "local" and artifacts and artifacts[0]["kind"] in ("vendor_email", "internal_brief") and extract_ai.available() and not ai_error:
        em.progress("output", "Asking the AI to polish the wording (numbers are locked: any change to a figure is rejected)")
        new = extract_ai.polish(artifacts[0]["kind"], artifacts[0]["body"])
        if new:
            artifacts[0]["body_original"] = artifacts[0]["body"]
            artifacts[0]["body"] = new
            polished = True
    ledger_note = ""
    if not dry_run:
        ledger_note = _record(run_id, r, decision, inv, vendor, po_info or {}, supersedes, doc)
    em.done("output", "done", {"APPROVE": "Posting payload ready, books updated", "HOLD": "Vendor e-mail drafted, hold recorded",
                                "ESCALATE": "Escalation brief drafted, payment blocked", "REJECT": "Rejection recorded, no payment"}[decision]
            + ("" if not dry_run else " (what-if run: books not touched)"),
            dict(artifacts=artifacts, ledger=ledger_note, ai_polished=polished))

    # ------------------------------------------------------------- persist
    dur = max(1, int((time.time() - t_run - em.slept) * 1000))
    result = dict(
        decision=decision, headline=head, explanation=expl, next_action=nxt, risk=d["risk"], confidence=d["confidence"], route=d["route"],
        route_label=narrate.ROUTE_LABEL.get(d["route"], d["route"]), checks_passed=d["checks_passed"], checks_total=d["checks_total"],
        findings=findings, deciding=[f["code"] for f in d["deciding"]], artifacts=artifacts,
        invoice={k: v for k, v in inv.items() if not k.startswith("_") and k not in ("conf",)}, invoice_conf=inv.get("conf", {}),
        vendor=vendor, po=dict(method=po_info.get("method"), po=po_info.get("po"), lines=po_info.get("lines"), remaining_before=po_info.get("remaining_before"),
                               remaining_after=po_info.get("remaining_after"), candidates=po_info.get("candidates")) if po_info else None,
        duplicates=dinfo, extraction=ex, local_extraction=local if (ex is not None and local is not ex) else None,
        doc=doc_to_public(doc) if doc else None, engine=engine or "n/a", ai_meta=ai_meta, ai_error=ai_error, mode=mode, due_date=inv.get("_due"),
        supersedes=supersedes, duration_ms=dur, dry_run=bool(dry_run))
    reason_codes = [f["code"] for f in d["deciding"]] or [f["code"] for f in d["warnings"]][:3]
    db.x("""UPDATE runs SET status='done', finished_at=?, decision=?, confidence=?, risk=?, vendor_id=?, vendor_name=?, invoice_number=?,
            invoice_date=?, currency=?, total=?, po_number=?, headline=?, reason_codes=?, result_json=?, duration_ms=?, engine_used=?,
            review_status=?, supersedes_run_id=? WHERE id=?""",
         (db.now_iso() if r.get("source") != "seed" else (r["created_at"] or db.now_iso()), decision, d["confidence"], d["risk"],
          vendor["vendor_id"] if vendor else None, (vendor or {}).get("legal_name") or inv.get("vendor_name"), inv.get("number"), inv.get("date"),
          inv.get("currency"), inv.get("total"), ((po_info or {}).get("po") or {}).get("po_number"), head,
          json.dumps(reason_codes), json.dumps(result, default=str), dur, engine or "n/a",
          "open" if decision in ("HOLD", "ESCALATE") else None, supersedes["run_id"] if supersedes and supersedes.get("run_id") else None, run_id))
    em._write("run", "complete", dict(decision=decision, headline=head, duration_ms=dur))
    return result


# =============================================================================
def _record(run_id, r, decision, inv, vendor, po_info, supersedes, doc):
    """Write the outcome into the ledger; approved invoices consume PO balance."""
    status = dict(APPROVE="approved", HOLD="held", ESCALATE="pending_review", REJECT="rejected")[decision]
    from .normalize import invoice_number_core, norm_invoice_number
    cur = db.x("""INSERT INTO ledger (vendor_id,invoice_number,norm_number,core_number,invoice_date,po_number,net,tax,total,currency,status,
                  file_hash,run_id,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (vendor["vendor_id"] if vendor else None, inv.get("number"), norm_invoice_number(inv.get("number")),
                invoice_number_core(inv.get("number")), inv.get("date"), (po_info.get("po") or {}).get("po_number"), inv.get("net"), inv.get("tax"),
                inv.get("total"), inv.get("currency"), status, doc.sha256 if doc else None, run_id, r.get("source") or "run", db.now_iso()))
    lid = cur.lastrowid
    note = f"Ledger: invoice recorded as '{status}'."
    if supersedes and supersedes.get("ledger_id"):
        db.x("UPDATE ledger SET status='superseded', note=? WHERE id=?", (f"superseded by RUN-{run_id:04d}", supersedes["ledger_id"]))
        if supersedes.get("run_id"):
            db.x("UPDATE runs SET review_status='superseded' WHERE id=?", (supersedes["run_id"],))
        note += f" Held run RUN-{supersedes['run_id']:04d} marked superseded."
    if decision == "APPROVE":
        consume(lid, run_id, po_info, inv)
        if po_info.get("remaining_after") is not None:
            note += f" PO balance now ${po_info['remaining_after']:,.2f}."
    return note


def consume(ledger_id, run_id, po_info, inv):
    po = po_info.get("po")
    if not po:
        return
    wrote = False
    for l in po_info.get("lines") or []:
        if l.get("kind") == "qty":
            db.x("INSERT INTO consumption (po_number,line_no,qty,amount,ledger_id,run_id) VALUES (?,?,?,?,?,?)",
                 (po["po_number"], l["po_line"], l["billed_qty"], round(l["billed_qty"] * l["unit_price"], 2), ledger_id, run_id))
        else:
            db.x("INSERT INTO consumption (po_number,line_no,qty,amount,ledger_id,run_id) VALUES (?,?,?,?,?,?)",
                 (po["po_number"], l["po_line"], 0, l["billed"], ledger_id, run_id))
        wrote = True
    if not wrote and inv.get("net") is not None:     # bundled, header-level: book against the line with the most balance
        st = matching.po_state(po["po_number"])
        tgt = max(st, key=lambda l: l["remaining_amt"]) if st else None
        if tgt:
            db.x("INSERT INTO consumption (po_number,line_no,qty,amount,ledger_id,run_id) VALUES (?,?,?,?,?,?)",
                 (po["po_number"], tgt["line_no"], 0, inv["net"], ledger_id, run_id))


# =============================================================================
def create_run(path, filename, source="upload", sample_id=None, mode="auto", created_at=None, as_of=None, dry_run=False, whatif_of=None):
    """Register a run and keep a private copy of the original file next to its page images."""
    import shutil
    cur = db.x("INSERT INTO runs (created_at,filename,file_path,size,source,sample_id,engine_mode,status,dry_run,as_of,whatif_of) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (created_at or db.now_iso(), filename, path, os.path.getsize(path), source, sample_id, mode, "running", 1 if dry_run else 0, as_of, whatif_of))
    rid_ = cur.lastrowid
    folder = os.path.join(config.DATA_DIR, "runs", str(rid_))
    os.makedirs(folder, exist_ok=True)
    ext = os.path.splitext(filename)[1].lower() or os.path.splitext(path)[1].lower() or ".pdf"
    dest = os.path.join(folder, "original" + ext)
    shutil.copyfile(path, dest)
    db.x("UPDATE runs SET file_path=? WHERE id=?", (dest, rid_))
    return rid_

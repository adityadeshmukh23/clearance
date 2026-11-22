/* The case view: live stage rail + document with evidence boxes + decision, explanation and drafts.
   Works from the stored event log, so a live run, a replay and a run opened from the ledger all use one code path. */
const Case = (() => {
  const STAGES = ["intake", "classify", "extract", "verify", "vendor", "duplicates", "po_match", "policy", "decide", "output"];
  const TITLES = { intake: "Receive and read the document", classify: "Is this an invoice?", extract: "Extract the data", verify: "Verify the numbers",
    vendor: "Verify vendor and bank details", duplicates: "Check for duplicates", po_match: "Match to a purchase order", policy: "Apply payment policy",
    decide: "Decide", output: "Draft the output and update the books" };
  const ROUTES = { vendor: "the vendor", ap: "an AP specialist", buyer: "the PO buyer", treasury: "Treasury", receiving: "Receiving and the buyer", approver: "the budget owner",
    security: "the AP lead and Security", compliance: "Compliance", procurement: "Procurement", payment_run: "the payment run" };
  const MONEY = new Set(["subtotal", "tax_amount", "shipping", "discount", "total"]);
  const DATES = new Set(["invoice_date", "due_date"]);
  let S = null;

  function evidenceKeys(code) {
    const c = code || "";
    if (/INVOICE_NUMBER|DUPLICATE|RESUBMISSION/.test(c)) return ["invoice_number"];
    if (/^(ARITH|TOTAL)/.test(c)) return ["subtotal", "tax_amount", "total"];
    if (/^TAX_/.test(c)) return ["tax_amount", "tax_rate"];
    if (/BANK/.test(c)) return ["bank_account", "bank_routing", "bank_name"];
    if (/EMAIL/.test(c)) return ["vendor_email"];
    if (/TAXID/.test(c)) return ["vendor_tax_id"];
    if (/^VENDOR/.test(c)) return ["vendor_name", "vendor_tax_id"];
    if (/DATE|^DUE_/.test(c)) return ["invoice_date", "due_date"];
    if (/^PO_(MISSING|NOT_FOUND|REF|INFER|AMB|FOUND|VENDOR)/.test(c)) return ["po_reference"];
    if (/CURRENCY/.test(c)) return ["currency"];
    if (/MISSING_TOTAL|OVER_LIMIT|PO_OVERBILLED/.test(c)) return ["total"];
    if (/PRICE|LINE_|QTY/.test(c)) return ["lines"];
    if (/TERMS/.test(c)) return ["payment_terms"];
    return [];
  }

  function close() {
    if (!S) return;
    S.dead = true;
    if (S.es) S.es.close();
    clearTimeout(S.poll); (S.timers || []).forEach(clearTimeout);
    S = null;
  }

  async function open(runId) {
    close();
    const view = $("#view"); view.innerHTML = "";
    S = { id: runId, dead: false, seen: new Set(), stage: {}, events: [], result: null, run: null, decision: null, tab: "result", sel: null,
      boxes: {}, pageEls: [], stamped: false, timers: [], instant: false, es: null };
    const mine = S;
    let run;
    try { run = await api("/api/runs/" + runId); } catch (e) { view.append(h("div", { class: "empty" }, "This run could not be opened. " + e.message, " ", h("a", { href: "#/ledger" }, "Back to the ledger"))); return; }
    if (S !== mine) return;
    S.run = run;
    build();
    if (run.status === "running") connect();
    else { S.instant = true; ingest(run.events, true); finish(run); }
  }

  /* ------------------------------------------------ structure */
  function build() {
    const r = S.run, view = $("#view");
    S.head = h("div", { class: "case-head" });
    S.banner = h("div");
    S.stagesEl = h("div", { class: "sheet stages", role: "list", "aria-label": "Pipeline stages" });
    S.docEl = h("div", { class: "docbox" });
    S.resultEl = h("div", { class: "sheet result" });
    view.append(S.head, S.banner, h("div", { class: "case" }, S.stagesEl, S.docEl, S.resultEl));
    STAGES.forEach((id) => {
      const el = h("button", { class: "stage pending", role: "listitem", "data-id": id, onclick: () => selectStage(id) },
        h("span", { class: "g" }), h("span", {}, h("span", { class: "n" }, TITLES[id]), h("div", { class: "s" })));
      S.stage[id] = { status: "pending", el };
      S.stagesEl.append(el);
    });
    S.docEl.append(h("div", { class: "docbar" }, h("span", { id: "docTitle" }, "Document")), h("div", { class: "docscroll", id: "docScroll" }, h("div", { class: "empty-doc" }, "Opening the file…")));
    paintHead(); paintResultShell();
  }

  function paintHead() {
    const r = S.run, res = S.result;
    const src = r.dry_run ? "What-if" : r.source === "seed" ? "Seeded history" : r.source === "sample" ? "Sample file" : "Uploaded";
    S.head.innerHTML = "";
    S.head.append(...[
      h("a", { class: "btn secondary small", href: "#/desk" }, "New invoice"),
      h("span", { class: "code" }, runCode(r.id)),
      h("span", { class: "file", title: r.filename }, r.filename),
      h("span", { class: "tag" }, src),
      res ? h("span", { class: "tag", title: "Which reader extracted the data" }, res.engine || r.engine_used) : null,
      h("span", { class: "grow" }),
      r.has_file ? h("a", { class: "btn link", href: `/api/runs/${r.id}/file`, target: "_blank", rel: "noopener" }, "Open original file") : null,
      h("button", { class: "btn secondary small", id: "replayBtn", onclick: replay, disabled: !S.result }, "Replay")].filter(Boolean));
    S.banner.innerHTML = "";
    if (r.dry_run) S.banner.append(h("div", { class: "banner" }, "What-if run: it uses today's policy settings and does not touch the books or the review queue. ",
      r.whatif_of ? h("a", { href: "#/run/" + r.whatif_of }, "Compare with " + runCode(r.whatif_of)) : null));
    if (res && res.ai_error && res.mode !== "local") {
      const nokey = /No Gemini API key/.test(res.ai_error);
      S.banner.append(h("div", { class: "banner" + (nokey ? "" : " warn") }, nokey
        ? "No AI key is configured, so this run used the local reader only (PDF text, OCR and rules). With a key, an AI reader runs first and the local reader cross-checks it."
        : "The AI reader failed for this run (" + res.ai_error + "). The local reader did the extraction, so the result is still valid but was not cross-checked by a second reader."));
    }
  }

  /* ------------------------------------------------ events */
  function connect() {
    const mine = S;
    const es = new EventSource(`/api/runs/${S.id}/events`);
    S.es = es;
    es.addEventListener("stage", (m) => { if (S !== mine) return; try { ingest([JSON.parse(m.data)]); } catch (e) { console.error(e); } });
    const done = () => { if (S !== mine) return; es.close(); refresh(); };
    es.addEventListener("end", done);
    es.onerror = () => { if (S !== mine) return; es.close(); mine.poll = setTimeout(refresh, 600); };
  }

  async function refresh() {
    if (!S) return; const mine = S;
    try {
      const run = await api("/api/runs/" + S.id);
      if (S !== mine) return;
      ingest(run.events);
      if (run.status === "running") S.poll = setTimeout(refresh, 700); else finish(run);
    } catch (e) { S && (S.poll = setTimeout(refresh, 1500)); }
  }

  function ingest(events, instant) {
    for (const ev of events) {
      if (S.seen.has(ev.seq)) continue;
      S.seen.add(ev.seq); S.events.push(ev);
      apply(ev, instant || S.instant);
    }
    paintTab();
  }

  function apply(ev, instant) {
    const p = ev.payload || {};
    if (ev.stage === "run") return;
    const st = S.stage[ev.stage]; if (!st) return;
    if (ev.status === "progress") {                       // a live note from a running stage (model fallback, retries...), not a result
      (st.log = st.log || []).push(p.message); st.note = p.message; paintStage(ev.stage); return;
    }
    st.status = ev.status;
    if (ev.status !== "running") { st.payload = p; st.ms = p.ms; st.note = null; }
    paintStage(ev.stage);
    if (!S.decision) { const b = $(".working b", S.resultEl); const cur = STAGES.find((id) => S.stage[id].status === "running"); if (b && cur) b.textContent = TITLES[cur]; }
    if (ev.stage === "intake" && ev.status !== "running" && p.details) setupPages(p.details.doc);
    if (ev.stage === "extract") { const sc = $(".page", S.docEl); if (sc) sc.classList.toggle("scanning", ev.status === "running"); }
    if (ev.stage === "extract" && ev.status !== "running" && p.details) drawBoxes(p.details, instant);
    if (ev.stage === "decide" && ev.status !== "running" && p.details) {
      S.decision = p.details; markEvidence(); paintResultShell();
      stamp(p.details.decision, instant);
    }
  }

  function finish(run) {
    if (!S) return;
    S.run = Object.assign(S.run, run); S.result = run.result; S.instant = S.instant;
    if (run.result && !S.decision) { S.decision = { decision: run.result.decision, headline: run.result.headline, risk: run.result.risk, confidence: run.result.confidence, route: run.result.route, checks_passed: run.result.checks_passed, checks_total: run.result.checks_total }; stamp(run.decision, true); }
    if (run.status === "error" && !run.result) S.error = run.error || "The run failed.";
    STAGES.forEach((id) => { if (S.stage[id].status === "running") { S.stage[id].status = "fail"; paintStage(id); } });
    markEvidence(); paintHead(); paintResultShell(); paintTab();
    const sc = $(".page", S.docEl); if (sc) sc.classList.remove("scanning");
  }

  async function replay() {
    if (!S || !S.run) return;
    const events = S.events.slice(), run = S.run, result = S.result;
    S.timers.forEach(clearTimeout); S.timers = [];
    STAGES.forEach((id) => { S.stage[id].status = "pending"; S.stage[id].payload = null; S.stage[id].ms = null; paintStage(id); });
    S.decision = null; S.result = null; S.seen = new Set(); S.events = []; S.stamped = false; S.sel = null; S.tab = "result"; S.boxes = {};
    $$(".stamp, .ev", S.docEl).forEach((e) => e.remove());
    paintResultShell(); paintTab();
    $("#replayBtn").disabled = true;
    let t = 0; const mine = S;
    events.filter((e) => e.stage !== "run").forEach((ev) => {
      t += ev.status === "running" ? 120 : 520;
      S.timers.push(setTimeout(() => { if (S === mine) { ingest([ev], false); } }, t));
    });
    S.timers.push(setTimeout(() => { if (S !== mine) return; S.result = result; S.run = run; markEvidence(); paintHead(); paintResultShell(); paintTab(); }, t + 300));
  }

  /* ------------------------------------------------ stage rail */
  function paintStage(id) {
    const st = S.stage[id], el = st.el, p = st.payload || {};
    el.className = "stage " + st.status + (S.sel === id ? " sel" : "");
    const g = $(".g", el);
    g.innerHTML = st.status === "done" ? icon("check") : st.status === "warn" ? icon("warn") : st.status === "fail" ? icon("cross") : st.status === "skipped" ? icon("minus") : "";
    const n = $(".n", el); n.firstChild && (n.firstChild.nodeValue = p.title || TITLES[id]);
    let msEl = $(".ms", n); if (st.ms != null && (st.status === "done" || st.status === "warn" || st.status === "fail")) { if (!msEl) { msEl = h("span", { class: "ms" }); n.append(msEl); } msEl.textContent = ms(st.ms); }
    else if (msEl) msEl.remove();
    $(".s", el).textContent = st.status === "pending" ? "" : st.status === "running" ? (st.note || "Working…") : (p.summary || "");
  }

  function selectStage(id) {
    if (!S.stage[id].payload) return;
    S.sel = S.sel === id ? null : id;
    STAGES.forEach(paintStage);
    S.tab = "checks"; paintResultShell(); paintTab();
  }

  /* ------------------------------------------------ document + evidence */
  function setupPages(doc) {
    const sc = $("#docScroll", S.docEl); sc.innerHTML = ""; S.pageEls = [];
    const pages = (doc && doc.pages) || [];
    if (!pages.length) { sc.append(h("div", { class: "empty-doc" }, "No preview is available for this file.")); return; }
    $("#docTitle").textContent = (doc.pages.length > 1 ? doc.pages.length + " pages" : "1 page") + " · " + (doc.text_source === "ocr" ? "scanned image, read by OCR" : doc.text_source === "text-layer" ? "digital PDF with a text layer" : "document");
    pages.forEach((pg, i) => {
      const el = h("div", { class: "page" + (i === 0 ? "" : " later"), style: { maxWidth: "720px", marginTop: i ? "12px" : "0" } },
        h("img", { src: `/api/runs/${S.id}/page/${i + 1}`, alt: `Page ${i + 1} of the invoice`, width: pg.w, height: pg.h }), h("div", { class: "scan-bar" }));
      sc.append(el); S.pageEls.push(el);
    });
    S.pageEls[0].classList.add("scanning");
    if (S.decision) stamp(S.decision.decision, true);
  }

  function addBox(key, label, b, instant, delay) {
    const pg = S.pageEls[b[0]] || S.pageEls[0]; if (!pg) return;
    const [, x0, y0, x1, y1] = b;
    const el = h("div", { class: "ev" + (key.startsWith("line:") ? " line" : ""), "data-y": y0 + "," + y1, style: { left: (x0 * 100) + "%", top: (y0 * 100) + "%", width: ((x1 - x0) * 100) + "%", height: ((y1 - y0) * 100) + "%" } }, h("i", {}, label));
    pg.append(el);
    (S.boxes[key] = S.boxes[key] || []).push(el);
    if (instant) el.classList.add("on"); else S.timers.push(setTimeout(() => el.classList.add("on"), delay));
  }

  function drawBoxes(d, instant) {
    let i = 0;
    (d.fields || []).forEach((f) => (f.boxes || []).forEach((b) => addBox(f.key, f.label, b, instant, 120 + (i++) * 55)));
    (d.line_items || []).forEach((li, n) => (li.boxes || []).forEach((b) => { addBox("line:" + n, "Line " + (n + 1), b, instant, 120 + (i++) * 55); (S.boxes.lines = S.boxes.lines || []).push(S.boxes["line:" + n].slice(-1)[0]); }));
  }

  function markEvidence() {
    if (!S.result) return;
    $$(".ev.bad", S.docEl).forEach((e) => e.classList.remove("bad"));
    S.result.findings.filter((f) => f.severity === "fail").forEach((f) => evidenceKeys(f.code).forEach((k) => (S.boxes[k] || []).forEach((e) => e.classList.add("bad"))));
  }
  function hot(keys, on) {
    Object.values(S.boxes).flat().forEach((e) => e && e.classList.remove("hot"));
    if (on) keys.forEach((k) => (S.boxes[k] || []).forEach((e) => { e.classList.add("hot"); e.classList.add("on"); }));
  }

  function stamp(decision, instant) {
    if (S.stamped || !DEC[decision] || !S.pageEls[0]) { if (!S.pageEls[0]) S.pendingStamp = decision; return; }
    S.stamped = true;
    const sc = $(".page", S.docEl); sc && sc.classList.remove("scanning");
    const r = S.run, when = fmtDate((r.finished_at || r.created_at || "").slice(0, 10) || undefined);
    const el = h("div", { class: "stamp " + decision + (instant ? "" : " land"), role: "img", "aria-label": DEC[decision].label }, DEC[decision].stamp, h("small", {}, runCode(r.id) + " · " + when));
    S.pageEls[0].append(el);
    const spot = emptyBand(); if (spot != null) el.style.top = `calc(${(spot * 100).toFixed(1)}% - 34px)`;
  }

  /* the stamp goes where it covers the least: the widest vertical gap between evidence boxes on page 1 */
  function emptyBand() {
    const ys = $$(".ev", S.pageEls[0]).map((e) => e.dataset.y.split(",").map(Number)).sort((a, b) => a[0] - b[0]);
    if (!ys.length) return null;
    let best = null, cur = 0.1;
    for (const [a, b] of ys) { if (a - cur > (best ? best.gap : 0)) best = { gap: a - cur, mid: (a + cur) / 2 }; cur = Math.max(cur, b); }
    if (0.9 - cur > (best ? best.gap : 0)) best = { gap: 0.9 - cur, mid: (0.9 + cur) / 2 };
    return best && best.gap >= 0.09 ? best.mid : null;
  }

  /* ------------------------------------------------ result panel */
  function paintResultShell() {
    const el = S.resultEl, d = S.decision, res = S.result;
    el.innerHTML = "";
    if (S.error) { el.append(h("div", { class: "working" }, "This run failed: " + S.error)); return; }
    if (d) {
      const route = (res && res.route_label) || ROUTES[d.route] || d.route;
      el.append(h("div", { class: "verdict " + d.decision },
        pill(d.decision), h("h2", {}, d.headline),
        h("div", { class: "facts" },
          h("span", {}, "Routed to ", h("b", {}, route)),
          h("span", { title: "Share of checks passed and how sure the readers were" }, "Confidence ", h("b", {}, pct(d.confidence || 0))),
          h("span", { title: "Weighted score of the problems found, 0 to 100" }, "Risk ", h("b", {}, String(d.risk ?? 0))),
          h("span", {}, h("b", {}, `${d.checks_passed}/${d.checks_total}`), " checks passed"),
          S.run.duration_ms ? h("span", {}, "Took ", h("b", {}, ms(S.run.duration_ms))) : null)));
    } else {
      const cur = STAGES.find((id) => S.stage[id].status === "running");
      el.append(h("div", { class: "working" }, h("span", { class: "spin" }), h("div", {}, h("b", {}, cur ? TITLES[cur] : "Starting"), h("div", { class: "muted" }, "Each stage reports back as soon as it finishes."))));
    }
    S.tabsEl = h("div", { class: "tabs", role: "tablist" }, [["result", "Result"], ["data", "Data"], ["checks", "Checks"], ["output", "Drafts"], ["raw", "Audit and raw"]].map(([k, l]) =>
      h("button", { role: "tab", "aria-selected": S.tab === k ? "true" : "false", onclick: () => { S.tab = k; if (k !== "checks") S.sel = null, STAGES.forEach(paintStage); paintResultShell(); paintTab(); } }, l)));
    S.paneEl = h("div", { class: "tabpane", role: "tabpanel" });
    el.append(S.tabsEl, S.paneEl);
  }

  function paintTab() {
    if (!S || !S.paneEl) return;
    const pane = S.paneEl; const top = pane.scrollTop; pane.innerHTML = "";
    ({ result: tabResult, data: tabData, checks: tabChecks, output: tabOutput, raw: tabRaw }[S.tab] || tabResult)(pane);
    pane.scrollTop = top;
  }

  function tabResult(pane) {
    const res = S.result, r = S.run;
    if (!res) { pane.append(h("p", { class: "muted" }, "The explanation, next step and drafts appear when the run finishes. Click any stage on the left to see what it checked.")); return; }
    pane.append(h("ul", { class: "points" }, res.explanation.map((x) => h("li", { class: x.tone || "info", html: icon(x.tone === "good" ? "check" : x.tone === "bad" ? "cross" : x.tone === "warn" ? "warn" : "info") + `<span>${esc(x.text)}</span>` }))));
    pane.append(h("div", { class: "next" }, h("b", {}, "What happens next"), res.next_action));
    if (res.supersedes) pane.append(h("div", { class: "next" }, h("b", {}, "Replaces an earlier run"), "This invoice resolves ", h("a", { href: "#/run/" + (res.supersedes.run_id || "") }, res.supersedes.run_id ? runCode(res.supersedes.run_id) : "an earlier held invoice"), ", which is now closed."));
    const act = h("div", { class: "actions" });
    if (r.review_status === "open" && !r.dry_run) {
      const note = h("textarea", { rows: "2", placeholder: "Reason (required): what did you check, who did you speak to?", "aria-label": "Reason for the decision" });
      const send = async (action) => {
        if (note.value.trim().length < 3) { toast("Write a short reason first. Every manual decision is logged with it.", "bad"); note.focus(); return; }
        try { await api(`/api/runs/${r.id}/review`, { method: "POST", json: { action, note: note.value.trim() } }); toast(action === "approve" ? "Approved and logged" : "Rejected and logged"); App.refreshBadge(); open(r.id); }
        catch (e) { toast(e.message, "bad"); }
      };
      act.append(h("div", { class: "field" }, h("label", {}, "This run is waiting for a person"), note, h("div", { class: "help" }, "A manual decision is recorded in the audit trail with your reason. Approving also reserves the PO balance.")),
        h("div", { class: "btns" }, h("button", { class: "btn good small", onclick: () => send("approve") }, "Approve anyway"), h("button", { class: "btn danger small", onclick: () => send("reject") }, "Reject invoice")));
    } else if (r.review_status === "approved" || r.review_status === "rejected") {
      act.append(h("div", { class: "next", style: { margin: 0 } }, h("b", {}, r.review_action || "Reviewed"), (r.audit || []).slice(-1).map((a) => a.note)));
    }
    act.append(h("div", { class: "btns" }, r.dry_run ? null : h("button", { class: "btn secondary small", onclick: whatIf, title: "Run this file again with the current policy settings. Nothing is saved." }, "Re-run with current policy"),
      h("a", { class: "btn secondary small", href: "#/reference" }, "Change policy")));
    pane.append(act);
  }

  async function whatIf() {
    try { const x = await api(`/api/runs/${S.id}/whatif`, { method: "POST", json: { mode: S.run.engine_mode || "auto", pace: App.state.pace } }); location.hash = "#/run/" + x.run_id; }
    catch (e) { toast(e.message, "bad"); }
  }

  function fmtField(k, v) {
    if (v == null || v === "") return null;
    if (MONEY.has(k)) return usd(v); if (DATES.has(k)) return fmtDate(v); if (k === "tax_rate") return v + "%";
    return String(v);
  }
  function tabData(pane) {
    const ex = S.stage.extract.payload && S.stage.extract.payload.details;
    if (!ex) { pane.append(h("p", { class: "muted" }, "Fields appear here as soon as the document has been read.")); return; }
    const res = S.result, sec = () => h("div");
    const cross = sec(), po = sec(), ven = sec(), dupe = sec(), fields = sec(), items = sec();

    /* two readers compared (AI runs only) */
    if (ex.crosscheck && ex.crosscheck.length) {
      const bad = ex.crosscheck.filter((x) => !x.agree).length;
      cross.append(h("h3", {}, "Two readers compared"), h("div", { class: "sub muted", style: { margin: "-4px 0 8px" } }, bad ? "The AI reader and the local reader disagree on " + bad + " field" + (bad > 1 ? "s" : "") + ". The run is not trusted until a person looks." : "The AI reader and the independent local reader read the same values."),
        h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["Field", "AI reader", "Local reader", ""].map((t) => h("th", {}, t)))),
          h("tbody", {}, ex.crosscheck.map((x) => h("tr", {}, h("td", {}, x.field), h("td", {}, x.ai == null ? "-" : String(x.ai)), h("td", {}, x.local == null ? "-" : String(x.local)),
            h("td", { html: x.agree ? icon("check") : icon("warn"), style: { color: x.agree ? "var(--approve)" : "var(--reject)", width: "28px" }, title: x.agree ? "Agree" : "Disagree" }))))));
    }

    /* purchase order: how it was matched and what is left */
    const pm = S.stage.po_match.payload && S.stage.po_match.payload.details;
    if (pm && pm.po) {
      const bill = pm.billed_to_date || 0, inv = Math.max(0, (pm.remaining_before || 0) - (pm.remaining_after || 0)), ord = pm.ordered_total || 1, over = (pm.remaining_after || 0) < 0;
      const scale = Math.max(ord, bill + inv);
      po.append(h("h3", {}, `Purchase order ${pm.po.po_number}`), h("div", { class: "kv" },
        h("div", {}, "Matched by"), h("div", {}, ({ explicit: "PO number printed on the invoice", fuzzy: "Close match to the printed number", inferred: "Inferred from vendor, items and prices" }[pm.method] || pm.method) + (pm.confidence ? ` (${pct(pm.confidence)} sure)` : "")),
        h("div", {}, "Buyer and cost centre"), h("div", {}, `${pm.po.buyer || "-"}, ${pm.po.cost_center || "-"}`),
        h("div", {}, "PO value"), h("div", { class: "num" }, usd(pm.ordered_total)),
        h("div", {}, "Billed before this invoice"), h("div", { class: "num" }, usd(bill)),
        h("div", {}, "This invoice (net)"), h("div", { class: "num" }, usd(inv)),
        h("div", {}, "Left after this invoice"), h("div", { class: "num" }, over ? h("b", { style: { color: "var(--reject-ink)" } }, usd(pm.remaining_after) + " (over budget)") : usd(pm.remaining_after))),
        h("div", { class: "meter", style: { marginTop: "10px" }, role: "img", "aria-label": `PO balance: ${usd(bill)} billed, this invoice ${usd(inv)}, PO value ${usd(ord)}` }, h("i", { style: { width: (bill / scale * 100) + "%" } }), h("i", { class: over ? "d" : "w", style: { width: (inv / scale * 100) + "%", marginLeft: "2px" } })),
        h("div", { class: "legend", style: { marginTop: "6px", fontSize: "12.5px" } }, h("span", {}, h("i", { style: { background: "#2f3d54" } }), "Already billed"), h("span", {}, h("i", { style: { background: over ? "var(--reject)" : "var(--escalate)" } }), "This invoice")));
      if ((pm.lines || []).length) po.append(h("table", { class: "tbl", style: { marginTop: "12px" } }, h("thead", {}, h("tr", {}, ["Line", "Billed", "Received", "Price vs PO"].map((t, i) => h("th", { class: i ? "r" : "" }, t)))),
        h("tbody", {}, pm.lines.map((l) => h("tr", {}, h("td", {}, h("div", {}, l.sku || l.po_description || "-"), h("div", { class: "sub" }, l.kind === "lump" ? "lump sum" : (l.po_description || ""))),
          h("td", { class: "r" }, l.billed_qty ?? "-"), h("td", { class: "r" }, l.received_qty ?? "-"),
          h("td", { class: "r" }, l.po_unit_price == null ? "-" : usd(l.unit_price) + " vs " + usd(l.po_unit_price) + (l.price_var_pct ? ` (${l.price_var_pct > 0 ? "+" : ""}${l.price_var_pct.toFixed(1)}%)` : "")))))));
    }

    /* vendor master */
    if (res && res.vendor) {
      const v = res.vendor, i = res.invoice || {};
      const rows = [["Legal name", i.vendor_name, v.legal_name], ["Tax ID", i.vendor_tax_id, v.tax_id], ["Bank account", i.bank_account, v.bank_account], ["Bank", i.bank_name, v.bank_name], ["Payment terms", i.terms, v.payment_terms]];
      ven.append(h("h3", {}, `Compared with the vendor master (${v.vendor_id})`),
        h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "On the invoice"), h("th", {}, "On file"), h("th", {}, ""))),
          h("tbody", {}, rows.map(([l, a, b]) => { const same = !a || !b || String(a).replace(/\W/g, "").toLowerCase() === String(b).replace(/\W/g, "").toLowerCase();
            return h("tr", {}, h("td", {}, l), h("td", {}, a || h("span", { class: "muted" }, "not on invoice")), h("td", {}, b || "-"), h("td", { html: same ? icon("check") : icon("warn"), style: { color: same ? "var(--approve)" : "var(--reject)", width: "28px" }, title: same ? "Matches" : "Differs" })); }))));
    }

    /* duplicates */
    const dup = S.stage.duplicates.payload && S.stage.duplicates.payload.details;
    if (dup && dup.matches && dup.matches.length) {
      dupe.append(h("h3", {}, "Possible duplicates on file"),
        h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["Invoice", "Date", "Amount", "Why it matched"].map((t, i) => h("th", { class: i === 2 ? "r" : "" }, t)))),
          h("tbody", {}, dup.matches.map((m) => h("tr", { class: m.run_id ? "click" : "", onclick: () => m.run_id && (location.hash = "#/run/" + m.run_id) }, h("td", {}, m.invoice_number, h("div", { class: "sub" }, m.run_id ? runCode(m.run_id) : "")), h("td", {}, fmtDate(m.invoice_date)), h("td", { class: "r" }, usd(m.total)), h("td", {}, m.kind, h("div", { class: "sub" }, m.status + (m.paid_date ? ", paid " + fmtDate(m.paid_date) : ""))))))));
    }

    /* what was read, with hover-to-locate */
    fields.append(h("h3", {}, "What was read from the document"));
    (ex.fields || []).forEach((f) => {
      const v = fmtField(f.key, f.value), row = h("div", { class: "field-row", title: f.evidence ? "Read from: " + f.evidence : "" },
        h("span", { class: "k" }, f.label), h("span", { class: "v" + (v == null ? " missing" : "") }, v == null ? "Not found" : v), confBars(f.confidence || 0));
      row.addEventListener("mouseenter", () => hot([f.key], true)); row.addEventListener("mouseleave", () => hot([], false));
      fields.append(row);
    });
    const li = ex.line_items || [];
    if (li.length) {
      items.append(h("h3", {}, `Line items (${li.length})`),
        h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["Item", "Qty", "Unit price", "Amount"].map((t, i) => h("th", { class: i ? "r" : "" }, t)))),
          h("tbody", {}, li.map((x, n) => { const tr = h("tr", {}, h("td", {}, h("div", {}, x.description || "-"), h("div", { class: "sub" }, x.sku || "")), h("td", { class: "r" }, x.qty ?? "-"), h("td", { class: "r" }, x.unit_price == null ? "-" : usd(x.unit_price)), h("td", { class: "r" }, x.amount == null ? "-" : usd(x.amount)));
            tr.addEventListener("mouseenter", () => hot(["line:" + n], true)); tr.addEventListener("mouseleave", () => hot([], false)); return tr; }))));
    }
    pane.append(cross, po, ven, dupe, fields, items);
  }

  function tabChecks(pane) {
    if (S.sel) pane.append(h("div", { class: "filter-note" }, h("span", {}, "Showing the checks from ", h("b", {}, TITLES[S.sel])), h("button", { class: "btn link", onclick: () => { S.sel = null; STAGES.forEach(paintStage); paintTab(); } }, "Show all stages")));
    let any = false;
    STAGES.filter((id) => !S.sel || id === S.sel).forEach((id) => {
      const p = S.stage[id].payload; if (!p) return;
      const cs = p.checks || []; if (!cs.length && S.sel) pane.append(h("p", { class: "muted" }, p.summary || "No separate checks in this stage."));
      if (!cs.length) return; any = true;
      const fails = cs.filter((c) => c.severity === "fail" || c.severity === "warn").length;
      pane.append(h("h3", { class: "group-h" }, p.title || TITLES[id], h("span", { class: "sm" }, fails ? `${fails} to look at, ${cs.length} checks` : `${cs.length} check${cs.length === 1 ? "" : "s"} passed`)));
      pane.append(h("div", { class: "checks" }, cs.map((c) => {
        const el = h("div", { class: "check " + c.severity, html: icon(sevIcon(c.severity)) }, h("div", {}, h("div", { class: "t" }, c.title), h("div", { class: "d" }, c.detail),
          c.expected != null || c.found != null ? h("div", { class: "cmp" }, c.expected != null ? h("div", {}, h("span", {}, "Expected "), h("b", {}, c.expected)) : null, c.found != null ? h("div", {}, h("span", {}, "Found "), h("b", {}, c.found)) : null) : null));
        const keys = evidenceKeys(c.code); if (keys.length) { el.addEventListener("mouseenter", () => hot(keys, true)); el.addEventListener("mouseleave", () => hot([], false)); }
        return el;
      })));
    });
    if (!any && !S.sel) pane.append(h("p", { class: "muted" }, "Checks appear here as each stage completes."));
  }

  function tabOutput(pane) {
    const res = S.result;
    const out = S.stage.output.payload && S.stage.output.payload.details;
    if (!res) { pane.append(h("p", { class: "muted" }, "Drafts are written after the decision: a vendor e-mail, an escalation brief or the ERP posting.")); return; }
    const NAMES = { vendor_email: "E-mail to the vendor (draft, not sent)", internal_brief: "Escalation brief (draft, not sent)", erp_payload: "ERP posting payload (not posted)" };
    (res.artifacts || []).forEach((a) => {
      pane.append(h("div", { class: "doc-out", style: { marginBottom: "14px" } },
        h("header", {}, h("div", {}, h("b", {}, NAMES[a.kind] || a.kind), h("div", { class: "sub muted" }, "To: " + a.to + (a.subject ? "  |  " + a.subject : ""))),
          h("button", { class: "btn secondary small", onclick: () => copyText(a.body), "aria-label": "Copy to clipboard" }, "Copy")),
        h("pre", { class: a.kind === "erp_payload" ? "json" : "" }, a.body)));
    });
    if (!(res.artifacts || []).length) pane.append(h("p", { class: "muted" }, "Nothing was drafted for this run."));
    if (out && out.ledger) pane.append(h("div", { class: "next" }, h("b", {}, "Books"), out.ledger));
    if (out && out.ai_polished) pane.append(h("p", { class: "muted", style: { marginTop: "10px" } }, "The wording of the e-mail was polished by AI. Every number in it was checked against the source data first."));
  }

  function tabRaw(pane) {
    const r = S.run, res = S.result;
    pane.append(h("h3", {}, "Audit trail"));
    const rows = [{ ts: r.created_at, actor: "Clearance", note: `${runCode(r.id)} received (${r.filename})` }];
    if (r.status === "done") rows.push({ ts: r.finished_at, actor: "Clearance", note: `${DEC[r.decision] ? DEC[r.decision].label : r.decision}: ${r.headline}` });
    (r.audit || []).forEach((a) => rows.push({ ts: a.ts, actor: a.actor, note: `${a.action === "approve" ? "Approved" : "Rejected"} manually. ${a.note}` }));
    pane.append(h("ul", { class: "audit" }, rows.map((a) => h("li", {}, h("div", { class: "t" }, fmtDT(a.ts) + " by " + a.actor), h("div", {}, a.note)))));
    const notes = STAGES.flatMap((id) => (S.stage[id].log || []).map((m) => [id, m]));
    if (notes.length) pane.append(h("h3", {}, "Notes from the stages"), h("ul", { class: "audit" }, notes.map(([id, m]) => h("li", {}, h("div", { class: "t" }, TITLES[id]), h("div", {}, m)))));
    if (res) {
      pane.append(h("h3", {}, "Reader"), h("div", { class: "kv" }, h("div", {}, "Engine"), h("div", {}, res.engine || "-"), h("div", {}, "Mode"), h("div", {}, res.mode || "-"),
        h("div", {}, "File"), h("div", {}, `${res.doc.name} (${res.doc.pages.length} page${res.doc.pages.length > 1 ? "s" : ""}, ${res.doc.text_source}${res.doc.ocr_seconds ? ", OCR " + res.doc.ocr_seconds + " s" : ""})`),
        h("div", {}, "SHA-256"), h("div", { style: { overflowWrap: "anywhere", fontSize: "12.5px" } }, res.doc.sha256)));
      const pre = h("pre", { class: "json" }, JSON.stringify(res, null, 2));
      pane.append(h("h3", {}, "Full result as JSON"), h("div", { class: "doc-out" }, h("header", {}, h("span", {}, "What the pipeline produced for this run"), h("button", { class: "btn secondary small", onclick: () => copyText(pre.textContent) }, "Copy")), pre));
    }
  }

  return { open, close };
})();

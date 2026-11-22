/* Ledger: dashboard of what has been processed, the review queue, and the full run history. */
const Ledger = (() => {
  const F = { decision: "", q: "", seeded: true };

  async function render() {
    const view = $("#view"); view.innerHTML = "";
    const head = h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Ledger"), h("p", {}, "Everything the desk has processed, what needed a person, and how much work it saved.")),
      h("label", { class: "row", style: { fontSize: "13.5px", cursor: "pointer" } }, h("input", { type: "checkbox", checked: F.seeded, onchange: (e) => { F.seeded = e.target.checked; render(); } }), "Include seeded history"));
    view.append(head);
    let st;
    try { st = await api("/api/stats?include_seed=" + (F.seeded ? 1 : 0)); } catch (e) { view.append(h("div", { class: "empty" }, e.message)); return; }
    App.setBadge(st.open_review);

    /* headline figures */
    const hero = h("div", { class: "sheet hero" },
      h("div", { class: "big" }, h("b", {}, pct(st.stp_rate)), h("span", {}, "of invoices cleared with no one touching them")),
      h("div", { class: "mixwrap" }, Charts.mix(st.by_decision, st.amount), Charts.legend(DEC_ORDER)));
    const tile = (l, v, n, tip) => h("div", { class: "sheet tile", title: tip || "" }, h("div", { class: "l" }, l), h("div", { class: "v num" }, v), h("div", { class: "n" }, n));
    const kpis = h("div", { class: "kpis" }, hero,
      tile("Invoices processed", String(st.processed), `${st.by_decision.APPROVE} approved, ${st.processed - st.by_decision.APPROVE} stopped or sent to a person`),
      tile("Approved for payment", compactUsd(st.amount.APPROVE), `${st.by_decision.APPROVE} invoices, net of tax and matched to a PO`),
      tile("Held back for risk", compactUsd(st.risk_blocked), `${st.risk_blocked_n} invoices with duplicate, bank-change, vendor or hidden-text flags`, "Invoice value that would have been paid had it not been stopped"));
    kpis.append(tile("Average processing time", st.avg_seconds + " s", `Per invoice, against about ${st.manual_minutes_assumed} minutes by hand (assumption)`, "Compute time only. The pauses between stages on screen are not counted."),
      tile("Hours saved (estimate)", st.hours_saved + " h", `Approved invoices save the full ${st.manual_minutes_assumed} minutes, the rest save half because a person starts with the evidence`, "Assumption: a person spends 7 minutes on one invoice"),
      tile("Waiting for a person", String(st.open_review), st.open_review ? "See the review queue below" : "Nothing is waiting"));
    view.append(kpis);

    /* charts */
    view.append(h("div", { class: "charts" },
      h("div", { class: "sheet chart" }, h("h2", {}, "Runs per day"), h("div", { class: "sub", style: { marginBottom: "10px" } }, "Stacked by decision"), Charts.daily(st.series), h("div", { style: { marginTop: "8px" } }, Charts.legend(DEC_ORDER))),
      h("div", { class: "sheet chart" }, h("h2", {}, "Why invoices were not approved"), h("div", { class: "sub" }, "Main reason on each stopped invoice"), Charts.ranked(st.reasons))));

    /* review queue */
    const q = await api("/api/runs?review=open&limit=50&include_seed=" + (F.seeded ? 1 : 0)).catch(() => []);
    const rq = h("div", { class: "sheet review-q" }, h("header", {}, h("h2", {}, "Review queue"), h("span", { class: "sub" }, q.length ? `${q.length} waiting. Open one to approve or reject it with a reason.` : "Nothing is waiting for a person.")));
    if (q.length) rq.append(h("div", { class: "tbl-wrap" }, runsTable(q.slice().reverse(), true)));
    view.append(rq);

    /* history */
    const box = h("div", { class: "sheet" });
    const chips = h("div", { class: "filters" });
    const search = h("input", { type: "search", placeholder: "Search vendor, invoice, PO or reason", value: F.q, "aria-label": "Search runs", style: { width: "280px" } });
    const body = h("div", { class: "tbl-wrap" });
    const counts = st.by_decision;
    const draw = async () => {
      chips.innerHTML = "";
      chips.append(h("h2", { style: { marginRight: "8px" } }, "All runs"));
      [["", "All", st.processed]].concat(DEC_ORDER.map((k) => [k, DEC[k].label, counts[k] || 0])).forEach(([k, l, n]) =>
        chips.append(h("button", { class: "fchip", "aria-pressed": String(F.decision === k), onclick: () => { F.decision = k; draw(); } }, l, h("span", { class: "n" }, n))));
      chips.append(h("span", { class: "grow" }), search);
      const rows = await api(`/api/runs?limit=300&include_seed=${F.seeded ? 1 : 0}&decision=${F.decision}&q=${encodeURIComponent(F.q)}`).catch(() => []);
      body.innerHTML = "";
      body.append(rows.length ? runsTable(rows) : h("div", { class: "empty" }, F.q || F.decision ? "No runs match this filter." : "No runs yet. Process an invoice on the desk and it will show up here."));
      if (document.activeElement !== search && F.q) search.focus();
    };
    let t; search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { F.q = search.value.trim(); draw().then(() => { search.focus(); search.setSelectionRange(search.value.length, search.value.length); }); }, 220); });
    box.append(chips, body); view.append(box);
    await draw();
  }

  function runsTable(rows, queue) {
    const tbl = h("table", { class: "tbl runs" }, h("thead", {}, h("tr", {}, ["Run", "Received", "Vendor", "Invoice", "PO", "Amount", "Decision", "Outcome"].map((t, i) => h("th", { class: i === 5 ? "r" : "" }, t)))));
    const tb = h("tbody");
    rows.forEach((r) => {
      const tr = h("tr", { class: "click", tabindex: "0", onclick: () => (location.hash = "#/run/" + r.id), onkeydown: (e) => { if (e.key === "Enter") location.hash = "#/run/" + r.id; } },
        h("td", {}, h("span", { class: "code" }, runCode(r.id)), r.source === "seed" ? h("div", { class: "sub" }, "seeded") : r.dry_run ? h("div", { class: "sub" }, "what-if") : null),
        h("td", {}, fmtDT(r.created_at)),
        h("td", {}, r.vendor_name || h("span", { class: "muted" }, "Unknown")),
        h("td", {}, r.invoice_number || h("span", { class: "muted" }, "none read")),
        h("td", {}, r.po_number || h("span", { class: "muted" }, "-")),
        h("td", { class: "r" }, r.total == null ? "-" : usd(r.total)),
        h("td", {}, r.status === "running" ? pill(null, "Running") : r.status === "error" ? pill(null, "Failed") : pill(r.decision), r.review_status && r.review_status !== "open" ? h("div", { class: "sub" }, r.review_status === "approved" ? "approved by a person" : "rejected by a person") : null),
        h("td", { class: "head" }, r.headline || r.error || ""));
      tb.append(tr);
    });
    tbl.append(tb); return tbl;
  }
  return { render };
})();

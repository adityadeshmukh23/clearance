/* Desk: where an invoice comes in. Upload a file or run one of the prepared samples. */
const Desk = (() => {
  const GROUPS = [
    ["happy", "Happy path", "A clean invoice that should clear without anyone touching it."],
    ["edge1", "Edge case 1: a smudged scan with no PO number", "Run the first, then the vendor's corrected resubmission."],
    ["edge2", "Edge case 2: milestone billing against one PO", "Run in order. The second one pushes the PO over budget."],
    ["edge3", "Edge case 3: the same invoice again, re-scanned", "A different file and a different number format for an invoice already paid."],
    ["edge4", "Edge case 4: changed bank details", "Looks normal until the bank account and e-mail domain are checked."],
    ["tolerance|layout|extra", "Also handled", "Tolerances, other layouts, unknown and blocked vendors, statements, hidden instructions."],
  ];

  async function render() {
    const view = $("#view"); view.innerHTML = "";
    view.append(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Invoice desk"),
      h("p", {}, "Drop in an invoice or run a prepared sample. Every stage runs live, shows what it checked, and ends in one of four decisions."))));
    const left = h("div"), right = h("div", { class: "sheet" });
    view.append(h("div", { class: "inbox" }, left, right));

    /* upload */
    const input = h("input", { type: "file", accept: ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp,.bmp,application/pdf,image/*", class: "sr", id: "fileIn" });
    const drop = h("label", { class: "drop", for: "fileIn", tabindex: "0" }, h("b", {}, "Drop an invoice here"), h("p", {}, "PDF, PNG, JPG or TIFF, up to 12 MB. Scans and photos are read with OCR."));
    const go = async (file) => {
      if (!file) return;
      const fd = new FormData(); fd.append("file", file); fd.append("mode", App.state.mode); fd.append("pace", App.state.pace);
      drop.querySelector("b").textContent = "Uploading " + file.name + "…";
      try { const r = await api("/api/runs", { method: "POST", form: fd }); location.hash = "#/run/" + r.run_id; }
      catch (e) { toast(e.message, "bad"); drop.querySelector("b").textContent = "Drop an invoice here"; }
    };
    input.addEventListener("change", () => go(input.files[0]));
    drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => go(e.dataTransfer.files[0]));

    /* options */
    const aiOn = App.sys && App.sys.ai.configured;
    const seg = (key, items, disabledKeys = []) => {
      const box = h("div", { class: "seg", role: "group" });
      items.forEach(([v, l]) => { const b = h("button", { type: "button", "aria-pressed": String(App.state[key] === v), disabled: disabledKeys.includes(v), onclick: () => { App.state[key] = v; $$("button", box).forEach((x) => x.setAttribute("aria-pressed", String(x === b))); } }, l); box.append(b); });
      return box;
    };
    const opts = h("div", { class: "sheet sheet-pad opts" },
      h("div", { class: "field" }, h("label", {}, "Reader"), seg("mode", [["auto", "AI + local"], ["local", "Local only"]]),
        h("div", { class: "help" }, aiOn ? "The AI reads the invoice and the local reader checks it independently. If the AI is unavailable the run falls back to local." : "No AI key is set, so both options use the local reader (PDF text, OCR and rules). Add GEMINI_API_KEY to turn on the AI reader.")),
      h("div", { class: "field" }, h("label", {}, "Pace of the stages"), seg("pace", [["normal", "Demo"], ["slow", "Slow"], ["instant", "Instant"]]),
        h("div", { class: "help" }, "Demo gives each stage about a second so it can be followed on screen. Instant skips the pause.")));
    if (App.state.mode === "auto" && !aiOn) { /* keep as is: server falls back */ }
    left.append(input, drop, opts);

    /* samples */
    right.append(h("div", { class: "sheet-pad", style: { borderBottom: "1px solid var(--rule)" } }, h("h2", {}, "Prepared samples"),
      h("div", { class: "sub" }, "All data here is synthetic. The samples are designed to cover the cases that make invoice handling hard.")));
    const list = h("div", { class: "sample-list" }, h("div", { class: "empty" }, "Loading samples…")); right.append(list);
    let man = [];
    try { man = await api("/api/samples"); } catch (e) { list.innerHTML = ""; list.append(h("div", { class: "empty" }, "Samples could not be loaded. " + e.message)); return; }
    list.innerHTML = "";
    const byId = Object.fromEntries(man.map((m) => [m.id, m]));
    GROUPS.forEach(([groups, title, blurb]) => {
      const items = man.filter((m) => groups.split("|").includes(m.group)); if (!items.length) return;
      list.append(h("div", { class: "sample-group" }, title + "  ", h("span", { class: "muted", style: { fontWeight: 400 } }, blurb)));
      items.forEach((m) => {
        const hint = m.after && byId[m.after] ? `Run after ${m.after}` : "";
        const btn = h("button", { class: "sample", onclick: async () => {
          btn.disabled = true;
          try { const r = await api("/api/runs/sample", { method: "POST", json: { sample_id: m.id, mode: App.state.mode, pace: App.state.pace } }); location.hash = "#/run/" + r.run_id; }
          catch (e) { toast(e.message, "bad"); btn.disabled = false; }
        } },
          h("span", { class: "t" }, h("span", { class: "muted", style: { fontWeight: 500, marginRight: "8px" } }, m.id), m.title.replace(/^(EDGE\s+\d+[a-z]?|[A-Z]\d+[a-z]?)\s*[-:]\s*/, "")),
          h("span", { class: "side" }, m.last ? pill(m.last.decision) : h("span", {}, "Not run yet"), h("span", { class: "go" }, m.last ? "Run again" : "Run")),
          h("span", { class: "b" }, m.blurb),
          h("span", { class: "meta" }, (m.tags || []).map((t) => h("span", { class: "tag" }, t)), hint ? h("span", { class: "tag", style: { background: "var(--escalate-wash)", borderColor: "#ecd08d", color: "var(--escalate-ink)" } }, hint) : null));
        list.append(btn);
      });
    });
  }
  return { render };
})();

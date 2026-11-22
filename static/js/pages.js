/* Reference data (vendors, POs, policy) and the How it works page. */
const Reference = (() => {
  async function render() {
    const view = $("#view"); view.innerHTML = "";
    view.append(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Reference data"), h("p", {}, "What the desk checks every invoice against: the vendor master, open purchase orders and the payment policy. Change a policy value and use \"Re-run with current policy\" on any run to see the outcome change."))));
    let ref;
    try { ref = await api("/api/reference"); } catch (e) { view.append(h("div", { class: "empty" }, e.message)); return; }

    const vend = h("div", { class: "sheet" }, h("div", { class: "sheet-pad" }, h("h2", {}, "Vendor master"), h("div", { class: "sub" }, "Bank details here are what invoices are compared with. They change only through a controlled process, never from an invoice.")),
      h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["Vendor", "Tax ID", "Terms", "Bank on file", "Status"].map((t) => h("th", {}, t)))),
        h("tbody", {}, ref.vendors.map((v) => h("tr", {}, h("td", {}, h("div", {}, v.legal_name), h("div", { class: "sub" }, v.vendor_id)), h("td", { class: "nw" }, v.tax_id), h("td", { class: "nw" }, v.payment_terms),
          h("td", {}, v.bank_name, h("div", { class: "sub" }, "…" + String(v.bank_account || "").slice(-4))),
          h("td", {}, h("span", { class: "tag", style: v.status !== "approved" ? { background: "var(--reject-wash)", color: "var(--reject-ink)", borderColor: "#efb9b9" } : {} }, v.status), v.status_note ? h("div", { class: "sub" }, v.status_note) : null)))))));

    const pos = ref.pos.filter((p) => !String(p.description || "").startsWith("Background"));
    const poBox = h("div", { class: "sheet" }, h("div", { class: "sheet-pad" }, h("h2", {}, "Purchase orders"), h("div", { class: "sub" }, `${ref.pos.length} on file. Remaining balance updates as invoices are approved. Showing the ones the samples use first.`)),
      h("div", { class: "tbl-wrap", style: { maxHeight: "460px" } }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["PO", "Vendor", "Ordered", "Billed", "Left"].map((t, i) => h("th", { class: i > 1 ? "r" : "" }, t)))),
        h("tbody", {}, pos.concat(ref.pos.filter((p) => !pos.includes(p))).map((p) => {
          const left = p.remaining, used = p.billed || 0;
          return h("tr", {}, h("td", {}, h("div", {}, p.po_number), h("div", { class: "sub" }, p.status)), h("td", {}, p.vendor_name || p.vendor_id),
            h("td", { class: "r" }, usd0(p.ordered)), h("td", { class: "r" }, usd0(used)), h("td", { class: "r" }, left < 0 ? h("b", { style: { color: "var(--reject-ink)" } }, usd0(left)) : usd0(left)));
        })))));

    const inputs = {};
    const polBox = h("div", { class: "sheet" }, h("div", { class: "sheet-pad", style: { borderBottom: "1px solid var(--rule)" } }, h("h2", {}, "Payment policy"), h("div", { class: "sub" }, "Rules are code, not prompts. The AI never decides whether an invoice is paid.")),
      h("div", { class: "policy" }, ref.policy_meta.map((m) => {
        const isBool = typeof m.value === "boolean";
        const inp = isBool ? h("select", { id: "p_" + m.key }, h("option", { value: "true", selected: m.value }, "Yes"), h("option", { value: "false", selected: !m.value }, "No")) : h("input", { type: "number", step: "any", value: m.value, id: "p_" + m.key });
        inputs[m.key] = inp;
        return h("div", { class: "p" }, h("label", { for: "p_" + m.key }, m.label), h("div", { class: "help" }, m.help), h("div", { class: "in" }, inp, h("span", { class: "u" }, isBool ? "" : m.unit)));
      }), h("div", { class: "row", style: { justifyContent: "flex-end", gap: "10px", paddingTop: "6px" } },
        h("button", { class: "btn secondary small", onclick: () => render() }, "Discard edits"),
        h("button", { class: "btn small", onclick: async () => {
          const body = {}; ref.policy_meta.forEach((m) => { const el = inputs[m.key]; body[m.key] = typeof m.value === "boolean" ? el.value === "true" : parseFloat(el.value); if (!(typeof m.value === "boolean") && Number.isNaN(body[m.key])) body[m.key] = m.value; });
          try { await api("/api/policy", { method: "PUT", json: body }); toast("Policy saved. New runs use it straight away."); } catch (e) { toast(e.message, "bad"); }
        } }, "Save policy"))));
    view.append(h("div", { class: "two" }, h("div", { style: { display: "grid", gap: "16px" } }, vend, poBox), polBox));
  }
  return { render };
})();

const About = (() => {
  const STEPS = [
    ["Receive and read", "Opens the file. Digital PDFs give exact text and positions; scans and photos are straightened and read with OCR.", "rules", "Local"],
    ["Is this an invoice?", "Statements, quotes and other documents are turned away before any AI is used.", "rules", "Rules"],
    ["Extract the data", "An AI reader returns fields with a confidence and the place on the page it read each from. A local reader does the same independently.", "both", "AI and local"],
    ["Verify the numbers", "Subtotal, tax and total are added up again in code. Missing or low-confidence fields stop the run.", "rules", "Rules"],
    ["Verify vendor and bank", "Matches the vendor by tax ID and name, then compares bank details and e-mail domain with the vendor master.", "rules", "Rules"],
    ["Check for duplicates", "Same file, same number, a re-formatted number, or the same vendor, amount and date. A held invoice that is resubmitted replaces the held one.", "rules", "Rules"],
    ["Match to a PO", "Uses the printed PO, a near match, or infers it from items and prices. Checks prices, quantities received and what is left on the PO.", "rules", "Rules"],
    ["Apply policy", "Approval limit, stale and future dates, tolerances. Values come from the policy you can edit.", "rules", "Rules"],
    ["Decide", "Four outcomes. Reject beats escalate beats hold. Every decision lists the checks that caused it.", "rules", "Rules"],
    ["Draft and update", "Writes the e-mail, the escalation brief or the ERP posting, and reserves the PO balance. AI may polish wording, but every number is verified first.", "both", "Rules, AI wording"],
  ];
  async function render() {
    const view = $("#view"); view.innerHTML = "";
    const sys = await api("/api/system?probe=0").catch(() => null);
    view.append(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "How it works"), h("p", {}, "The AI reads and writes. Deterministic rules decide. Arithmetic is checked in code, and two independent readers are compared before anything is trusted."))));
    const flow = h("div", { class: "flow" }, STEPS.map(([t, d, by, who]) => h("div", { class: "s" }, h("b", {}, t), h("p", {}, d), h("span", { class: "by " + by }, who))));
    view.append(h("div", { class: "sheet sheet-pad" }, h("h2", {}, "The ten stages"), h("div", { class: "sub" }, "Blue marks where an AI model is involved; dark marks plain code."), flow));
    const col = (title, items) => h("div", { class: "sheet sheet-pad prose" }, h("h2", { style: { margin: "0 0 8px", fontSize: "15px" } }, title), h("ul", {}, items.map((i) => h("li", {}, i))));
    view.append(h("div", { class: "cols3", style: { marginTop: "16px" } },
      col("Decisions", ["Approve: every check passed. The posting payload is ready and the PO balance is reserved.", "Hold: something is missing and only the vendor can supply it. A reply is drafted.", "Escalate: a person with authority must look. The brief names who and why.", "Reject: a duplicate, an unknown or blocked vendor, or not an invoice."]),
      col("Assumptions", ["The vendor master and PO list are the source of truth; invoices never change them.", "Net of tax is matched to the PO, since POs are priced without tax.", "Amounts are in one currency. A different currency is escalated, not converted.", "A person takes about 7 minutes per invoice. This is only used for the hours-saved estimate."]),
      col("Limits and next steps", ["The data is synthetic. Free AI tiers may use prompts for training, so do not upload real invoices.", "Messy handwriting and heavy photos can fall below the confidence floor and go to a person, by design.", "Next: connect to a real ERP and mailbox, learn vendor-specific layouts from corrections, and add multi-currency.", "Emails are drafts. Nothing is sent and nothing is posted."])));
    if (sys) {
      const box = h("div", { class: "sheet sheet-pad", style: { marginTop: "16px" } }, h("h2", {}, "System check"));
      const rows = h("div", { class: "kv", style: { marginTop: "10px" } },
        h("div", {}, "AI reader"), h("div", { id: "aiState" }, sys.ai.configured ? "Key found. Models in order: " + sys.ai.models.join(", ") : "No key set (GEMINI_API_KEY). The local reader is used."),
        h("div", {}, "OCR"), h("div", {}, sys.tesseract), h("div", {}, "Clock for date checks"), h("div", {}, fmtDate(sys.as_of)), h("div", {}, "Runs stored"), h("div", {}, String(sys.runs)));
      box.append(rows, h("div", { style: { marginTop: "12px" } }, h("button", { class: "btn secondary small", onclick: App.checkAI }, "Test the AI connection")));
      view.append(box);
    }
  }
  return { render };
})();

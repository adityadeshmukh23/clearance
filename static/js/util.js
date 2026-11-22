/* Shared helpers: DOM, API, formatting, icons, tooltip, toasts. No framework, no build step. */
const App = { state: { mode: "auto", pace: "normal" }, sys: null };

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") { for (const [sk, sv] of Object.entries(v)) sk.startsWith("--") ? el.style.setProperty(sk, sv) : (el.style[sk] = sv); }
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.json !== undefined) { init.headers["content-type"] = "application/json"; init.body = JSON.stringify(opts.json); }
  if (opts.form) init.body = opts.form;
  let res;
  try { res = await fetch(path, init); } catch (e) { throw new Error("Cannot reach the server. Is it still running?"); }
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || j); } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

/* ---------- formatting ---------- */
const usd = (n, d = 2) => n == null ? "-" : (n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const usd0 = (n) => usd(n, 0);
const compactUsd = (n) => n == null ? "-" : Math.abs(n) >= 1e6 ? "$" + (n / 1e6).toFixed(2) + "M" : Math.abs(n) >= 1e4 ? "$" + (n / 1e3).toFixed(1) + "K" : usd0(n);
const pct = (x, d = 0) => (x * 100).toFixed(d) + "%";
const fmtDate = (iso) => { if (!iso) return "-"; const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso); return d.toLocaleDateString("en-US", { day: "numeric", month: "short", year: "numeric" }); };
const fmtDay = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-US", { day: "numeric", month: "short" });
const fmtDT = (iso) => { if (!iso) return "-"; const d = new Date(iso); return d.toLocaleDateString("en-US", { day: "numeric", month: "short" }) + ", " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }); };
const ms = (n) => n == null ? "" : n < 1000 ? n + " ms" : (n / 1000).toFixed(1) + " s";
const runCode = (id) => "RUN-" + String(id).padStart(4, "0");

const DEC = {
  APPROVE: { label: "Approved", short: "Approve", stamp: "APPROVED", icon: "check", color: "var(--approve)" },
  HOLD: { label: "On hold", short: "Hold", stamp: "ON HOLD", icon: "pause", color: "var(--hold)" },
  ESCALATE: { label: "Escalated", short: "Escalate", stamp: "ESCALATED", icon: "flag", color: "var(--escalate)" },
  REJECT: { label: "Rejected", short: "Reject", stamp: "REJECTED", icon: "cross", color: "var(--reject)" },
};
const DEC_ORDER = ["APPROVE", "HOLD", "ESCALATE", "REJECT"];

const CODE_LABEL = {
  DUPLICATE_INVOICE: "Duplicate invoice", BANK_MISMATCH: "Bank account changed", BANK_CHANGE_NOTICE: "Bank change notice", EMAIL_LOOKALIKE: "Look-alike e-mail domain",
  EMAIL_DOMAIN_DIFF: "E-mail domain differs", HIDDEN_TEXT: "Hidden text in file", INJECTION_TEXT: "Instructions hidden in file", VENDOR_BLOCKED: "Vendor blocked",
  VENDOR_UNKNOWN: "Vendor not in master", VENDOR_NOT_APPROVED: "Vendor not approved", VENDOR_UNCERTAIN: "Vendor match uncertain", TAXID_MISMATCH: "Tax ID mismatch",
  NOT_AN_INVOICE: "Not an invoice", UNREADABLE_FILE: "File unreadable", MISSING_INVOICE_NUMBER: "Invoice number missing", MISSING_TOTAL: "Total missing",
  MISSING_VENDOR: "Vendor missing", MISSING_DATE: "Invoice date missing", MISSING_PO: "No PO reference", LOW_CONF_FIELD: "Low-confidence read",
  OVER_LIMIT: "Above approval limit", PO_OVERBILLED: "Over PO balance", PRICE_VARIANCE: "Price above PO", QTY_OVER_RECEIPT: "Billed more than received",
  QTY_OVER_ORDER: "Billed more than ordered", PO_AMBIGUOUS: "PO match ambiguous", PO_NOT_FOUND: "PO not found", PO_NOT_OPEN: "PO not open", PO_MISSING: "No PO on invoice",
  PO_VENDOR_MISMATCH: "PO belongs to another vendor", PO_NONE_AVAILABLE: "No open PO for vendor", LINE_NOT_ON_PO: "Line not on PO", TAX_MISMATCH: "Tax differs from PO",
  TOTAL_VARIANCE: "Total differs from PO", ARITH_TOTAL: "Totals do not add up", ARITH_LINES: "Lines do not add up", ARITH_INCL: "Tax-inclusive math off",
  ARITH_LINE_MATH: "Line math off", DATE_STALE: "Invoice too old", DATE_FUTURE: "Invoice dated in future", DATE_AMBIGUOUS: "Date ambiguous", DUE_BEFORE_INVOICE: "Due before invoice date",
  CURRENCY_MISMATCH: "Currency differs", INVOICE_NUMBER_REUSED: "Invoice number reused", XCHECK_MISMATCH: "Readers disagree", ANOMALY: "Document anomaly", BANK_OCR_DOUBT: "Bank digits unclear",
};
const codeLabel = (c) => CODE_LABEL[c] || (c || "").toLowerCase().replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase());

/* ---------- icons (inline, currentColor) ---------- */
const ICONS = {
  check: '<path d="M3.5 8.5l3 3 6-7" />', cross: '<path d="M4 4l8 8M12 4l-8 8" />', pause: '<path d="M5.5 3.5v9M10.5 3.5v9" />',
  flag: '<path d="M4.5 14V2.5M4.5 3h7l-1.8 2.8L11.5 8.6h-7" />', warn: '<path d="M8 3.2l5.6 9.6H2.4zM8 7v2.6M8 11.3v.1" />',
  minus: '<path d="M4 8h8" />', info: '<circle cx="8" cy="8" r="5.5" /><path d="M8 7.4v3.4M8 5.2v.1" />', arrow: '<path d="M3 8h10M9 4l4 4-4 4" />', copy: '<rect x="5.5" y="5.5" width="7.5" height="8" rx="1" /><path d="M3 10.5V3.5a1 1 0 011-1h6" />',
};
function icon(name, size) {
  const s = size ? ` style="width:${size}px;height:${size}px"` : "";
  return `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${s}>${ICONS[name] || ""}</svg>`;
}
const sevIcon = (sev) => ({ pass: "check", fail: "cross", warn: "warn", info: "info" }[sev] || "info");
function pill(decision, label) {
  const d = DEC[decision];
  if (!d) return h("span", { class: "pill none" }, label || "In progress");
  return h("span", { class: "pill " + decision, html: icon(d.icon) + `<span>${esc(label || d.label)}</span>` });
}

/* ---------- tooltip + toast ---------- */
const tipEl = () => $("#tip");
function showTip(ev, html) {
  const t = tipEl(); t.innerHTML = html; t.classList.add("on");
  const pad = 14, r = t.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
  t.style.left = Math.max(8, x) + "px"; t.style.top = Math.max(8, y) + "px";
}
const hideTip = () => tipEl().classList.remove("on");
function toast(msg, kind) {
  const el = h("div", { class: "toast " + (kind || "") }, msg);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "bad" ? 7000 : 3800);
}
async function copyText(text) { try { await navigator.clipboard.writeText(text); toast("Copied"); } catch (_) { toast("Copy failed. Select the text and copy it manually.", "bad"); } }

/* ---------- misc ---------- */
function confBars(c) {
  const lv = c >= .9 ? 3 : c >= .75 ? 2 : c > 0 ? 1 : 0;
  return h("span", { class: "conf" + (c > 0 && c < .8 ? " low" : ""), title: "Reader confidence " + pct(c) }, pct(c), h("i", { style: { "--h": lv >= 1 ? "100%" : "0%" } }), h("i", { style: { "--h": lv >= 2 ? "100%" : "0%" } }), h("i", { style: { "--h": lv >= 3 ? "100%" : "0%" } }));
}
const sleep = (t) => new Promise((r) => setTimeout(r, t));

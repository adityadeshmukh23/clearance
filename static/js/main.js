/* Router and top bar. */
App.setBadge = (n) => { const b = $("#reviewCount"); b.textContent = n; b.classList.toggle("hidden", !n); };
App.refreshBadge = () => api("/api/stats").then((s) => App.setBadge(s.open_review)).catch(() => {});

App.refreshSystem = async (probe) => {
  const dot = $("#aiDot"), lbl = $("#aiLbl");
  try {
    const s = await api("/api/system" + (probe ? "?probe=1" : "")); App.sys = s;
    if (!s.ai.configured) { dot.className = "dot warn"; lbl.textContent = "Local reader (no AI key)"; return s; }
    if (probe) {
      const p = s.ai.probe || {};
      dot.className = "dot " + (p.ok ? "ok" : "bad");
      lbl.textContent = p.ok ? "AI reader ready" + (p.model ? ": " + p.model : "") : "AI reader failed";
      toast(p.ok ? "AI connection works" + (p.model ? " (" + p.model + ")" : "") : "AI connection failed: " + (p.error || "unknown error"), p.ok ? "" : "bad");
    } else { dot.className = "dot ok"; lbl.textContent = "AI reader configured"; }
    return s;
  } catch (e) { dot.className = "dot bad"; lbl.textContent = "Server not reachable"; return null; }
};
App.checkAI = async () => { $("#aiLbl").textContent = "Testing…"; const s = await App.refreshSystem(true); const el = $("#aiState"); if (el && s && s.ai.probe) el.textContent = s.ai.probe.ok ? "Connected (" + (s.ai.probe.model || "ok") + ")" : "Failed: " + (s.ai.probe.error || "unknown"); };

const routes = {
  desk: () => Desk.render(), ledger: () => Ledger.render(), reference: () => Reference.render(), about: () => About.render(),
  run: (id) => Case.open(parseInt(id, 10)),
};
async function route() {
  Case.close(); hideTip();
  const [, name = "desk", arg] = (location.hash || "#/desk").split("/");
  const key = routes[name] ? name : "desk";
  $$(".nav a").forEach((a) => a.toggleAttribute("aria-current", false));
  const nav = $(`.nav a[data-nav="${key === "run" ? "ledger" : key}"]`); if (nav) nav.setAttribute("aria-current", "page");
  window.scrollTo(0, 0);
  try { await routes[key](arg); } catch (e) { console.error(e); $("#view").innerHTML = ""; $("#view").append(h("div", { class: "empty" }, "Something went wrong drawing this page: " + e.message)); }
  $("#view").focus({ preventScroll: true });
}
addEventListener("hashchange", route);
$("#aiChip").addEventListener("click", () => App.checkAI());
$("#resetBtn").addEventListener("click", async () => {
  if (!confirm("Delete the runs you made and restore the seeded history and PO balances?")) return;
  try { await api("/api/admin/reset", { method: "POST" }); toast("Demo reset"); App.refreshBadge(); location.hash = "#/desk"; route(); } catch (e) { toast(e.message, "bad"); }
});
(async () => { await App.refreshSystem(); App.refreshBadge(); route(); })();

/* Charts: hand-built SVG/HTML, thin marks, hairline grid, hover tooltips, legend always present for 2+ series.
   Decision colours come from CSS variables (validated as a set for colour-blind separation; amber is paired with labels). */
const Charts = {};

Charts.legend = function (keys) {
  return h("div", { class: "legend", role: "list" }, keys.map((k) => h("span", { role: "listitem" }, h("i", { class: k + "-bg" }), DEC[k].label)));
};

/* One whole, split into the four decisions. Tall segmented bar: share printed inside when it fits. */
Charts.mix = function (by, amount) {
  const total = DEC_ORDER.reduce((s, k) => s + (by[k] || 0), 0);
  const bar = h("div", { class: "segbar", role: "img", "aria-label": "Decision mix: " + DEC_ORDER.map((k) => `${by[k] || 0} ${DEC[k].label.toLowerCase()}`).join(", ") });
  if (!total) return h("div", { class: "muted" }, "No runs yet.");
  DEC_ORDER.forEach((k) => {
    const n = by[k] || 0; if (!n) return;
    const share = n / total;
    const seg = h("div", { class: k.toLowerCase() + " " + k + "-bg", style: { flex: String(n) + " 1 0" }, tabindex: "0",
      "aria-label": `${DEC[k].label}: ${n} runs, ${pct(share)}, ${usd0(amount[k] || 0)}` }, share >= 0.1 ? pct(share) : "");
    const tip = (e) => showTip(e, `<b><i class="${k}-bg"></i>${DEC[k].label}</b><div class="r"><span>Runs</span><span>${n} (${pct(share)})</span></div><div class="r"><span>Invoice value</span><span>${usd0(amount[k] || 0)}</span></div>`);
    seg.addEventListener("mousemove", tip); seg.addEventListener("mouseleave", hideTip);
    seg.addEventListener("focus", () => { const r = seg.getBoundingClientRect(); tip({ clientX: r.left + 10, clientY: r.bottom }); }); seg.addEventListener("blur", hideTip);
    bar.append(seg);
  });
  return bar;
};

function niceMax(v) {
  if (v <= 4) return 4;
  const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}
function topRounded(x, y, w, hh, r) {
  r = Math.min(r, hh, w / 2);
  return `M${x},${y + hh}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hh}Z`;
}

/* Runs per day, stacked by decision. */
Charts.daily = function (series) {
  const days = series.slice(-30);
  const wrap = h("div");
  if (!days.length) { wrap.append(h("div", { class: "empty" }, "Runs will appear here by day.")); return wrap; }
  const W = 640, H = 230, L = 30, R = 6, T = 8, B = 28, pw = W - L - R, ph = H - T - B;
  const maxV = niceMax(Math.max(1, ...days.map((d) => DEC_ORDER.reduce((s, k) => s + d[k], 0))));
  const band = pw / days.length, bw = Math.min(24, band * 0.64);
  const y = (v) => T + ph - (v / maxV) * ph;
  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = (maxV / 4) * i, yy = y(v);
    g += `<line class="ax" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/><text x="${L - 8}" y="${yy + 4}" text-anchor="end">${Math.round(v)}</text>`;
  }
  const every = Math.max(1, Math.ceil(days.length / 7));
  days.forEach((d, i) => {
    const cx = L + band * i + band / 2;
    if (i % every === 0 || i === days.length - 1) g += `<text x="${cx}" y="${H - 8}" text-anchor="middle">${fmtDay(d.day)}</text>`;
    let acc = 0;
    const present = DEC_ORDER.filter((k) => d[k] > 0);
    present.forEach((k, idx) => {
      const v = d[k], y0 = y(acc), y1 = y(acc + v), gap = idx > 0 ? 2 : 0;
      const top = y1, height = Math.max(1, y0 - y1 - gap);
      const col = `var(--${k.toLowerCase()})`;
      g += idx === present.length - 1
        ? `<path d="${topRounded(cx - bw / 2, top, bw, height, 4)}" fill="${col}"/>`
        : `<rect x="${cx - bw / 2}" y="${top}" width="${bw}" height="${height}" fill="${col}"/>`;
      acc += v;
    });
  });
  g += `<line class="ax" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" style="stroke:#b9c1cc"/>`;
  const svg = h("div", { html: `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Runs per day by decision">${g}<g id="hit"></g></svg>` });
  const hit = $("#hit", svg);
  days.forEach((d, i) => {
    const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r.setAttribute("x", L + band * i); r.setAttribute("y", T); r.setAttribute("width", band); r.setAttribute("height", ph); r.setAttribute("fill", "transparent");
    const tot = DEC_ORDER.reduce((s, k) => s + d[k], 0);
    r.addEventListener("mousemove", (e) => showTip(e, `<b>${fmtDate(d.day)}: ${tot} run${tot === 1 ? "" : "s"}</b>` + DEC_ORDER.filter((k) => d[k]).map((k) => `<div class="r"><span><i class="${k}-bg"></i>${DEC[k].label}</span><span>${d[k]}</span></div>`).join("")));
    r.addEventListener("mouseleave", hideTip);
    hit.append(r);
  });
  wrap.append(svg);
  return wrap;
};

/* Ranked horizontal bars, one neutral colour (nominal categories carry no colour). */
Charts.ranked = function (rows, onClick) {
  if (!rows.length) return h("div", { class: "empty" }, "Nothing has been stopped yet.");
  const max = Math.max(...rows.map((r) => r[1]));
  return h("div", { class: "barlist" }, rows.map(([code, n]) => {
    const el = h("div", { class: "bl" },
      h("span", { title: code }, codeLabel(code)),
      h("span", { class: "track", "aria-hidden": "true" }, h("i", { style: { width: Math.max(4, (n / max) * 100) + "%" } })),
      h("span", { class: "c" }, n));
    el.addEventListener("mousemove", (e) => showTip(e, `<b>${esc(codeLabel(code))}</b><div class="r"><span>Runs that were not approved</span><span>${n}</span></div>`));
    el.addEventListener("mouseleave", hideTip);
    return el;
  }));
};

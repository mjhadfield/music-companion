/*
 * Minimal SVG bar chart, following the dataviz skill's mark specs:
 * bars capped at 24px thick with a 2px surface gap, 4px rounded data-end
 * (square at the baseline), hairline recessive gridlines, a hover
 * tooltip (every chart here is a single series, so no legend needed --
 * the section heading already names what's plotted).
 *
 * No charting library -- one file, one function, plain SVG strings.
 */

function chartEsc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" }[c]
  ));
}

function niceCeiling(value) {
  if (value <= 0) return 1;
  const exp = Math.floor(Math.log10(value));
  const base = 10 ** exp;
  const fraction = value / base;
  const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return niceFraction * base;
}

function roundedTopRectPath(x, y, w, h, r) {
  if (h <= 0) return "";
  r = Math.min(r, h, w / 2);
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
}

/**
 * @param container  DOM element to render into
 * @param data        [{label, value, key}]  -- key defaults to label; it's
 *                     the raw bucket identity used for click-selection and
 *                     (by the caller) for filtering, while label is what's
 *                     drawn on the axis
 * @param opts        { color, height, formatValue(v), maxLabels, onClick(d), selectedKey }
 */
function renderBarChart(container, data, opts = {}) {
  const {
    color = "var(--accent-scrobble)",
    height = 160,
    formatValue = (v) => v.toLocaleString(),
    maxLabels = 14,
    selectedKey = null,
  } = opts;

  if (!data.length) {
    container.innerHTML = '<div class="subtle">No data yet.</div>';
    return;
  }

  const width = 640;
  const padLeft = 38, padBottom = 20, padTop = 8, padRight = 4;
  const plotWidth = width - padLeft - padRight;
  const plotHeight = height - padTop - padBottom;

  const maxVal = Math.max(...data.map((d) => d.value));
  const niceMax = niceCeiling(maxVal);
  const tickCount = 4;
  const tickVals = Array.from({ length: tickCount + 1 }, (_, i) => Math.round((niceMax * i) / tickCount));

  const n = data.length;
  const slot = plotWidth / n;
  const barWidth = Math.max(2, Math.min(24, slot - 2));
  const barOffset = (slot - barWidth) / 2;
  const labelEvery = Math.max(1, Math.ceil(n / maxLabels));

  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="bar chart">`;

  tickVals.forEach((tv) => {
    const y = padTop + plotHeight - (niceMax > 0 ? (tv / niceMax) * plotHeight : 0);
    svg += `<line class="chart-gridline" x1="${padLeft}" x2="${width - padRight}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" />`;
    svg += `<text class="chart-axis-label" x="${padLeft - 6}" y="${(y + 3).toFixed(1)}" text-anchor="end">${formatValue(tv)}</text>`;
  });

  data.forEach((d, i) => {
    const x = padLeft + i * slot + barOffset;
    const barH = niceMax > 0 ? (d.value / niceMax) * plotHeight : 0;
    const y = padTop + plotHeight - barH;
    const path = roundedTopRectPath(x, y, barWidth, barH, 4);
    const isSelected = selectedKey != null && (d.key ?? d.label) === selectedKey;
    const stroke = isSelected ? `stroke="var(--text)" stroke-width="1.5"` : "";
    svg += `<path class="chart-bar${isSelected ? " selected" : ""}" data-index="${i}" fill="${color}" ${stroke} d="${path}"></path>`;
    if (i % labelEvery === 0 || i === n - 1) {
      svg += `<text class="chart-axis-label" x="${(x + barWidth / 2).toFixed(1)}" y="${height - 4}" text-anchor="middle">${chartEsc(d.label)}</text>`;
    }
  });

  svg += "</svg>";

  container.innerHTML = `<div class="chart-wrap">${svg}<div class="chart-tooltip"></div></div>`;

  const wrap = container.querySelector(".chart-wrap");
  const tooltip = wrap.querySelector(".chart-tooltip");

  wrap.querySelectorAll(".chart-bar").forEach((bar) => {
    const d = data[Number(bar.dataset.index)];
    const position = () => {
      const barRect = bar.getBoundingClientRect();
      const wrapRect = wrap.getBoundingClientRect();
      tooltip.style.left = `${barRect.left - wrapRect.left + barRect.width / 2}px`;
      tooltip.style.top = `${barRect.top - wrapRect.top}px`;
    };
    bar.addEventListener("mouseenter", () => {
      // Prefer a fuller label on hover than what's printed under the bar --
      // e.g. the x-axis reads "SEP" but a 12-month window can genuinely
      // contain two Septembers a year apart, so the tooltip disambiguates
      // with "September 2026" where a page supplies one.
      const label = d.tooltipLabel || d.label;
      tooltip.innerHTML = `<span class="tt-value">${formatValue(d.value)}</span> <span class="tt-label">${chartEsc(label)}</span>`;
      tooltip.classList.add("visible");
      position();
    });
    bar.addEventListener("mousemove", position);
    bar.addEventListener("mouseleave", () => tooltip.classList.remove("visible"));
    if (opts.onClick) bar.addEventListener("click", () => opts.onClick(d));
  });
}

// ---------------------------------------------------------------------
// Time-granularity controls (Day / Month / Year / All), shared by every
// chart that plots something against a date column. "All" is the full
// history bucketed by year (the default); each step in toward "Day"
// trades time range for finer buckets -- last 12 months by month, last
// 30 days by day, last 24 hours by hour -- the same drill-down pattern
// Last.fm's own charts use.
// ---------------------------------------------------------------------
const GRANULARITIES = {
  day: { label: "Day", rangeModifier: "-1 day", fmt: "%Y-%m-%d %H:00" },
  month: { label: "Month", rangeModifier: "-30 days", fmt: "%Y-%m-%d" },
  year: { label: "Year", rangeModifier: "-12 months", fmt: "%Y-%m" },
  all: { label: "All", rangeModifier: null, fmt: "%Y" },
};
const GRANULARITY_ORDER = ["day", "month", "year", "all"];

const MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

function bucketTickLabel(granularity, key) {
  if (!key) return key;
  if (granularity === "day") return key.slice(11, 16); // "HH:MM"
  if (granularity === "month") return `${Number(key.slice(8, 10))} ${MONTH_ABBR[Number(key.slice(5, 7)) - 1]}`; // "27 AUG"
  if (granularity === "year") return MONTH_ABBR[Number(key.slice(5, 7)) - 1] || key; // "YYYY-MM" -> "SEP"
  return key; // all -> "YYYY"
}

function humanBucketLabel(granularity, key) {
  if (!key) return key;
  // Day: the bucket already IS "now-ish", so a date prefix just added
  // noise -- the plain time reads clearly on its own.
  if (granularity === "day") return key.slice(11, 16); // "HH:MM"
  // Month: UK day-before-month order, matching the axis; no year, since
  // a 30-day window is never ambiguous about which year it's in.
  if (granularity === "month") return `${Number(key.slice(8, 10))} ${MONTH_ABBR[Number(key.slice(5, 7)) - 1]}`; // "27 AUG"
  if (granularity === "year") {
    try {
      return new Date(`${key}-01T00:00:00`).toLocaleDateString(undefined, { year: "numeric", month: "long" });
    } catch {
      return key;
    }
  }
  return key; // all -> "YYYY"
}

/** Renders the Day/Month/Year/All tabs plus an active-filter "clear" pill
 * into `container`, reading/writing `state.granularity` and
 * `state.periodFilter` ({key, label} | null) in place, calling
 * `onChange()` after either changes. */
function renderChartToolbar(container, state, onChange, { center = false } = {}) {
  const tabs = GRANULARITY_ORDER.map((g) => `
    <button class="gtab ${state.granularity === g ? "active" : ""}" data-g="${g}">${GRANULARITIES[g].label}</button>
  `).join("");
  const filterPill = state.periodFilter
    ? `<button class="clear-filter-pill">${chartEsc(state.periodFilter.label)} <span aria-hidden="true">&times;</span></button>`
    : "";

  container.innerHTML = `<div class="chart-toolbar${center ? " chart-toolbar--center" : ""}"><div class="granularity-tabs">${tabs}</div>${filterPill}</div>`;

  container.querySelectorAll(".gtab").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.granularity === btn.dataset.g) return;
      state.granularity = btn.dataset.g;
      state.periodFilter = null;
      onChange();
    });
  });
  const clearBtn = container.querySelector(".clear-filter-pill");
  if (clearBtn) clearBtn.addEventListener("click", () => { state.periodFilter = null; onChange(); });
}

/** A plain tab strip -- same look as the granularity tabs above, minus
 * the click-to-filter/clear-pill machinery -- for widgets that just need
 * a time-window switch (e.g. the home page's Most Played, Week/Month/
 * Year/All rather than charts.js's own Day/Month/Year/All). `windows` is
 * an object keyed by window id with a `.label`; `order` lists those ids
 * in display order. */
function renderTimeWindowTabs(container, windows, order, activeKey, onSelect, { center = false } = {}) {
  const tabs = order.map((k) => `
    <button class="gtab ${k === activeKey ? "active" : ""}" data-k="${k}">${chartEsc(windows[k].label)}</button>
  `).join("");
  container.innerHTML = `<div class="granularity-tabs">${tabs}</div>`;
  container.classList.toggle("tabs-only--center", center);
  container.querySelectorAll(".gtab").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.k === activeKey) return;
      onSelect(btn.dataset.k);
    });
  });
}

/** Toggles state.periodFilter for a clicked bar: selecting it, or
 * clearing it if the same bucket was already selected (click again to
 * deselect, same as pressing the clear pill). */
function toggleBucketFilter(state, granularity, bucketKey) {
  state.periodFilter = state.periodFilter && state.periodFilter.key === bucketKey
    ? null
    : { key: bucketKey, label: humanBucketLabel(granularity, bucketKey) };
}

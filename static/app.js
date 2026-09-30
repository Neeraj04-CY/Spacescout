// SpaceScout UI. Vanilla JS, no build step. All text is inserted with
// textContent (never innerHTML) so listing data and model output cannot inject markup.
"use strict";

const EXAMPLES = [
  "Quiet place for 4 people in Bandra tomorrow afternoon, fast wifi, under ₹600 per person per hour, ideally with a whiteboard",
  "Meeting room for 10 near Lower Parel on Friday from 2 to 5pm, must have a projector",
  "somewhere nice to work",
  "Meeting room for 50 people in Bandra under ₹200 per person per hour",
  "cabin for 3 in Juhu tomorrow morning",
  "kal dopahar Andheri mein 4 logon ke liye quiet meeting room",
];

const SPACE = { hot_desk: "Hot desk", meeting_room: "Meeting room", private_cabin: "Private cabin" };
const NOISE = { quiet: "Quiet", moderate: "Moderate", lively: "Lively" };
const AMEN = {
  whiteboard: "Whiteboard", projector: "Projector", tv_screen: "TV screen", video_conferencing: "Video conferencing",
  phone_booth: "Phone booth", coffee: "Coffee/tea", pantry: "Pantry", parking: "Parking", printer: "Printer",
  power_backup: "Power backup", access_24x7: "24x7 access", wheelchair_accessible: "Wheelchair accessible",
  standing_desk: "Standing desks", lockers: "Lockers", natural_light: "Natural light",
};
const inr = (n) => "₹" + Math.round(n).toLocaleString("en-IN");

// tiny DOM helper: h("div", {class: "x"}, "text", child, ...)
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);
const form = $("search-form"), input = $("q"), btn = $("submit");
const statusEl = $("status"), resultsEl = $("results");
let lastQuery = "";
let inflight = null;

// ---------------------------------------------------------------- init
function init() {
  $("examples").append(...EXAMPLES.map((q) => h("button", { type: "button", class: "chip", onclick: () => run(q) }, q.length > 60 ? q.slice(0, 57) + "…" : q)));
  input.addEventListener("input", () => { $("char-count").textContent = `${input.value.length} / 500`; });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
  form.addEventListener("submit", (e) => { e.preventDefault(); run(input.value); });
  $("dataset").addEventListener("toggle", loadDataset, { once: true });
  fetch("/api/health").then((r) => r.json()).then((hl) => {
    const pill = $("llm-status");
    if (hl.llm_configured) { pill.textContent = "LLM: " + hl.llm_model; pill.className = "pill pill-ok"; }
    else { pill.textContent = "LLM off · rule-based parser"; pill.className = "pill pill-warn"; pill.title = "Set LLM_API_KEY to enable the LLM parser"; }
  }).catch(() => {});
}

// ---------------------------------------------------------------- search
async function run(query) {
  query = (query || "").trim();
  if (!query) { input.focus(); return; }
  input.value = query; $("char-count").textContent = `${query.length} / 500`;
  lastQuery = query;
  if (inflight) inflight.abort();
  inflight = new AbortController();
  btn.disabled = true; btn.textContent = "Searching…";
  statusEl.replaceChildren(h("p", { class: "muted small" }, "Understanding your request and checking listings…"));
  resultsEl.replaceChildren(...[0, 1, 2].map(() => $("skeleton").content.cloneNode(true)));
  try {
    const res = await fetch("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }), signal: inflight.signal,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.details ? data.details.map((d) => d.message).join("; ") : `Server error (${res.status})`);
    render(data);
  } catch (err) {
    if (err.name === "AbortError") return;
    resultsEl.replaceChildren();
    statusEl.replaceChildren(h("div", { class: "banner banner-error", role: "alert" },
      h("p", { class: "q" }, "Search failed"),
      h("p", {}, err.message || "Could not reach the server."),
      h("button", { class: "btn-link", onclick: () => run(lastQuery) }, "Retry")));
  } finally {
    btn.disabled = false; btn.textContent = "Search";
  }
}

// ---------------------------------------------------------------- render
function render(d) {
  statusEl.replaceChildren();
  resultsEl.replaceChildren();

  // what the system understood
  const chips = d.interpretation.map((c) => h("span", { class: `chip chip-${c.kind}` }, c.label));
  statusEl.append(h("div", { class: "understood" },
    h("div", { class: "understood-head" },
      h("span", { class: "label" }, "Understood as"),
      h("span", { class: "legend" },
        h("span", {}, h("i", { style: "background:var(--accent-soft)" }), "must match"),
        h("span", {}, h("i", { style: "border:1px dashed var(--muted)" }), "preference"),
        h("span", {}, h("i", { style: "background:var(--warn-soft)" }), "can't filter"),
        h("span", { class: "src" }, "parser: " + d.trace.parser))),
    chips.length ? h("div", { class: "chip-row" }, chips) : h("p", { class: "muted small" }, "Nothing specific yet.")));

  // status banner
  if (d.status !== "ok" || d.clarifying_question) {
    const cls = d.status === "ok" ? "banner-info" : d.status === "needs_clarification" ? "banner-info" : "banner-warn";
    const sugg = (d.suggestions || []).map((s) => looksLikeQuery(s)
      ? h("button", { type: "button", class: "chip", onclick: () => run(s) }, s)
      : h("span", { class: "chip" }, s));
    statusEl.append(h("div", { class: `banner ${cls}` },
      d.message && h("p", {}, d.message),
      d.clarifying_question && h("p", { class: "q" }, d.clarifying_question),
      sugg.length && h("div", { class: "chip-row" }, sugg)));
  }

  if (d.results.length) {
    const exact = d.results.filter((r) => r.match === "exact").length;
    const heading = d.status === "needs_clarification" ? "Well-reviewed spaces while you decide (not tailored to you)"
      : exact ? `${d.total_exact_matches} match${d.total_exact_matches === 1 ? "" : "es"} · showing top ${exact}`
      : "Closest alternatives";
    resultsEl.append(h("div", { class: "results-head" }, h("span", { class: "label" }, heading),
      h("span", { class: "muted small" }, "Ranked by fit, trust, value and booking likelihood")));
    d.results.forEach((r, i) => resultsEl.append(card(r, i + 1, d.parsed)));
  } else if (d.status === "no_exact_match") {
    resultsEl.append(h("p", { class: "muted" }, "No listings to show. Adjust the request using the hints above."));
  }

  resultsEl.append(traceBlock(d.trace));
}

function looksLikeQuery(s) { return /\b(for|in|near)\b/i.test(s) && s.length > 25; }

function card(r, rank, parsed) {
  const l = r.listing;
  const alt = r.match !== "exact";
  const wanted = new Set([...(parsed.required_amenities || []), ...(parsed.preferred_amenities || [])]);
  const price = l.pricing === "per_seat"
    ? `${inr(l.price_per_hour)}/seat/hr`
    : `${inr(l.price_per_hour)}/hr` + (r.price_per_person_hour ? ` · ${inr(r.price_per_person_hour)}/person` : "");
  const rating = l.review_count ? `★ ${l.rating.toFixed(1)} (${l.review_count})` : "New · no reviews";

  const bars = [["Fit", r.score.fit], ["Trust", r.score.trust], ["Value", r.score.value], ["Conversion", r.score.conversion]]
    .flatMap(([k, v]) => [h("span", {}, k), h("span", { class: "bar" }, h("i", { style: `width:${v === null ? 0 : Math.round(v * 100)}%` })), h("span", { class: "muted" }, v === null ? "n/a" : Math.round(v * 100))]);

  return h("article", { class: "card" + (alt ? " card-alt" : "") },
    h("div", { class: "card-top" },
      h("div", { class: "rank", "aria-label": `Rank ${rank}` }, alt ? "~" : rank),
      h("div", { class: "card-title" },
        h("h3", {}, l.name),
        h("div", { class: "sub" }, `${SPACE[l.space_type]} · ${l.address}`)),
      h("div", { class: "score", title: "Match score (0-100)" }, h("b", {}, Math.round(r.score.total)), h("span", {}, alt ? "alternative" : "score"))),
    h("div", { class: "facts" },
      h("span", {}, h("span", { class: "k" }, "Seats"), l.capacity),
      h("span", {}, h("span", { class: "k" }, "Price"), price),
      h("span", {}, rating),
      h("span", {}, h("span", { class: "k" }, "Noise"), NOISE[l.noise_level]),
      h("span", {}, h("span", { class: "k" }, "Wi-Fi"), `${l.wifi_mbps} Mbps`),
      h("span", {}, l.instant_book ? "Instant book" : "Host approval"),
      r.available_slot && h("span", {}, h("span", { class: "k" }, "Free"), r.available_slot)),
    h("p", { class: "explain" }, r.explanation),
    h("div", { class: "cols" },
      h("div", {}, h("span", { class: "label" }, "Fits"), h("ul", { class: "list list-good" }, r.why.map((w) => h("li", {}, w)))),
      h("div", {},
        r.violations.length ? [h("span", { class: "label" }, "Doesn't meet"), h("ul", { class: "list list-bad" }, r.violations.map((w) => h("li", {}, w)))] : null,
        (() => { const t = r.tradeoffs.filter((x) => !r.violations.includes(x)); return t.length ? [h("span", { class: "label" }, "Trade-offs"), h("ul", { class: "list list-trade" }, t.map((w) => h("li", {}, w)))] : null; })())),
    h("div", { class: "chip-row amen" }, l.amenities.map((a) => h("span", { class: "chip" + (wanted.has(a) ? " hit" : "") }, AMEN[a] || a))),
    h("div", { class: "card-foot" },
      h("details", { class: "breakdown" }, h("summary", {}, "Why this rank"), h("div", { class: "bars" }, bars)),
      h("span", { class: "src", title: r.explanation_source }, r.explanation_source === "llm" ? "Explanation: LLM, grounding-checked" : "Explanation: from listing facts")));
}

function traceBlock(t) {
  const lines = [
    `request_id   ${t.request_id}`,
    `reference    ${t.reference_time}`,
    `parser       ${t.parser}${t.fallback_reason ? "  (fallback: " + t.fallback_reason + ")" : ""}`,
    `timings_ms   ${Object.entries(t.timings_ms || {}).map(([k, v]) => k + "=" + v).join("  ")}`,
    t.counts ? `counts       ${Object.entries(t.counts).map(([k, v]) => k + "=" + v).join("  ")}` : null,
    ...(t.llm_calls || []).map((c) => `llm_call     ${c.purpose}: ${c.model} ${c.mode} attempts=${c.attempts} ${c.latency_ms}ms tokens=${c.prompt_tokens ?? "?"}/${c.completion_tokens ?? "?"} → ${c.outcome}`),
  ].filter(Boolean);
  return h("details", { class: "trace" }, h("summary", {}, "How this was computed"), h("pre", {}, lines.join("\n")));
}

// ---------------------------------------------------------------- dataset
async function loadDataset() {
  const body = $("dataset-body");
  try {
    const rows = await (await fetch("/api/listings")).json();
    const cols = ["ID", "Name", "Type", "Area", "Seats", "Price/hr", "Noise", "Wi-Fi", "Rating", "Amenities"];
    body.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, c)))),
      h("tbody", {}, rows.map((l) => h("tr", {},
        h("td", {}, l.id), h("td", {}, l.name), h("td", {}, SPACE[l.space_type]), h("td", {}, l.area.replace("_", " ")),
        h("td", {}, l.capacity), h("td", {}, inr(l.price_per_hour) + (l.pricing === "per_seat" ? "/seat" : "")),
        h("td", {}, NOISE[l.noise_level]), h("td", {}, l.wifi_mbps), h("td", {}, l.review_count ? `${l.rating} (${l.review_count})` : "new"),
        h("td", { class: "wrap-cell" }, l.amenities.map((a) => AMEN[a] || a).join(", ")))))));
  } catch {
    body.replaceChildren(h("p", { class: "muted small" }, "Could not load listings."));
  }
}

init();

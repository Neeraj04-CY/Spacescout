// Record the evaluation queries against a deployed SpaceScout server, from a browser tab
// open on that server (same origin, so no CORS or proxy is involved).
//
// Usage in the browser console:
//   startRecording(QUERIES)            // QUERIES = [{id, query}, ...] from eval/queries.json
//   window.__rec.done / window.__rec.records   // poll; then copy JSON.stringify(window.__rec.records)
// Then:  python -m eval.run_eval --parser llm --replay recorded.json
//
// Queries are spaced out to stay under free-tier LLM token limits. Each returned listing
// is compared with /api/listings so the replay can verify results come from the dataset.
async function startRecording(queries, { gapMs = 25000, referenceTime = "2026-10-05T10:00:00+05:30" } = {}) {
  const rec = (window.__rec = { done: false, records: [], error: null });
  try {
    const health = await fetch("/api/health").then((r) => r.json());
    const dataset = Object.fromEntries((await fetch("/api/listings").then((r) => r.json())).map((l) => [l.id, JSON.stringify(l)]));
    for (const [i, { id, query }] of queries.entries()) {
      if (i) await new Promise((r) => setTimeout(r, gapMs));
      const t0 = performance.now();
      const res = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, parser: "llm", reference_time: referenceTime }),
      });
      const latency_ms = Math.round(performance.now() - t0);
      const body = await res.json();
      if (!res.ok) { rec.records.push({ id, http_status: res.status, error: body, latency_ms }); continue; }
      const t = body.trace || {};
      rec.records.push({
        id, latency_ms, origin: location.origin, version: health.version,
        response: {
          ...body,
          trace: { parser: t.parser, fallback_reason: t.fallback_reason, llm_calls: t.llm_calls || [], timings_ms: t.timings_ms },
          results: body.results.map(({ listing, ...rest }) => ({
            ...rest, listing_id: listing.id, dataset_equal: dataset[listing.id] === JSON.stringify(listing),
          })),
        },
      });
    }
  } catch (e) {
    rec.error = String(e);
  }
  rec.done = true;
  return rec;
}

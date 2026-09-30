"""Evaluation runner.

Runs every query in eval/queries.json through the real SearchService and checks
machine-verifiable expectations (parsed fields, outcome status, that exact results
honour every hard constraint, and that results are grounded in the dataset).

Usage:
    python -m eval.run_eval --parser rules          # offline, no API key needed
    python -m eval.run_eval --parser llm            # needs LLM_API_KEY / GROQ_API_KEY

Free-tier LLM plans have low per-minute token limits (Groq free tier: 8,000 TPM
for gpt-oss-20b), and one search uses two LLM calls. In --parser llm mode the
runner therefore paces queries (--delay) and, if a query was rate-limited into
the rule-based fallback, waits (--cooldown) and re-runs it. Rows that still fell
back are marked, because they measure the rule parser, not the LLM.
Writes eval/results_<parser>.md and eval/results_<parser>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from app.config import Settings, get_settings
from app.core.models import SearchRequest, SearchResponse
from app.data.repository import load_listings
from app.services.explainer import FactSheet, check_grounding
from app.services.llm_client import LLMClient
from app.services.search import SearchService

HERE = Path(__file__).parent
REFERENCE_TIME = "2026-10-05T10:00:00+05:30"  # Monday 10:00 IST, fixed for reproducibility
MARK = {"exact": "", "alternative": "~", "suggestion": "*"}


def check(case: dict, r: SearchResponse, data: dict) -> tuple[list[str], int, int, list[str]]:
    """Return (failures, parse_fields_ok, parse_fields_total, parse_misses)."""
    e = case["expect"]
    fails: list[str] = []
    misses: list[str] = []
    ok_fields = total_fields = 0

    if r.status not in e["status"]:
        fails.append(f"status {r.status}, expected {'/'.join(e['status'])}")

    # ---- parsing ------------------------------------------------------------
    from app.services.normalize import resolve_location  # local import keeps module import light
    areas, *_ = resolve_location(r.parsed.location_text)
    if "areas" in e:
        total_fields += 1
        if sorted(areas) == sorted(e["areas"]):
            ok_fields += 1
        else:
            misses.append(f"areas={areas}")
    if "areas_include" in e:
        total_fields += 1
        if set(e["areas_include"]) <= set(areas):
            ok_fields += 1
        else:
            misses.append(f"areas={areas}")
    for k, v in e.get("parse", {}).items():
        total_fields += 1
        got = getattr(r.parsed, k)
        if isinstance(v, list):
            same = sorted(got) == sorted(v)
        elif isinstance(v, (int, float)) and got is not None and not isinstance(v, bool):
            same = abs(float(got) - float(v)) < 1e-6
        else:
            same = got == v
        if same:
            ok_fields += 1
        else:
            misses.append(f"{k}={got!r}")
    if misses:
        fails.append("parse: " + ", ".join(misses))

    # ---- outcome ------------------------------------------------------------
    exact = [it for it in r.results if it.match == "exact"]
    rc = e.get("results")
    if rc:
        for it in exact:
            l = it.listing
            if "area_in" in rc and l.area not in rc["area_in"]:
                fails.append(f"{l.id} outside requested area")
            if "min_capacity" in rc and l.capacity < rc["min_capacity"]:
                fails.append(f"{l.id} too small")
            if "space_type" in rc and l.space_type != rc["space_type"]:
                fails.append(f"{l.id} wrong space type")
            if "max_cost" in rc and (it.cost_in_budget_unit is None or it.cost_in_budget_unit > rc["max_cost"] + 1e-6):
                fails.append(f"{l.id} over budget")
            for a in rc.get("has_amenities", []):
                if a not in l.amenities:
                    fails.append(f"{l.id} lacks {a}")
    if "top_id" in e and r.status == "ok" and (not exact or exact[0].listing.id != e["top_id"]):
        fails.append(f"top result {exact[0].listing.id if exact else None}, expected {e['top_id']}")
    if "exact_count" in e and r.total_exact_matches != e["exact_count"]:
        fails.append(f"{r.total_exact_matches} exact matches, expected {e['exact_count']}")
    if e.get("results_empty") and r.results:
        fails.append("returned results, expected none")
    if e.get("question") and not r.clarifying_question:
        fails.append("no clarifying question")
    if e.get("only_suggestions") and any(it.match != "suggestion" for it in r.results):
        fails.append("ranked results for a vague query")
    if "message_contains" in e and e["message_contains"].lower() not in (r.message or "").lower():
        fails.append(f"message lacks '{e['message_contains']}'")
    if "alternatives_mention" in e and not all(e["alternatives_mention"] in " ".join(it.violations) for it in r.results):
        fails.append("alternatives don't state the location trade-off")
    for u in e.get("unsupported_contains", []):
        if not any(u in x for x in r.parsed.unsupported_requests):
            fails.append(f"'{u}' not flagged as unsupported")

    # ---- grounding (checked for every query) ---------------------------------
    for it in r.results:
        if data.get(it.listing.id) != it.listing:
            fails.append(f"GROUNDING: {it.listing.id} differs from dataset")
        if it.explanation_source == "llm":
            reason = check_grounding(it.explanation, FactSheet(it.listing.id, it.listing.name, it.why, it.tradeoffs))
            if reason:
                fails.append(f"GROUNDING: {it.listing.id} explanation {reason}")
    return fails, ok_fields, total_fields, misses


async def main(parser: str, delay: float, cooldown: float, max_retries: int) -> None:
    settings: Settings = get_settings()
    if parser == "llm" and not settings.llm_enabled:
        raise SystemExit("Set LLM_API_KEY (or GROQ_API_KEY) to run the LLM evaluation.")
    svc = SearchService(settings, load_listings(), LLMClient(settings))
    data = {l.id: l for l in load_listings()}
    cases = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))

    rows, raw = [], []
    ok_fields = total_fields = 0
    latencies, fallbacks, llm_tokens, rate_limit_retries = [], 0, 0, 0
    for i, case in enumerate(cases):
        if i and delay:
            await asyncio.sleep(delay)
        for attempt in range(max_retries + 1):
            t = time.perf_counter()
            r = await svc.search(SearchRequest(query=case["query"], reference_time=REFERENCE_TIME, parser=parser))
            elapsed = (time.perf_counter() - t) * 1000
            reason = r.trace.get("fallback_reason", "")
            if parser == "llm" and "429" in reason and attempt < max_retries:
                rate_limit_retries += 1
                print(f"{case['id']}: rate-limited, cooling down {cooldown:.0f}s before retrying")
                await asyncio.sleep(cooldown)
                continue
            break
        latencies.append(elapsed)
        print(f"{case['id']}: {r.trace.get('parser')} {r.status} ({elapsed:.0f} ms)")
        fails, okf, totf, _ = check(case, r, data)
        ok_fields += okf
        total_fields += totf
        fallbacks += "fallback" in r.trace.get("parser", "")
        llm_tokens += sum((c.get("prompt_tokens") or 0) + (c.get("completion_tokens") or 0) for c in r.trace.get("llm_calls", []))
        top = ", ".join(f"{it.listing.id}{MARK[it.match]}" for it in r.results[:3]) or "—"
        rows.append((case, r, fails, okf, totf, top))
        raw.append({"id": case["id"], "query": case["query"], "status": r.status, "parser": r.trace.get("parser"),
                    "fallback_reason": r.trace.get("fallback_reason"), "latency_ms": round(elapsed),
                    "llm_calls": [{k: c.get(k) for k in ("purpose", "mode", "attempts", "latency_ms", "outcome", "errors")} for c in r.trace.get("llm_calls", [])],
                    "parsed": r.parsed.model_dump(), "results": [it.listing.id for it in r.results], "failures": fails,
                    "message": r.message, "question": r.clarifying_question})

    passed = sum(1 for _, _, f, *_ in rows if not f)
    by_cat: dict[str, list[int]] = {}
    for case, _, f, *_ in rows:
        by_cat.setdefault(case["category"], [0, 0])
        by_cat[case["category"]][1] += 1
        by_cat[case["category"]][0] += not f
    grounding_fail = sum(1 for _, _, f, *_ in rows if any("GROUNDING" in x for x in f))

    lines = [
        f"# Evaluation results: `{parser}` parser",
        "",
        f"Reference time: {REFERENCE_TIME} (Monday). Model: {settings.llm_model if parser != 'rules' else 'n/a (rule-based)'}. "
        f"Generated by `python -m eval.run_eval --parser {parser}`.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Queries passing all checks | {passed}/{len(rows)} |",
        *[f"| {cat} queries passing | {p}/{n} |" for cat, (p, n) in by_cat.items()],
        f"| Parsed fields correct | {ok_fields}/{total_fields} ({100 * ok_fields / max(total_fields, 1):.0f}%) |",
        f"| Grounding violations (result not in dataset, or LLM text with unsupported facts) | {grounding_fail} |",
        *([f"| Queries actually parsed by the LLM | {len(rows) - fallbacks}/{len(rows)} |",
           f"| Rate-limit cool-downs used | {rate_limit_retries} |"] if parser != "rules" else []),
        f"| LLM parser fallbacks to rules | {fallbacks} |",
        f"| Latency per query, median / max (ms) | {statistics.median(latencies):.0f} / {max(latencies):.0f} |",
        *([f"| Total LLM tokens (all queries) | {llm_tokens} |"] if parser != "rules" else []),
        "",
        *([f"> **Warning:** {fallbacks} quer{'y' if fallbacks == 1 else 'ies'} fell back to the rule parser. "
           "Those rows measure the rule parser, not the LLM. Re-run with a longer `--delay`.", ""]
          if parser == "llm" and fallbacks else []),
        "## Per-query results",
        "",
        "`~` = alternative (misses at least one hard constraint), `*` = untailored suggestion shown with a clarifying question. Parsed = expected fields extracted correctly.",
        "",
        "| ID | Type | Query | Parser | Status | Parsed | Top results | Pass | What failed |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for case, r, fails, okf, totf, top in rows:
        q = case["query"].replace("|", "\\|")
        used = "rules (fallback)" if "fallback" in r.trace.get("parser", "") else ("llm" if r.trace.get("parser", "").startswith("llm") else "rules")
        lines.append(f"| {case['id']} | {case['category']} | {q} | {used} | {r.status} | {okf}/{totf} | {top} | {'✅' if not fails else '❌'} | {'; '.join(fails) or ''} |")
    (HERE / f"results_{parser}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (HERE / f"results_{parser}.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("\n".join(lines[:16]))
    print(f"\nwrote eval/results_{parser}.md")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--parser", choices=["rules", "llm", "auto"], default="auto")
    ap.add_argument("--delay", type=float, default=None, help="seconds between queries (default: 30 for llm, 0 otherwise)")
    ap.add_argument("--cooldown", type=float, default=65.0, help="seconds to wait after a rate-limited query before retrying it")
    ap.add_argument("--max-retries", type=int, default=2, help="re-runs per query after a rate-limit fallback")
    a = ap.parse_args()
    d = a.delay if a.delay is not None else (30.0 if a.parser == "llm" else 0.0)
    asyncio.run(main(a.parser, d, a.cooldown, a.max_retries))

"""LLM failure handling, using an in-process mock of the chat-completions API."""

import json

import httpx

from app.data.repository import load_listings
from tests.helpers import chat_response, mock_service, parsed_payload, run_search

EXAMPLE = "Quiet place for 4 people in Bandra tomorrow afternoon, fast wifi, under ₹600 per person per hour, ideally with a whiteboard"
GOOD = parsed_payload(location_text="Bandra", party_size=4, budget_amount=600, budget_unit="per_person_per_hour",
                      day_kind="tomorrow", time_of_day="afternoon", prefer_quiet=True, prefer_fast_wifi=True,
                      preferred_amenities=["whiteboard"])


def _router(parse_responses, explain_response=None):
    """Return a handler that serves parse calls from a queue and explain calls from a fixed response."""
    calls = {"parse": 0, "explain": 0, "bodies": []}
    queue = list(parse_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls["bodies"].append(body)
        is_explain = "explanations" in json.dumps(body.get("response_format", {})) or "short explanations" in body["messages"][0]["content"]
        if is_explain:
            calls["explain"] += 1
            if explain_response is None:
                return httpx.Response(503)
            return explain_response(body)
        calls["parse"] += 1
        item = queue.pop(0) if queue else queue_fallback
        return item(body) if callable(item) else item

    queue_fallback = httpx.Response(503)
    return handler, calls


def test_llm_parse_happy_path_uses_llm_and_strict_schema():
    handler, calls = _router([chat_response(GOOD)])
    r = run_search(mock_service(handler, explain_with_llm=False), EXAMPLE)
    assert r.trace["parser"].startswith("llm:")
    assert r.status == "ok" and r.results[0].listing.id == "L001"
    rf = calls["bodies"][0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True


def test_malformed_json_falls_back_to_rules():
    bad = chat_response("this is not json {")
    handler, _ = _router([bad, bad, bad, bad, bad, bad])
    r = run_search(mock_service(handler, explain_with_llm=False), EXAMPLE)
    assert r.trace["parser"] == "rules (fallback)"
    assert "LLMBadOutput" in r.trace["fallback_reason"]
    assert r.status == "ok"  # rules parser still answers the query


def test_rate_limit_then_success_retries():
    handler, calls = _router([httpx.Response(429, headers={"retry-after": "0"}), chat_response(GOOD)])
    r = run_search(mock_service(handler, explain_with_llm=False), EXAMPLE)
    assert r.trace["parser"].startswith("llm:")
    assert r.trace["llm_calls"][0]["attempts"] == 2


def test_strict_schema_rejected_degrades_to_json_mode():
    handler, calls = _router([httpx.Response(400, json={"error": "schema not supported"}), chat_response(GOOD)])
    r = run_search(mock_service(handler, explain_with_llm=False), EXAMPLE)
    assert r.trace["parser"].startswith("llm:")
    assert calls["bodies"][1]["response_format"] == {"type": "json_object"}


def test_auth_failure_falls_back_without_retry():
    handler, calls = _router([httpx.Response(401)])
    r = run_search(mock_service(handler, explain_with_llm=False), EXAMPLE)
    assert r.trace["parser"] == "rules (fallback)" and calls["parse"] == 1


def test_timeout_falls_back():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)
    r = run_search(mock_service(handler, explain_with_llm=False), EXAMPLE)
    assert r.trace["parser"] == "rules (fallback)"


def test_schema_invalid_output_gets_one_repair_attempt():
    handler, calls = _router([chat_response({**GOOD, "party_size": -4}), chat_response(GOOD)])
    r = run_search(mock_service(handler, explain_with_llm=False), EXAMPLE)
    assert r.trace["parser"].startswith("llm:") and calls["parse"] == 2
    assert r.parsed.party_size == 4


def test_parse_cache_avoids_second_call():
    handler, calls = _router([chat_response(GOOD)])
    svc = mock_service(handler, explain_with_llm=False)
    run_search(svc, EXAMPLE)
    r2 = run_search(svc, EXAMPLE.upper())
    assert calls["parse"] == 1 and "(cached)" in r2.trace["parser"]


def test_llm_explanation_with_invented_fact_is_rejected():
    def explain_resp(body):
        items = json.loads(body["messages"][1]["content"])
        out = [{"id": it["id"], "text": "Great room with a rooftop pool and a projector."} for it in items]
        out[1]["text"] = "Quiet space."  # faithful for the second result (L002 is quiet)
        return chat_response({"items": out})

    handler, _ = _router([chat_response(GOOD)], explain_response=explain_resp)
    r = run_search(mock_service(handler), EXAMPLE)
    srcs = [it.explanation_source for it in r.results]
    assert srcs[0].startswith("template (LLM text rejected")
    assert srcs[1] == "llm" and r.results[1].explanation == "Quiet space."


def test_explain_failure_keeps_template():
    handler, _ = _router([chat_response(GOOD)], explain_response=None)  # explain returns 503
    r = run_search(mock_service(handler), EXAMPLE)
    assert all(it.explanation_source == "template" for it in r.results)


def test_results_are_verbatim_dataset_records():
    handler, _ = _router([chat_response(GOOD)])
    r = run_search(mock_service(handler, explain_with_llm=False), EXAMPLE)
    data = {l.id: l for l in load_listings()}
    for it in r.results:
        assert it.listing == data[it.listing.id]

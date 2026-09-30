"""Shared helpers for tests (plain functions, no fixtures needed)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.core.models import SearchRequest
from app.data.repository import load_listings
from app.services.llm_client import LLMClient
from app.services.search import SearchService

REF = "2026-10-05T10:00:00+05:30"  # a Monday; "tomorrow" = Tue 6 Oct
NOW = dt.datetime(2026, 10, 5, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def offline_service() -> SearchService:
    s = Settings(llm_api_key=None, _env_file=None)
    return SearchService(s, load_listings(), LLMClient(s))


def mock_service(handler, **settings_overrides) -> SearchService:
    s = Settings(llm_api_key="test-key", _env_file=None, **settings_overrides)
    return SearchService(s, load_listings(), LLMClient(s, transport=httpx.MockTransport(handler)))


def run_search(svc: SearchService, query: str, **kw):
    return asyncio.run(svc.search(SearchRequest(query=query, reference_time=REF, **kw)))


def chat_response(content: dict | str, status: int = 200) -> httpx.Response:
    body = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(status, json={
        "choices": [{"message": {"role": "assistant", "content": body}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 120},
    })


def parsed_payload(**fields) -> dict:
    base = {
        "location_text": None, "party_size": None, "space_type": None, "budget_amount": None, "budget_unit": None,
        "day_kind": None, "weekday": None, "date": None, "time_of_day": None, "start_time": None, "end_time": None,
        "duration_hours": None, "required_amenities": [], "preferred_amenities": [], "prefer_quiet": False,
        "prefer_fast_wifi": False, "min_rating": None, "unsupported_requests": [], "off_topic": False,
    }
    base.update(fields)
    return base

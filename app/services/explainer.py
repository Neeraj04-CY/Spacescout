"""Result explanations with a grounding guarantee.

1. Code builds a fact sheet per result (what fits, what the trade-off is) from
   listing data only.
2. A template turns facts into 1-2 sentences. This alone satisfies the
   requirement and is always available.
3. Optionally, one batched LLM call rewrites the facts into more natural prose.
   Every LLM sentence is checked: any number, amenity or noise claim that is not
   in that listing's fact sheet causes the LLM text to be rejected and the
   template text to be used instead. The LLM never sees the raw user query, so
   injected instructions in the query cannot reach this step.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.vocab import AMENITIES
from app.services.llm_client import CallMeta, LLMClient, LLMError

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_NOISE_CLAIMS = ["quiet", "silent", "peaceful", "calm", "noisy", "lively", "buzzing"]


@dataclass
class FactSheet:
    listing_id: str
    name: str
    why: list[str]
    tradeoffs: list[str]

    def text(self) -> str:
        return " ".join([self.name, *self.why, *self.tradeoffs]).lower()


def _lc_first(s: str) -> str:
    """Lower-case the first letter unless it starts an acronym-like token (Wi-Fi, TV, BKC)."""
    first = s.split(" ", 1)[0]
    if len(first) > 1 and (first[1].isupper() or "-" in first and first[0].isupper() and any(c.isupper() for c in first[1:])):
        return s
    return s[:1].lower() + s[1:]


def template_explanation(f: FactSheet) -> str:
    why = f.why[:3]
    first = "; ".join([why[0], *(_lc_first(w) for w in why[1:])]) if why else "Meets your stated requirements"
    s = first[0].upper() + first[1:] + "."
    if f.tradeoffs:
        t = f.tradeoffs[0]
        s += f" Trade-off: {_lc_first(t)}."
    else:
        s += " No notable trade-offs for your request."
    return s


def _numbers(s: str) -> set[str]:
    out = set()
    for n in _NUM_RE.findall(s):
        v = n.replace(",", "")
        try:
            out.add(str(float(v)))
        except ValueError:
            pass
    return out


def check_grounding(text: str, facts: FactSheet) -> str | None:
    """Return None if `text` only uses facts from the sheet, else the reason for rejection."""
    if not text or len(text) > 300:
        return "empty or too long"
    ft = facts.text()
    extra_nums = _numbers(text) - _numbers(ft)
    if extra_nums:
        return f"number(s) not in facts: {', '.join(sorted(extra_nums))}"
    tl = text.lower()
    for key, (label, syns) in AMENITIES.items():
        for term in [label.lower(), *syns]:
            if len(term) < 3:
                continue
            if re.search(rf"\b{re.escape(term)}\b", tl) and not re.search(rf"\b{re.escape(term)}\b", ft):
                return f"mentions '{term}' which is not in the facts"
    for w in _NOISE_CLAIMS:
        if re.search(rf"\b{w}\b", tl) and not re.search(rf"\b{w}\b", ft):
            return f"noise claim '{w}' not in the facts"
    return None


EXPLAIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
            },
        }
    },
    "required": ["items"],
}

EXPLAIN_PROMPT = """You write short explanations for coworking search results, shown under each result card.
For each item write one or two sentences, at most 35 words in total:
first the strongest reason it fits, then only the single most important trade-off (skip it if there are none).
Use ONLY the facts given for that item. Do not add amenities, prices, numbers, ratings, distances or adjectives
that are not in its facts. Keep numbers exactly as written. Do not start with "It" and do not mention other items.
Plain text, no markdown. Return JSON: {"items": [{"id": "...", "text": "..."}]}."""


async def explain(
    sheets: list[FactSheet], client: LLMClient | None, use_llm: bool
) -> tuple[dict[str, tuple[str, str]], CallMeta | None]:
    """Return {listing_id: (text, source)} and the LLM call metadata (if any)."""
    result = {f.listing_id: (template_explanation(f), "template") for f in sheets}
    if not (use_llm and client and client.enabled and sheets):
        return result, None
    payload = [{"id": f.listing_id, "fits": f.why, "tradeoffs": f.tradeoffs} for f in sheets]
    try:
        raw, meta = await client.chat_json(
            purpose="explain",
            messages=[{"role": "system", "content": EXPLAIN_PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            schema=EXPLAIN_SCHEMA,
            schema_name="explanations",
            max_tokens=1500,
        )
    except LLMError:
        return result, None  # meta already logged by the client; template stays
    by_id = {f.listing_id: f for f in sheets}
    rejected = 0
    for item in raw.get("items", []) if isinstance(raw.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        lid, text = item.get("id"), str(item.get("text", "")).strip()
        if lid not in by_id:
            continue
        reason = check_grounding(text, by_id[lid])
        if reason is None:
            result[lid] = (text, "llm")
        else:
            rejected += 1
            result[lid] = (result[lid][0], f"template (LLM text rejected: {reason})")
    meta.outcome = f"ok, {rejected} rejected by grounding check" if rejected else meta.outcome
    return result, meta

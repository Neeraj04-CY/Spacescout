# Design note

## 1. Approach

A search request goes through six steps, each with one job:

```
parse (LLM, rules fallback) → normalise (code) → decide: search or clarify (code)
→ match hard constraints (code) → rank (code) → explain (code facts, optional LLM phrasing)
```

The core decision is **what the LLM is allowed to do**. It turns free text into a fixed schema. It never sees the listings, never computes anything, and never decides what is shown. That one rule is what makes the honesty requirement enforceable, not just hoped for.

This is deliberately not an agent: the flow is fixed and each step runs once, so an agent loop would add latency and failure modes without adding capability.

## 2. Splitting work between the LLM and regular code

| Concern | Owner | Reasoning |
|---|---|---|
| **Parsing** | LLM | Users write "4 of us", "kal dopahar", "meetng room fr 6", "ideally a whiteboard". Mapping that to fields is a language problem, and it is exactly where the rule-based parser fails in the evaluation. The output is constrained by a strict JSON schema: every amenity must be one of 15 known keys, and anything else goes into `unsupported_requests` so the user sees "Can't filter on: sea view" instead of silence. Pydantic re-validates the output. One repair retry is allowed, then the system falls back to the rule parser. |
| **Normalisation** | Code | The LLM outputs `day_kind: "tomorrow"`, `time_of_day: "afternoon"`, not dates. Code turns that into *Tue 06 Oct, 12:00–17:00, 2-hour slot* and states the assumption on screen. The same applies to location aliases, typos (fuzzy match), "near X" (areas within 5 km) and budget units. ₹600 *per person* for a ₹1,800/hr room booked by 4 people is ₹450: that is arithmetic, not language. |
| **Filtering** | Code | Hard constraints (area, capacity, budget, time, space type, "must-have" amenities) are checked against every listing. Each miss is recorded as a costed violation, for example *"₹450/person/hr: ₹150 over your ₹300/person/hr"* or *"In Khar, 1.6 km from Bandra"*. |
| **Ranking** | Code | Explicit weights (below), deterministic, and each result's score breakdown is shown in the UI ("Why this rank"). |
| **Explanation** | Code builds a fact sheet; the LLM may rephrase it | The template alone meets the requirement: *"Quiet space; has whiteboard; seats 4, enough for your 4. Trade-off: Wi-Fi is only 50 Mbps."* When the LLM rewrites it, each sentence is checked. Any number, amenity or noise word not in that listing's facts gets the text rejected and the template used. The explainer never receives the user's raw text, so a prompt injection in the query cannot reach it. |

**Hard vs soft.** Location, capacity, budget, time and explicit "must/need" amenities are hard. "Ideally", "preferably" and unqualified mentions ("quiet", "fast wifi", "whiteboard") are soft: they affect rank, never eligibility. The UI shows the split as chips: filled for must-match, dashed for preferences, amber for things we can't filter on.

**Messy cases.**

- *Vague* ("somewhere nice to work"): ask where, how many and when. Show three well-reviewed spaces, labelled as not tailored.
- *Off-topic* or *outside Mumbai*: say so and ask. Nothing is searched.
- *Nothing matches*: show the closest alternatives, ordered by how badly they miss, each stating the miss. Report the single relaxation that unlocks the most options, for example *"Everything else fits 4 spaces, but the cheapest is ₹450/person/hr against your ₹300/person/hr"*, or *"No space in our listings fits 50 people; the largest fits 40"*.
- *Fewer than three exact matches*: add labelled alternatives.

## 3. Ranking, from a marketplace's point of view

`score = 0.45·fit + 0.25·trust + 0.15·value + 0.15·conversion` (with no soft preferences, fit is dropped and the other three are re-weighted).

- **Fit**: the share of soft preferences met. Quiet = 1, moderate = 0.4. Wi-Fi is scaled up to 100 Mbps.
- **Trust**: a Bayesian-average rating with 10 pseudo-reviews at the market mean, so 5.0★ from 3 reviews ranks below 4.8★ from 96. That protects the booker, and it is the same reason marketplaces don't sort by raw average.
- **Value**: price relative to the user's budget, or to the market median if no budget is given. It is deliberately small: cheapest-first is not what bookers want, but ignoring price drifts toward expensive listings.
- **Conversion**: instant-book, and whether the room is right-sized. Four people in a 20-seat room is a poor experience and a likely cancellation.

There is no paid boosting. A sponsored slot would be a separate, labelled row, never mixed into the score.

## 4. Model choice

Default: **`openai/gpt-oss-20b` served by Groq**, through any OpenAI-compatible endpoint (`LLM_BASE_URL`, `LLM_MODEL`).

- **Guaranteed schema.** Groq's docs list gpt-oss-20b among the models supporting *strict* JSON-schema structured output, using constrained decoding. Parse failures become a provider bug, not a normal case. If a provider rejects the schema, the client drops to JSON mode, and validation plus fallback still apply.
- **Size fits the task.** Extraction into a 19-field schema does not need a frontier model. A small model on fast inference hardware keeps the parse step cheap, and `reasoning_effort=low` limits hidden reasoning tokens.
- **Replaceable.** Only `llm_client.py` knows about HTTP. Switching to OpenAI or a local model is a config change.

Temperature is 0. Parsed queries are cached (LRU keyed on the normalised text), so repeated searches cost nothing.

## 5. What changes at 100,000 listings

Today every listing is evaluated in Python on each request. That is fine for 40 listings and wrong for 100,000.

1. **Storage and filtering in the database.** Move to Postgres, with indexes on `(city, area, space_type, capacity)` and price, and PostGIS for real distance ("near Bandra" becomes a radius query, not an alias table). Hard constraints become a `WHERE` clause that returns a candidate set of hundreds, not 100k rows.
2. **Availability from the booking system.** Query a calendar service for the candidates only, with a short-TTL cache, and always re-check at booking time.
3. **Relaxation as facet counts.** "What if budget were dropped?" becomes a `COUNT` per constraint in one query, or a search-engine facet (OpenSearch/Elasticsearch) instead of re-running the matcher.
4. **Two-stage ranking.** Rules and SQL retrieve roughly the top 500. A learned ranker then re-orders them, trained on logged searches, clicks and completed bookings, which is where conversion signal actually comes from. The hand-set weights become features and a baseline.
5. **Embeddings earn a place.** With real descriptions and reviews, preferences like "good for podcast recording" or "feels premium" can't be mapped to 15 amenity keys. Embed listing text into a vector index (pgvector is enough at this size) and use semantic similarity as one ranking feature, not as a filter, so it can never override a hard constraint.
6. **LLM cost stays flat.** The LLM never sees listings, so its cost doesn't grow with inventory. Add rate limits and a provider fallback chain.
7. **Continuous evaluation.** Run a labelled sample of production queries in CI on every prompt or model change. Track zero-result rate and search-to-booking conversion.

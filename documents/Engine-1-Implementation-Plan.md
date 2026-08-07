# Engine 1 — Find Opportunities: Implementation Plan

Derived from `Engine-1-Find-Opportunities-Design-Document.md`. Full phase-by-phase build plan: what to build, in what order, with what files, and what "done" looks like.

---

## 0. Pre-build: decisions needed from client (blocking)

- [ ] **Category taxonomy** (§2.2, §16.1) — client-supplied URL-pattern mapping. Blocks column 3 and page-type classification fallback.
- [ ] **Estimated impact method** (§10.2) — confirm position→CTR curve approach and whether to fit it to client's own GSC data.
- [ ] **Priority score → P0** (§2.1) — confirm promotion from optional to required column.
- [ ] **Intent + Confidence columns** (§2.1) — confirm in/out at P1.
- [ ] **Competitor lists per market** (§16.2) — client to confirm/override DataForSEO-suggested list.
- [ ] **Seed keywords per market** (§16.3) — to anchor keyword research per market.
- [ ] **GSC + GA4 access and auth model** (§16.4) — service account vs OAuth refresh token.
- [ ] **Run cadence and market rollout order** (§16.6).
- [ ] **`value_per_conversion`** (§16.7) — needed before estimated impact can show revenue, not just clicks.

---

## Phase 1 — Foundations

**Goal:** Data flowing in, visible in Django admin. No clustering, no scoring, no sheet yet.

### Build order
1. `django-admin startproject config` + `apps/` structure per §5:
   `apps/clients`, `apps/connectors`, `apps/ingestion`, `apps/topics`, `apps/pages`, `apps/opportunities`, `apps/exports`, `apps/runs`, `apps/api`
2. Settings split: `config/settings/{base,local,production}.py`, `config/celery.py`, `django-environ` for config (§4.3)
3. Core models (`apps/clients/models.py`): `Client`, `Market`, `Competitor` (§6.1)
4. Config models: `EngineSettings`, `ScoringWeights` (§6.2) + `SettingsResolver` service (market override → client default → code default)
5. Run tracking (`apps/runs/models.py`): `Run`, `RunStage` (§6.6)
6. Connector base: `apps/connectors/base.py` — `Connector` ABC, single method `fetch() -> RawFetch`
7. `RawFetch` model (`apps/ingestion/models.py`) with `request_hash` index for cache-window reuse (§6.3)
8. DataForSEO connector (`apps/connectors/dataforseo/`): client wrapper, Pydantic schemas for keyword_ideas, domain_intersection, relevant_pages, bulk_keyword_difficulty (§7.1)
9. GSC connector (`apps/connectors/gsc/`): `searchAnalytics.query`, pagination via `startRow`, apply 2–3 day lag offset to comparison windows (§7.2)
10. Celery + Redis: install `celery`, `django-celery-beat`, `django-celery-results`; per-queue rate limits; Redis token bucket for DataForSEO account limit (§7.1)
11. Register all models in Django admin — this **is** the back office, no custom UI needed (§4)
12. `structlog` wired with `run_id`/`client_id` bound early, even before Sentry (§4.3, §13.3)

### Testing
- `pytest-django` + `factory_boy` scaffolding
- VCR.py cassette for one DataForSEO call and one GSC call, proving the connector interface works offline

### Exit criteria
Trigger a run via Django admin or shell → see `RawFetch` rows land per market/source → costs recorded on `RawFetch.cost_usd`.

---

## Phase 2 — Core engine

**Goal:** Topics with actions in the database. Everything here runs off Postgres only — no new external calls beyond Phase 1's ingestion.

### Build order
1. `apps/ingestion`: `KeywordObservation`, `PageObservation` typed rows, normalisation stage reading `RawFetch.payload` → typed rows (§6.3)
2. English-only normalisation first (`apps/topics/normalise.py`): lowercase, strip punctuation, stopwords, spaCy `en_core_web_sm` lemmatisation, canonical token sort (§8.1) — DE/FR/NL deferred to Phase 6
3. Blocking (`apps/topics/blocking.py`): group by `core_keyword`, IDF-weighted rare-token blocks, pgvector HNSW top-k=50 ANN lookup (§8.2)
4. Similarity (`apps/topics/similarity.py`): blended `sim(a,b)` — SERP overlap, cosine on embeddings, Jaccard on tokens, with `w_serp` renormalisation when SERP data absent (§8.3)
5. Clustering (`apps/topics/cluster.py`): scikit-learn agglomerative, complete linkage, cut at `1 - semantic_similarity_threshold`; post-cluster split on max internal distance or dual dominant intent (§8.4)
6. Selection/labelling (`apps/topics/select.py`): primary/secondary keyword selection, deduplicated volume sum, LLM label call (cached by cluster hash) (§8.5)
7. `Topic`, `TopicKeyword` models + `topic_uid` stability logic — match new clusters to existing topics by centroid + keyword overlap before minting a new UID (§6.5, §8.6)
8. Existing-page matching (`apps/pages/match.py`): ranking match (GSC) → content match (pgvector cosine on title+H1+meta embeddings) → slug match, combined score vs `existing_page_match_threshold` (§9.1)
9. Cannibalisation detection (`apps/pages/cannibalisation.py`): 2+ URLs matching same topic + overlapping GSC rankings → flag, pick canonical survivor by clicks/conversions/authority (§9.2)
10. Decision matrix (`apps/opportunities/decide.py`): implement §9.3 table exactly as a rule chain; every branch writes `decision_trace` (§6.5)
11. `Opportunity` model (§6.5)
12. Basic scoring (`apps/opportunities/score.py`): subset of §10.3 formula, conversion weight excluded until GA4 lands in Phase 5
13. **Golden-dataset test**: hand-labelled EN keyword set with known-correct clusters; CI fails if clustering quality regresses below threshold (§13.5) — build this now, this is the risk-mitigation step called out in §15

### Exit criteria
Given Phase 1's ingested data, running stages 4–9 produces `Topic` + `Opportunity` rows with populated `decision_trace`, inspectable via admin/shell. No sheet, no API yet.

---

## Phase 3 — Output

**Goal:** A sheet the client can actually use. First client-visible deliverable.

### Build order
1. `apps/exports/builder.py`: assemble `OpportunityRow` set from `Opportunity` + `Topic` + `Market`, P0 columns only (Topic, Market, Primary/Secondary keyword, Total volume, Current position, Action, Target URL) (§10)
2. `apps/exports/sheets.py`: gspread + `google-auth` service account client, tab creation for `Opportunities`, `Ignored`, `Cannibalisation`, `Run log`, `Reference` (§11.1)
3. Merge-on-write (`apps/exports/merge.py`) — build and test this carefully, it's flagged as highest-trust-cost if wrong (§11.2, §15):
   - Read current sheet keyed by hidden `topic_uid`
   - Partition engine-owned (cols 1–19) vs human-owned columns
   - Existing UID → update engine columns, preserve human columns
   - New UID → append, highlight
   - UID missing this run → move to `Archived` tab with reason, never delete
   - Single `batch_update` call per run (Sheets API quota)
4. Formatting: conditional formatting on action/priority, frozen header, data validation dropdown on action column, `Last updated` per-row stamp (§11.3)
5. `POST /api/v1/runs/{id}/export/` endpoint (§12)
6. `Run.sheet_url` populated on successful export

### Testing
- Unit test merge-on-write against a fixture sheet state with simulated human edits — assert edits survive re-export
- End-to-end: seeded fixture client → scratch spreadsheet

### Exit criteria
Client receives a real sheet from a real run with all P0 columns; re-running preserves any manual edits made to human-owned columns.

---

## Phase 4 — Enrichment

**Goal:** Full column set (all P1 columns).

### Build order
1. Difficulty (`apps/opportunities/difficulty.py`): DataForSEO KD adjusted by competitor domain strength in SERP, bucketed Low/Med/High (col 12)
2. Page type (`apps/opportunities/page_type.py`): derive from intent + SERP result composition (col 13)
3. Slug suggestion: LLM-generated + deterministic post-processing (slugify, language rules, collision check against `ExistingPage.path`) (col 14, §4.4)
4. Intent classification (col 20) — DataForSEO SERP feature composition, near-free per §2.1
5. AI search opportunity (`apps/opportunities/ai_opportunity.py`): AI Overview/PAA present + informational/commercial-investigational intent + structured-answerable + existing topical authority (top-20 in category) → Yes (§10.1)
6. Confidence scoring (col 21): degrade confidence when SERP-based similarity was unavailable during clustering (§8.3), or when action decision was a close call
7. Decay wiring: `PositionSnapshot` table populated from GSC history, decay trigger per `decay_from`/`decay_to`/`decay_min_drop` settings (§2.2, §7.2) — note decay rows only appear from run 2 onward

### Exit criteria
Sheet has all P1 columns populated correctly on a second+ run (decay needs run history).

---

## Phase 5 — Commercial signal

**Goal:** Commercially weighted queue.

### Build order
1. GA4 connector (`apps/connectors/ga4/`): `runReport`, dimensions `[landingPagePlusQueryString, sessionDefaultChannelGroup]`, metrics `[sessions, conversions, purchaseRevenue, keyEvents]`, filtered to organic, rolled up via URL-pattern taxonomy (§7.3)
2. `conversion_potential` + `conversion_basis` (`data` vs `inferred`) on `Opportunity` and `ExistingPage` (col 15, §7.3)
3. `w_conversion` redistribution logic in `ScoringWeights` resolution when GA4 absent — proportional redistribution, not zeroing (§7.3)
4. Estimated impact (`apps/opportunities/impact.py`): implement §10.2 formula; position→CTR curve fitted to client's own GSC data; `target_position` rules by action type (new=8, optimise=current−gain band, merge=best−gain band)
5. Full priority score (`apps/opportunities/score.py` — extend Phase 2 version): add conversion weight term, log-scaled volume, position-opportunity curve peaking in quick-win band, signal weight defaults (§10.3)

### Exit criteria
Sheet shows revenue-based estimated impact where GA4 is connected; clicks-only + `inferred` basis where it isn't.

---

## Phase 6 — Multi-market

**Goal:** All four markets (UK/DE/FR/NL) live.

### Build order
1. spaCy models per market: `de_core_news_*`, `fr_core_news_*`, `nl_core_news_*`
2. German/Dutch compound splitting (`Laufschuhe` ↔ `Lauf Schuhe`), umlaut transliteration indexing (`ü`↔`ue`) (§8.1)
3. French accent-insensitive matching + elision handling (`l'`, `d'`) (§8.1)
4. Dutch `ij` normalisation (§8.1)
5. Per-market golden datasets — separate from the Phase 2 English one, each hand-labelled independently (§15 risk item)
6. Cross-market propagation (`apps/topics/cross_market.py`): after all markets clustered, group into `cross_market_group` via pivot-language translation + multilingual embedding match; guardrail on source-market performance bar and target-market demand threshold (§9.4)
7. Validate per-market `EngineSettings` overrides end-to-end (already modeled in Phase 1, now exercised across real market differences)

### Exit criteria
Full run across all four markets produces correct per-market topics and correctly-guarded cross-market propagation rows.

---

## Phase 7 — Hardening

**Goal:** Production launch.

### Build order
1. Cost guardrails: enforce `max_serp_calls_per_run` and `max_spend_per_run_usd` mid-run, halting with `Run.status = "partial"` and a clear error rather than silent overspend (§13.1)
2. Alerting (via Sentry + custom checks): run failure, stage duration > 2× rolling median, cost above threshold, row count deviating >40% from previous run, sustained external API errors (§13.4)
3. Structured logging hardening: credential redaction by `structlog` processor (not convention), OAuth tokens never logged (§13.3)
4. Full VCR.py cassette coverage across all connectors so test suite runs offline (§13.5)
5. Complete unit test coverage on every decision-matrix branch and scoring component — these encode client-agreed business logic (§13.5)
6. End-to-end test: seeded fixture client → full pipeline → scratch spreadsheet, asserting merge-on-write correctness
7. Security pass: `django-fernet-fields` or platform secret store for credentials at rest, GSC/GA4 service account read-only scoping, Sheets write scoped to the specific spreadsheet only (§13.3)
8. Threshold tuning session against real client feedback on Phase 2/3 output
9. Flower dashboard + Django admin run dashboard wired for task visibility (§4.3)

### Exit criteria
Production launch: guardrails tested under simulated overspend, alerts firing correctly, full test suite green and offline-runnable.

---

## Risks to actively track (from §15)

| Risk | Watch for | Relevant phase |
|---|---|---|
| Clustering quality disputes | Client reaction to Phase 2 golden dataset review | 2 |
| GA4 delayed indefinitely | Confirm Phase 5 isn't a hard blocker for other phases | 5 |
| DataForSEO cost overruns | Monitor actual vs estimated $25–45/run from first real run | 1, 7 |
| GSC gaps for low-volume queries | Confirm "absence ≠ zero" doesn't silently break quick-win/decay logic | 2, 4 |
| Human edits lost on re-export | Test merge-on-write thoroughly before Phase 3 ships to client | 3 |
| DE/FR/NL compound handling | Don't treat Phase 6 language work as copy-paste of English pipeline | 6 |
| Requirements drift (Engine 2+) | Keep `Opportunity` as the stable interface; avoid leaking pipeline internals into other engines | all |

---

*Plan generated from design doc v0.1 (August 2026). Update as client decisions in §0 land and phases complete.*

# Engine 1 — Find Opportunities
## Build Handover Document (for AI-assisted or human implementation)

| | |
|---|---|
| **Purpose** | Single source of truth to hand this build to an engineer or an AI coding assistant (Claude / Gemini / GPT) with zero missing context |
| **Inputs synthesized** | (1) Client requirements doc — "ENGINE 1 — Find Opportunities — Requirements" (3-page draft) (2) Engineering team's technical design response, v0.1 |
| **Status of this document** | Planning artifact. Not itself a spec to code blindly from — Section 0 lists unresolved questions that block parts of the build |
| **How to use this doc** | Read Section 0 and Section 1 first. Then work top to bottom — each numbered task in Section 4 onward is scoped to be handed to a coding agent as a standalone unit, with its dependencies stated |

---

## 0. Read this first — unresolved questions that block work

Before any implementation task below is started, these need a client decision. Each is flagged again inline at the task it blocks, but they're collected here so nothing slips through.

| # | Question | Raised by | Blocks | Engineering's recommendation (not yet confirmed) |
|---|---|---|---|---|
| Q1 | Should **Estimated impact** (column 18) be calculated, and if so by what method? Client left it blank pending agreement. | Client | Task 8.4, Task 10.2 | Formula-based: `search volume × (CTR at target position − CTR at current position) × conversion rate`. Traffic-only (no £/$ figure) until GA4 is connected. |
| Q2 | Should **Priority score** (column 19) be promoted from P1 to **P0**? Client's own rules say "scored, not just collected" implies it must always be present. | Client | Task 8.5, all of Section 5 | Yes — promote to P0. Score computed from data always available (volume, position, signal type); conversion weight degrades gracefully when GA4 absent. |
| Q3 | Should **Intent** and **Confidence** columns be added back in? They were in the original spec, dropped from the current sheet draft. | Client | Task 3.4, Task 6.5, Task 8.3 | Yes, both at P1. Intent is near-free (comes from SERP data already fetched) and feeds page-type/AI-opportunity columns. Confidence tells a human editor which rows to spot-check. |
| Q4 | What counts as a "topic"? Requirements never define whether near-duplicate keyword phrasings are one topic or two. | Engineering (gap found on review) | Task 7 (all of clustering) — this is the highest-risk, highest-effort part of the build | Define by **SERP overlap**, not by wording: two keywords are the same topic if a single page could realistically rank for both. See Task 7.3. |
| Q5 | What are the criteria for the **Ignore** action? Rules say every row must be actionable and Ignore is a valid action, but no threshold is defined for when a topic should be ignored vs. queued. | Engineering (gap found on review) | Task 9.3 | Ignored rows go to a separate `Ignored` sheet tab, not the main queue, driven by `min_search_volume` and duplicate-topic checks. See decision matrix in Task 9.3. |
| Q6 | How should **cannibalisation** (two of our own pages ranking for the same topic) be surfaced? The requirements name it as a concern but define no output column for it. | Engineering (gap found on review) | Task 9.2 | Use the `Merge` action with a multi-value Target URL field, plus a dedicated `Cannibalisation` sheet tab. Flag when 2+ of our URLs rank for overlapping keywords. |
| Q7 | Is the **decay trigger** "drop out of top 3–5" measuring a fall from position 3, or from position 5? Ambiguous as written. | Engineering (gap found on review) | Task 9.1 (`EngineSettings` model), Task 11.3 | Model as two separate adjustable thresholds — `decay_baseline_max_position` (default 5) and `decay_current_min_position` (default 5) — plus a `decay_min_drop` noise filter (default 3 positions). All configurable per client. |
| Q8 | What **refresh cadence** does the client want? Not specified in requirements. | Engineering (gap found on review) | Task 2.2 (`django-celery-beat` scheduling), Task 14 (delivery phasing) | Monthly full run + weekly GSC-only delta run, configurable per client. |
| Q9 | The sheet will be **manually edited** by humans (owners, status, notes) between runs. How do we avoid overwriting their edits on the next run? | Engineering (gap found on review) | Task 11 (entire export module) | Stable `topic_uid` row keys + merge-on-write logic that only touches engine-owned columns. See Task 11.4. |
| Q10 | What **language handling** is needed for DE/FR/NL? Not addressed in requirements at all. | Engineering (gap found on review) | Task 7.1, Task 2.3 (spaCy models) | Per-market language config using spaCy models, with specific compound-noun handling for German/Dutch. See Task 7.1. |
| Q11 | Where does **Category** (column 3) data come from — URL structure, GA4, or a client-supplied taxonomy? Undefined in requirements. | Engineering (gap found on review) | Task 6.4, and blocks all of Category population until resolved | Client-supplied taxonomy mapped to URL path patterns, with a fallback ML classifier for pages that don't match. **This one genuinely needs the client to supply the taxonomy — it cannot be inferred confidently by the engine alone.** |
| Q12 | What is `value_per_conversion` (£/$ per conversion), needed to express Estimated Impact as a revenue figure rather than a traffic figure? | Engineering | Task 10.2 (deferred, not blocking early phases) | Not something engineering can guess — needs a client-supplied number, likely from their own margin/AOV data. |
| Q13 | Auth model for Google Search Console and GA4 — service account with domain-wide delegation, or per-client OAuth refresh tokens? | Engineering | Task 4 (GSC connector), Task 5 (GA4 connector) | Depends on the client's Google Workspace policy — needs a client decision, not an engineering one. |
| Q14 | Competitor lists per market — does the client have a defined list, or should engineering derive one from DataForSEO's competitor-discovery endpoint? | Engineering | Task 3 (DataForSEO connector), Task 2.1 (`Competitor` model seeding) | Engineering can suggest competitors via `/v3/dataforseo_labs/google/competitors_domain/live`, but the client's own view should take precedence where supplied. |
| Q15 | Seed keywords / category terms per market — needed to anchor keyword research (check #1). Not supplied in requirements. | Client | Task 3.2 | Must come from the client before Phase 1 keyword research can run meaningfully. |
| Q16 | Run cadence and **market rollout order** — do UK/DE/FR/NL all launch together, or phased? | Client | Task 14 (phasing), Task 2.1 (`Market` seeding) | Not engineering's call — affects delivery sequencing in Section 5. |

**Net effect on sequencing:** Q1, Q2, Q3, Q4, Q7, Q9, Q10 all have a working default proposed by engineering and **do not need to block the start of the build** — they block only the specific task cited. Q11, Q12, Q13, Q14, Q15, Q16 need actual client input and have no safe default; these should be chased in parallel with Phase 1 engineering work, not treated as gating the whole project.

---

## 1. What this system is, in one paragraph

A scheduled backend pipeline pulls SEO and analytics data from three external APIs (DataForSEO, Google Search Console, GA4) plus a fourth, non-API source (the client's own sitemap). It groups the resulting keywords into **topics** (not one row per keyword), checks each topic against pages the client's site already has, decides whether that topic should become a new page, an improvement to an existing page, a merge of duplicate pages, or should be ignored, scores every actionable topic by priority, and writes one row per topic per market into a single Google Sheet. The sheet is the entire deliverable of this engine — there is no other UI for the end client. Content generation, publishing, and reporting are explicitly **out of scope** for Engine 1.

**The framing that should guide every implementation decision:** this is a decision system, not a data-collection system. Fetching data is the easy, cheap part. Grouping keywords correctly, avoiding duplicate recommendations, and producing a trustworthy priority order is the hard, expensive part, and is where engineering effort should concentrate.

---

## 2. The nine checks — source of truth table

The client's requirements describe "nine checks" against "three sources." Cross-referencing against the engineering response, there are actually **four sources** (sitemap crawling isn't part of any of the three named APIs), and **one check is computed internally**, not fetched from anywhere. This table is the canonical mapping — use it, not the client doc's framing, when building connectors.

| # | Check | Real source | Implementation note |
|---|---|---|---|
| 1 | Keyword research | DataForSEO Labs | Needs client-seeded terms/categories/competitors (Q15) |
| 2 | Competitor gaps | DataForSEO Labs | Domain Intersection endpoint, intersections disabled |
| 3 | Competitor top pages | DataForSEO Labs | Relevant Pages endpoint |
| 4 | Search Console performance | Google Search Console | Query × Page × Country dimensions |
| 5 | Sitemap / existing pages | **Client sitemap** — not an API, a crawl | Fourth source, not one of the "three" |
| 6 | Ranking decay | Google Search Console | **Cannot produce output on the very first run.** GSC gives up to 16 months of history for a backfilled baseline, but a true "previous vs current" comparison needs our own stored snapshot series. First run = baseline only. Decay rows start appearing from run 2 onward. Flag this expectation to the client explicitly so a client isn't confused by an empty column 8 on day one. |
| 7 | Quick wins | Google Search Console | Positions 7–20 band, adjustable |
| 8 | Working in other markets | **Derived internally — no external call** | Requires the topic table to already exist across all markets, so this runs as a second pass after all markets have completed clustering, not as part of ingestion |
| 9 | Conversion performance | GA4 | Landing-page level, rolled up to category |

---

## 3. Technology stack decision (confirmed direction, not open)

The client specified Python and leaned toward Django; engineering confirmed this is the right call for reasons beyond preference — the system is config-heavy (per-client thresholds, market definitions, competitor lists, scoring weights) and Django admin gives a working back office for that without building custom UI.

### 3.1 Core stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Framework | Django 5.x (ORM, migrations, admin, auth) |
| API layer | Django REST Framework |
| Database | PostgreSQL 16 + `pgvector` extension (JSONB for raw payloads, vectors for semantic clustering — one engine, not two) |
| Task queue | Celery 5 + Redis |
| Scheduling | `django-celery-beat` (DB-backed, editable per-client cadence in admin) |
| Task result history | `django-celery-results` |

### 3.2 Data and processing libraries

| Concern | Library | Reason |
|---|---|---|
| Dataframe transform | **Polars** (pandas as fallback) | Runs 100k+ keyword rows per client per run; Polars' lazy API keeps memory flat |
| External payload validation | **Pydantic v2** | DataForSEO responses are deeply nested and change occasionally — validate at the boundary, fail loudly |
| HTTP client | **httpx** + **tenacity** | Async pooling + declarative retry/backoff |
| Clustering | **scikit-learn** (agglomerative) + **scipy** sparse matrices | SERP-overlap distance matrix |
| Embeddings | `sentence-transformers`, multilingual model (`paraphrase-multilingual-mpnet-base-v2`), stored in `pgvector` | Must handle EN/DE/FR/NL in one shared vector space; self-hosted, zero marginal cost per run |
| Language processing | **spaCy**, one model per language (`en_core_web_sm`, German/French/Dutch equivalents) | Lemmatisation and stopwords are language-specific — see Task 7.1 |
| Sitemap crawling | `advertools` or `ultimate-sitemap-parser` + httpx | Handles sitemap indexes, gzip, nested sitemap sets |
| Sheets output | **gspread** + `google-auth` (service account) | Batch writes, named ranges, formatting |
| Google APIs | `google-api-python-client` (GSC), `google-analytics-data` (GA4) | Official clients |

### 3.3 Operational tooling

| Concern | Choice |
|---|---|
| Config / secrets | `django-environ`, 12-factor, platform secret store |
| Logging | `structlog` → JSON, `run_id` + `client_id` bound to every line |
| Error tracking | Sentry |
| Task visibility | Flower + a custom Django admin run dashboard |
| Containers | Docker + Compose (local and deployed use same images) |
| Testing | `pytest`, `pytest-django`, `factory_boy`, **VCR.py cassettes** for all external API calls (offline, deterministic test suite) |
| Code quality | `ruff`, `mypy` (strict on domain layer), `pre-commit` |

### 3.4 Where an LLM is used — and explicitly is NOT used

This is a firm design constraint, not a suggestion — carry it into every implementation task:

**LLM used for (3 places only):**
1. Topic labelling — turning a keyword cluster into a plain-language subject line (column 1). Deterministic labelling produces ugly, unusable output here.
2. Category classification — only where no taxonomy mapping exists (fallback for Q11).
3. Slug suggestion (column 14) — with deterministic post-processing to enforce URL format rules.

**LLM explicitly NOT used for:** clustering, action decisions, scoring, or difficulty calculation. These must be reproducible and explainable — an editor needs to get an answer to "why is this row here" from the `decision_trace` / `why_flagged` fields, not from a model's non-deterministic output. **Every LLM call must be cached by input hash** so re-runs cost nothing and produce identical labels.

### 3.5 Stack alternatives considered and rejected (context, don't re-litigate without cause)

- **FastAPI + SQLModel** instead of Django — rejected because it would mean rebuilding admin CRUD for config from scratch. Django admin's value here outweighs FastAPI's ergonomics.
- **Airflow / Dagster** instead of Celery for orchestration — rejected as heavy for a 9-stage *linear* chain; Celery chains cover it. Revisit only if future engines (2–N) introduce branching dependencies.
- **DuckDB** as the analytical layer — rejected because splitting state across two database engines complicates the "re-run stages 4–9 freely without re-fetching" property (see Task 5 — this property is the single most important structural decision in the whole design).

---

## 4. Architecture overview

```
                    ┌─────────────────────────────────────────────┐
                    │              Django + DRF                   │
                    │   admin · config · run control · API        │
                    └────────────────┬────────────────────────────┘
                                     │
                    ┌────────────────▼────────────────────────────┐
                    │         Celery workers (queued by type)     │
                    │  ingest · transform · cluster · score · export│
                    └────────────────┬────────────────────────────┘
                                     │
   ┌─────────────┬─────────────┬─────┴───────┬──────────────┬─────────────┐
   │             │             │             │              │             │
┌──▼──────┐ ┌────▼─────┐ ┌─────▼────┐ ┌──────▼─────┐ ┌──────▼──────┐ ┌────▼─────┐
│DataForSEO│ │  Search  │ │   GA4    │ │  Sitemap   │ │  Postgres   │ │  Google  │
│  Labs    │ │ Console  │ │Data API  │ │  crawler   │ │ + pgvector  │ │  Sheets  │
└──────────┘ └──────────┘ └──────────┘ └────────────┘ └─────────────┘ └──────────┘
```

### 4.1 Project layout (create this skeleton first — Task 1)

```
engine/
├── config/                 settings/{base,local,production}.py, celery.py, urls.py
├── apps/
│   ├── clients/            Client, Market, Competitor, ClientSettings, ScoringWeights
│   ├── connectors/         one subpackage per source, all behind a common interface
│   │   ├── base.py         Connector ABC: fetch() -> RawFetch
│   │   ├── dataforseo/     client, endpoints, schemas (pydantic), cost accounting
│   │   ├── gsc/
│   │   ├── ga4/
│   │   └── sitemap/
│   ├── ingestion/          RawFetch, normalisation, KeywordObservation, PageObservation
│   ├── topics/             clustering, labelling, Topic, TopicKeyword
│   ├── pages/               existing page index, embeddings, cannibalisation matching
│   ├── opportunities/       decision rules, scoring, Opportunity model
│   ├── exports/             sheet builder, gspread writer, merge-on-write
│   ├── runs/                Run, RunStage, orchestration tasks, status API
│   └── api/                 DRF viewsets and serializers
└── tests/
```

**Design rule to enforce in code review:** the `connectors` boundary must be strict. Every connector returns a `RawFetch` row and *nothing else* — no connector may know what a `Topic` is. This keeps provider changes contained and is what makes it possible to add a 5th data source later without touching the pipeline stages.

---

## 5. The 10-stage pipeline (this is the core sequencing model — build in this order)

The run is a linear chain. **Each stage must be idempotent and restartable from the previous stage's persisted output.** This is a hard requirement, not a nice-to-have: DataForSEO calls cost real money per call, and a bug in a downstream transform stage must never force a re-fetch of upstream paid data.

```
Stage 0  PLAN          resolve client config, markets, competitors, budget guardrails
Stage 1  INGEST        fetch from all sources → RawFetch (immutable JSON snapshots)
Stage 2  NORMALISE     RawFetch → typed rows (KeywordObservation, PageObservation, ConversionStat)
Stage 3  ENRICH        keyword difficulty, SERP features, intent, selective SERP fetch
Stage 4  CLUSTER       keywords → Topics (per market)
Stage 5  MATCH         topics ↔ existing pages; detect cannibalisation
Stage 6  DECIDE        assign action + target URL + page type + slug
Stage 7  CROSS-MARKET  check 8 — propagate topics proven in one market to others
Stage 8  SCORE         priority score, estimated impact, confidence
Stage 9  EXPORT        build OpportunityRow set, write to Google Sheet
```

**Critical structural property:** Stages 1–3 are the *only* stages that touch external, billable APIs. Stages 4–9 run entirely against the database. This means the expensive part of the pipeline runs once per data-refresh cycle, while the analytical/tuning part (clustering thresholds, scoring weights) can be re-run for free as many times as needed. This property is what makes the `/rescore` API endpoint possible (Task 12) and is the single most load-bearing design decision in this document — do not compromise it for convenience elsewhere.

---

## 6. Data model — build order and field-level detail

Build models in this dependency order. Each subsection below is a self-contained implementation task.

### 6.1 Task: `clients` app — Client, Market, Competitor

```python
class Client(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    primary_domain = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

class Market(models.Model):
    """One row per (client, country, language). UK, DE, FR, NL to start (per requirements)."""
    client = models.ForeignKey(Client, related_name="markets", on_delete=models.CASCADE)
    code = models.CharField(max_length=10)              # "UK", "DE"
    country_iso = models.CharField(max_length=2)         # "GB", "DE"
    language_code = models.CharField(max_length=5)       # "en", "de"
    dataforseo_location_code = models.IntegerField()     # 2826 = United Kingdom
    gsc_property = models.CharField(max_length=255)      # sc-domain:... or URL prefix
    ga4_property_id = models.CharField(max_length=50, blank=True)
    sitemap_url = models.URLField()
    url_pattern = models.CharField(max_length=255, blank=True)  # "/de/" or subdomain
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("client", "code")]

class Competitor(models.Model):
    market = models.ForeignKey(Market, related_name="competitors", on_delete=models.CASCADE)
    domain = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)   # primary set drives gap analysis
```

**Blocked by:** Q14 (competitor list source), Q16 (market rollout order) — models can be built now, seeding data needs client input.

### 6.2 Task: `EngineSettings` model — adjustable thresholds

The client's requirements name only three adjustable settings explicitly (quick-win band, decay trigger, markets in scope). Engineering's recommendation — build a fuller settings model now, because **every threshold in this engine will eventually be argued about by the client**, and retrofitting a settings row per threshold later is far more expensive than building it once, correctly, up front.

```python
class EngineSettings(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    market = models.ForeignKey(Market, null=True, blank=True, on_delete=models.CASCADE)
    # null market = client-wide default

    # --- explicitly required by the client ---
    quick_win_min_position = models.FloatField(default=7.0)
    quick_win_max_position = models.FloatField(default=20.0)
    decay_baseline_max_position = models.FloatField(default=5.0)   # answers Q7
    decay_current_min_position = models.FloatField(default=5.0)    # answers Q7
    decay_min_drop = models.FloatField(default=3.0)                # noise filter
    decay_baseline_days = models.IntegerField(default=90)
    decay_comparison_days = models.IntegerField(default=28)

    # --- discovery ---
    min_search_volume = models.IntegerField(default=50)
    min_keywords_per_topic = models.IntegerField(default=1)
    max_keyword_difficulty = models.IntegerField(default=100)

    # --- clustering ---
    serp_overlap_threshold = models.IntegerField(default=3)   # shared URLs in top 10
    semantic_similarity_threshold = models.FloatField(default=0.82)
    clustering_linkage = models.CharField(max_length=20, default="complete")

    # --- matching / cannibalisation ---
    existing_page_match_threshold = models.FloatField(default=0.75)
    cannibalisation_min_pages = models.IntegerField(default=2)

    # --- output ---
    max_rows_per_run = models.IntegerField(default=500)
    include_ignored_rows = models.BooleanField(default=True)

    # --- cost guardrails ---
    max_serp_calls_per_run = models.IntegerField(default=5000)
    max_spend_per_run_usd = models.DecimalField(max_digits=8, decimal_places=2, default=100)


class ScoringWeights(models.Model):
    """Priority score weights, separated so they can be tuned without touching thresholds."""
    client = models.OneToOneField(Client, on_delete=models.CASCADE)
    w_volume = models.FloatField(default=0.25)
    w_position_opportunity = models.FloatField(default=0.20)
    w_conversion = models.FloatField(default=0.20)
    w_difficulty = models.FloatField(default=0.15)
    w_signal = models.FloatField(default=0.10)
    w_market = models.FloatField(default=0.10)
    signal_weights = models.JSONField(default=dict)   # per why_flagged multiplier
    market_weights = models.JSONField(default=dict)   # e.g. {"UK": 1.0, "DE": 0.9}
```

**Implementation note:** resolution order at runtime is market override → client default → code default. Wrap this in a `SettingsResolver` service class so **no call site anywhere in the codebase reads a settings field directly** — this is what makes per-market overrides actually work in practice rather than being bypassed piecemeal.

### 6.3 Task: `RawFetch` and observation models (ingestion app)

```python
class RawFetch(models.Model):
    """Immutable snapshot of one external call. Never mutated, never deleted early."""
    run = models.ForeignKey("runs.Run", on_delete=models.CASCADE)
    market = models.ForeignKey(Market, on_delete=models.CASCADE)
    source = models.CharField(max_length=32)        # dataforseo | gsc | ga4 | sitemap
    endpoint = models.CharField(max_length=128)
    request_params = models.JSONField()
    request_hash = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField()                     # or S3 pointer if > 1MB
    cost_usd = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["market", "source", "fetched_at"])]
```

`request_hash` is a cost-saving mechanism, not just a dedup key: if an identical request was already made within a configurable `cache_ttl` for that source, reuse the stored payload instead of paying for it again. Keyword volume data doesn't move daily, so this alone materially cuts repeat-run cost.

```python
class KeywordObservation(models.Model):
    """One keyword as seen by one source in one market at one point in time."""
    run = models.ForeignKey("runs.Run", on_delete=models.CASCADE)
    market = models.ForeignKey(Market, on_delete=models.CASCADE)
    keyword = models.CharField(max_length=500, db_index=True)
    keyword_normalised = models.CharField(max_length=500, db_index=True)
    source = models.CharField(max_length=32)
    signal = models.CharField(max_length=40)          # maps to why_flagged (column 11)

    search_volume = models.IntegerField(null=True)
    cpc = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    keyword_difficulty = models.IntegerField(null=True)
    competition = models.FloatField(null=True)

    our_position = models.FloatField(null=True)
    our_url = models.URLField(max_length=1000, blank=True)
    impressions = models.IntegerField(null=True)
    clicks = models.IntegerField(null=True)
    ctr = models.FloatField(null=True)

    competitor_domain = models.CharField(max_length=255, blank=True)
    competitor_url = models.URLField(max_length=1000, blank=True)
    competitor_position = models.FloatField(null=True)

    serp_features = models.JSONField(default=list)    # ai_overview, paa, featured_snippet...
    serp_top_urls = models.JSONField(default=list)     # top 10 for clustering
    intent = models.CharField(max_length=20, blank=True)   # answers Q3
    embedding = VectorField(dimensions=768, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["run", "market", "keyword_normalised"]),
            models.Index(fields=["market", "signal"]),
        ]
```

**Deliberate design choice, do not "optimise" away:** one keyword can and will produce *several* observation rows — e.g. found in keyword research, currently ranking in GSC, and also held by a competitor. This is intentional. The union of a keyword's signals is exactly what populates the `why_flagged` column (11), and collapsing observations early loses that traceability permanently.

### 6.4 Task: `ExistingPage` model (pages app)

```python
class ExistingPage(models.Model):
    market = models.ForeignKey(Market, on_delete=models.CASCADE)
    url = models.URLField(max_length=1000)
    path = models.CharField(max_length=1000, db_index=True)
    title = models.CharField(max_length=500, blank=True)
    h1 = models.CharField(max_length=500, blank=True)
    meta_description = models.TextField(blank=True)
    category = models.CharField(max_length=200, blank=True)     # blocked by Q11
    page_type = models.CharField(max_length=32, blank=True)
    last_modified = models.DateTimeField(null=True)
    in_sitemap = models.BooleanField(default=True)

    # from GSC
    total_clicks_28d = models.IntegerField(default=0)
    total_impressions_28d = models.IntegerField(default=0)
    ranking_keyword_count = models.IntegerField(default=0)

    # from GA4
    sessions_28d = models.IntegerField(default=0)
    conversions_28d = models.IntegerField(default=0)
    conversion_rate = models.FloatField(null=True)
    revenue_28d = models.DecimalField(max_digits=12, decimal_places=2, null=True)

    embedding = VectorField(dimensions=768, null=True)

    class Meta:
        unique_together = [("market", "url")]
```

**Blocked by:** Q11 — the `category` field can be populated by URL-pattern matching once the client supplies a taxonomy; until then it should be left blank rather than guessed with low confidence.

### 6.5 Task: `Topic`, `TopicKeyword`, `Opportunity` models (topics + opportunities apps)

```python
class Topic(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    market = models.ForeignKey(Market, on_delete=models.CASCADE)
    topic_uid = models.CharField(max_length=64, unique=True)   # stable across runs — see Task 7.6
    label = models.CharField(max_length=300)                    # plain language, column 1
    primary_keyword = models.CharField(max_length=500)
    primary_keyword_volume = models.IntegerField(default=0)
    total_search_volume = models.IntegerField(default=0)
    category = models.CharField(max_length=200, blank=True)
    intent = models.CharField(max_length=20, blank=True)         # answers Q3
    centroid = VectorField(dimensions=768, null=True)
    cross_market_group = models.CharField(max_length=64, blank=True, db_index=True)
    first_seen_run = models.ForeignKey("runs.Run", null=True, on_delete=models.SET_NULL)

class TopicKeyword(models.Model):
    topic = models.ForeignKey(Topic, related_name="keywords", on_delete=models.CASCADE)
    keyword = models.CharField(max_length=500)
    search_volume = models.IntegerField(default=0)
    is_primary = models.BooleanField(default=False)
    our_position = models.FloatField(null=True)
    keyword_difficulty = models.IntegerField(null=True)

class Opportunity(models.Model):
    """One row in the output sheet — the final artifact of the whole pipeline."""
    run = models.ForeignKey("runs.Run", on_delete=models.CASCADE)
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE)
    market = models.ForeignKey(Market, on_delete=models.CASCADE)

    action = models.CharField(max_length=20)                # new|optimise|merge|ignore
    target_urls = models.JSONField(default=list)             # >1 implies merge (answers Q6)
    why_flagged = models.JSONField(default=list)              # signals, ordered by strength

    current_position = models.FloatField(null=True)
    previous_position = models.FloatField(null=True)
    difficulty = models.CharField(max_length=20, blank=True)
    difficulty_score = models.IntegerField(null=True)
    page_type = models.CharField(max_length=32, blank=True)
    suggested_slug = models.CharField(max_length=300, blank=True)
    conversion_potential = models.CharField(max_length=10, blank=True)
    conversion_basis = models.CharField(max_length=20, blank=True)  # data|inferred|unknown
    competitor_url = models.URLField(max_length=1000, blank=True)
    ai_search_opportunity = models.BooleanField(null=True)
    estimated_impact = models.JSONField(null=True)            # answers Q1
    priority_score = models.FloatField(null=True)             # answers Q2
    confidence = models.FloatField(null=True)                 # answers Q3
    decision_trace = models.JSONField(default=dict)            # why the engine chose this action

    class Meta:
        unique_together = [("run", "topic")]
        indexes = [models.Index(fields=["run", "-priority_score"])]
```

**Do not treat `decision_trace` as optional decoration.** When a human editor disputes a row's action or score, this field is the *only* mechanism for answering "why is this row here" — it records which rule fired, which thresholds applied, and what the runner-up action was. It's also what makes the scoring model tunable later, because decisions can be replayed against changed weights without re-fetching anything.

### 6.6 Task: `Run` and `RunStage` models (runs app)

```python
class Run(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    run_type = models.CharField(max_length=20)   # full | gsc_delta | rescore
    status = models.CharField(max_length=20)     # pending|running|complete|failed|partial
    markets = models.JSONField(default=list)
    settings_snapshot = models.JSONField(default=dict)   # what config produced this output
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    total_cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    sheet_url = models.URLField(blank=True)
    error = models.TextField(blank=True)

class RunStage(models.Model):
    run = models.ForeignKey(Run, related_name="stages", on_delete=models.CASCADE)
    name = models.CharField(max_length=40)
    status = models.CharField(max_length=20)
    records_in = models.IntegerField(default=0)
    records_out = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    error = models.TextField(blank=True)
```

**Why `settings_snapshot` matters:** it makes every sheet fully reproducible. Six weeks after a run, when someone asks why a topic scored 84 in June but 61 in July, the answer must be recoverable from stored data — not from a git log or someone's memory of what the thresholds used to be.

---

## 7. Topic clustering — the highest-risk, highest-value part of the build

This is called out explicitly because it is the part of the system most likely to determine whether the client trusts the output. "One row per topic, not one row per keyword" is easy to state as a requirement and genuinely hard to deliver well. Cluster too aggressively and distinct pages get incorrectly merged into one unusable row; cluster too loosely and the sheet degrades into the flat keyword list the client explicitly said they did not want.

### 7.1 Task: per-language normalisation (Stage A)

Lowercase, strip punctuation and extra whitespace, remove language-specific stopwords, lemmatise using the market's spaCy model, sort tokens into a canonical form.

Language-specific handling required (answers Q10):
- **German** — compound nouns are the primary hazard (`Laufschuhe` vs `Lauf Schuhe` — same meaning, different surface form). Apply compound splitting so both reach the same normalised form. Preserve umlauts *and* index a transliterated form (`ü` ↔ `ue`) in parallel, since real searchers type both variants.
- **French** — accent-insensitive matching; handle elision (`l'`, `d'`).
- **Dutch** — compounding as with German, plus `ij` digraph normalisation.

Output of this stage: a `keyword_normalised` field, plus a token set used for blocking in the next stage.

### 7.2 Task: blocking (Stage B) — required for performance, not optional

Comparing every keyword against every other keyword is O(n²) and is not affordable at the expected scale (100k+ keywords per client per run). Generate candidate pairs only within blocks, using the union of three separate blocking strategies:

1. Group by DataForSEO's supplied `core_keyword` field, where present.
2. Group by shared rare token (IDF-weighted, so common words like "best" or "shoes" don't create one giant, useless block).
3. Group by approximate-nearest-neighbour lookup on the embedding vector, using a pgvector HNSW index, top-k = 50.

The union of these three candidate sets is typically 2–4 orders of magnitude smaller than the full cross product — this is what makes clustering computationally tractable at scale.

### 7.3 Task: similarity scoring (Stage C) — implements the topic definition from Q4

**Working definition to implement:** two keywords belong to the same topic if a single page could realistically rank for both. The operational proxy: if Google returns substantially the same top-10 results for both queries, treat them as the same topic.

```
sim(a, b) = w_serp · serp_overlap(a, b)
          + w_sem  · cosine(emb_a, emb_b)
          + w_lex  · jaccard(tokens_a, tokens_b)
```

- `serp_overlap` (count of shared URLs in the top 10, normalised) is the strongest signal and should carry the highest weight. It requires a live SERP call per keyword, so it is **only available for a shortlist**, not the full keyword universe (see Task 3's cost note below).
- Shortlist selection criteria: by search volume, by whether we already rank for the keyword, and by whether the keyword arrived via a high-value signal (decay, quick win, competitor gap).
- Where SERP data is unavailable for a pair, `w_serp` is set to zero and the remaining weights renormalise across `w_sem` and `w_lex`. Pairs resolved without SERP evidence must carry a lower confidence score, which propagates into the row's final `confidence` field (Q3).

### 7.4 Task: clustering algorithm (Stage D)

Agglomerative clustering, **complete linkage** (not single linkage — single linkage chains, and chaining is exactly the failure mode that produces one 400-keyword "topic" spanning three unrelated subjects), cut at `1 − semantic_similarity_threshold` on distance `1 − sim`.

Post-cluster validation step: split any cluster where the maximum internal distance exceeds a configured ceiling, or where two distinct dominant search intents are detected within one cluster.

### 7.5 Task: selection and labelling (Stage E)

- **Primary keyword:** highest search volume in the cluster; ties broken by shortest form, then by best existing ranking position.
- **Secondary keywords:** remaining cluster members, volume-ordered, capped at a configurable count for sheet readability.
- **Total search volume:** sum across the cluster, **deduplicated by normalised form** so near-identical keyword variants don't inflate the number. Document this deduplication method in the sheet's `Reference` tab, since summed keyword volume is routinely overstated by other SEO tools and the client may cross-check against a different tool's number.
- **Label** (column 1): LLM-generated plain-language phrase derived from the keyword set, cached by cluster hash (per the LLM-usage rule in Task 3.4).

### 7.6 Task: stable topic identity across runs (Stage F)

A topic must retain the same identity from one run to the next, or the sheet cannot be diffed and human edits made between runs get destroyed. `topic_uid` is derived from the sorted set of the cluster's top-N keywords plus market. On each subsequent run, new clusters are matched against existing topics by centroid similarity and keyword overlap before a new UID is minted. Merges and splits of topics between runs must be explicitly recorded (not silently applied) so the change is visible to a human reviewer.

---

## 8. Ingestion detail per source

### 8.1 Task: DataForSEO connector

All DataForSEO Labs endpoints run in live mode. Billing is approximately **$0.01 per task + $0.0001 per returned item** — cost scales with volume requested, not with call frequency, which should directly shape the shortlisting logic in Task 7.3.

| Check | Endpoint | Key params |
|---|---|---|
| Keyword research | `/v3/dataforseo_labs/google/keyword_ideas/live`, `.../keyword_suggestions/live`, `.../related_keywords/live` | seed keywords, `location_code`, `language_code`, `limit` |
| Competitor discovery | `/v3/dataforseo_labs/google/competitors_domain/live` | our domain — used to validate/suggest the client's competitor list (Q14) |
| Competitor gaps | `/v3/dataforseo_labs/google/domain_intersection/live`, intersections disabled | `target1` = competitor, `target2` = us → keywords they rank for that we don't |
| Competitor top pages | `/v3/dataforseo_labs/google/relevant_pages/live` | competitor domain, ordered by estimated traffic |
| Difficulty | `/v3/dataforseo_labs/google/bulk_keyword_difficulty/live` | up to 1,000 keywords per call |
| SERP features + top URLs | `/v3/serp/google/organic/live/advanced` | **shortlisted keywords only — see cost note below** |

Two implementation notes to build around from day one:
- Labs responses include a `keyword_properties` object by default, carrying `keyword_difficulty`, `core_keyword`, and a synonym-clustering marker. Use `core_keyword` as a **free pre-grouping hint** before spending anything on SERP calls (feeds directly into Task 7.2's blocking stage).
- **SERP fetching is the cost centre of the entire pipeline.** A live advanced SERP call costs orders of magnitude more per keyword than a Labs item call. Never fetch SERPs for the full keyword universe — only the shortlist survives to clustering Stage C, typically 5–15% of raw keywords. `max_serp_calls_per_run` (from `EngineSettings`) must be enforced as a hard stop, not a soft warning.

Rate limiting: Celery per-queue rate limit plus a token bucket in Redis, so parallel workers cannot collectively exceed the DataForSEO account's rate limit.

### 8.2 Task: Google Search Console connector

Auth: service account with domain-wide delegation, **or** per-client OAuth refresh token — decided by the client's Google Workspace policy (Q13, unresolved). Pull `searchAnalytics.query` with dimensions `[query, page, country, date]`.

Constraints to design around:
- 25,000 rows per request, paginated via `startRow`.
- 16 months of history available — sufficient for a first-run decay baseline (see check #6's caveat in Section 2).
- Data is incomplete for low-volume queries (Google anonymises them) — **absence of a query in results is not evidence of zero impressions.** Do not treat it as such anywhere in the decision logic.
- 2–3 day reporting lag exists; comparison windows must be offset accordingly, or every run will show a phantom decline at the most recent edge of the data.

Store daily aggregates in a `PositionSnapshot` table rather than re-querying GSC's history at read time — this is faster and lets the engine define its own baseline window independent of GSC's retention quirks.

### 8.3 Task: GA4 connector

`runReport` on the Data API, dimensions `[landingPagePlusQueryString, sessionDefaultChannelGroup]`, metrics `[sessions, conversions, purchaseRevenue, keyEvents]`, filtered to organic search. Roll up from landing page to category using the **same** URL-pattern taxonomy that populates column 3 (Q11 — these two must stay consistent).

Per the client's explicit requirement, GA4 is a **dependency, not a blocker** — build graceful degradation from the start, not as an afterthought:
- When GA4 is absent: `conversion_potential` is set from rules-based inference (intent + category + page type combined), and `conversion_basis` is marked `inferred` — the sheet must stay honest about which cells are measured data vs. judgement.
- `w_conversion` (scoring weight) is redistributed proportionally across the *remaining* weights rather than zeroed out — zeroing it would penalise every topic equally and silently change the queue's ordering character without anyone noticing why.

### 8.4 Task: Sitemap crawler

Fetch `robots.txt` → sitemap index → all child sitemaps, handling gzip and nested sitemap sets. For each discovered URL, fetch title, H1, and meta description (throttled, respecting `robots.txt`). Cross-check discovered pages against GSC's page list: any URL that ranks in GSC but is absent from the sitemap should be flagged as an **orphan page** — itself a small, useful opportunity signal worth surfacing.

---

## 9. Matching, cannibalisation, and the decision engine

### 9.1 Task: match topics to existing pages

Three independent matchers, run in this order, each producing its own score:

1. **Ranking match** — does any of our URLs already rank for the topic's primary or a secondary keyword, per GSC? Strongest signal — a page ranking for a keyword *is*, by definition, a page about that topic.
2. **Content match** — cosine similarity between the topic's centroid embedding and each `ExistingPage` embedding (built from title + H1 + meta description), via pgvector.
3. **Slug match** — normalised URL path tokens against topic tokens. Weakest signal, used only as a tiebreaker.

Combined match score above `existing_page_match_threshold` (from `EngineSettings`) qualifies the page as a candidate target.

### 9.2 Task: cannibalisation detection (answers Q6)

If two or more of the client's own URLs match the same topic above threshold **and** both rank for overlapping keywords in GSC, flag this as cannibalisation. It produces a `merge` action with multiple entries in `target_urls`, and the row must carry a note identifying which URL should be the canonical survivor — chosen by combined clicks, conversions, and inbound authority. This is one of the higher-value outputs the engine can produce and the client's requirements only imply it exists as a concern — surface it explicitly and call it out to the client as a deliverable in its own right.

### 9.3 Task: the decision matrix (answers Q5 — Ignore criteria)

| Existing page match | Our current position | Additional condition | → Action | Target URL |
|---|---|---|---|---|
| None | — | Volume ≥ min threshold, difficulty ≤ max | **New content** | blank |
| None | — | Volume < min threshold | **Ignore** | blank |
| None | — | Topic already queued this run | **Ignore** (duplicate) | blank |
| One | 1–3 | No decay detected | **Ignore** (already winning) | matched URL |
| One | 1–3 | Decay detected | **Optimise** | matched URL |
| One | 4–20 | — | **Optimise** | matched URL |
| One | > 20 | Page thin or off-intent | **Optimise** | matched URL |
| One | > 20 | Page strongly off-intent | **New content** | blank, note existing |
| Two or more | any | Overlapping rankings | **Merge** | all matched URLs |
| Two or more | any | No ranking overlap | **Optimise** best match | best URL |

**Every path through this matrix must write to `decision_trace`.** Rows resolving to `Ignore` are routed to a separate sheet tab (Section 11), not deleted or hidden — this preserves the audit trail without polluting the working queue.

### 9.4 Task: cross-market propagation (check #8)

Runs only after all markets have completed clustering (this is a hard sequencing dependency on Stage 4 completing for every market first). Topics are grouped into a `cross_market_group` by translating the primary keyword to a pivot language and matching on the shared multilingual embedding space. A topic performing well in one market (good position, meaningful traffic, converting) that has no counterpart topic in another market generates a `new content` row in that other market, flagged `proven_in_other_market`.

**Guardrail, do not skip:** the source market's performance must clear a minimum bar, *and* the target market must show non-trivial search volume for the translated keyword set — otherwise the engine will propagate, e.g., UK topics into DE with no actual German demand behind them, which would actively damage trust in the output.

---

## 10. Scoring and the two open numeric methods

### 10.1 Task: full column derivation map

Build against this table directly — it is the canonical column-by-column spec, reconciling the client's original 19-column list with engineering's additions and priority-promotion recommendations.

| # | Column | Priority | Derivation | Status |
|---|---|---|---|---|
| 1 | Topic | P0 | Cluster label, LLM-generated (Task 7.5) | Ready to build |
| 2 | Market | P0 | `Market.code` | Ready to build |
| 3 | Category | P1 | URL-pattern taxonomy on matched page; classifier fallback for new content | **Blocked on Q11 — needs client taxonomy** |
| 4 | Primary keyword | P0 | Highest-volume cluster member, formatted `keyword (1,200)` | Ready to build |
| 5 | Secondary keywords | P0 | Remaining members, volume-ordered, capped | Ready to build |
| 6 | Total search volume | P0 | Deduplicated cluster sum (Task 7.5) | Ready to build |
| 7 | Current position | P0 | Weighted average GSC position, last 28 days; `—` if no page exists | Ready to build |
| 8 | Previous position | P1 | Same measure over baseline window; blank on first run | Ready to build (empty on run 1 by design) |
| 9 | Action | P0 | Decision matrix (Task 9.3) | Ready to build |
| 10 | Target URL | P0 | Matched page(s); multiple = merge; blank for new content | Ready to build |
| 11 | Why flagged | **Recommend P0** (client has as P1) | Ordered signal list from `KeywordObservation.signal` | Ready to build; **needs client sign-off on priority promotion** |
| 12 | Difficulty | P1 | DataForSEO KD for primary keyword, adjusted by competitor domain strength; bucketed Low/Med/High | Ready to build |
| 13 | Page type | P1 | Derived from intent + SERP result composition | Depends on Q3 (intent) being confirmed in |
| 14 | Suggested slug | P1 | Slugified primary keyword, language-appropriate, collision-checked against existing paths | Ready to build |
| 15 | Conversion potential | P1 | GA4 conversion rate of matched page/category vs. site median → High/Med/Low; `conversion_basis` records data vs. inference | Depends on GA4 connection (Task 8.3) |
| 16 | Competitor URL | P1 | Best-ranking competitor URL for primary keyword | Ready to build |
| 17 | AI search opportunity | P1 | Yes/No — see Task 10.3 below | Depends on Q3 (intent) |
| 18 | Estimated impact | **Open — Q1** | Method proposed in Task 10.4 | **Blocked — needs client agreement on method** |
| 19 | Priority score | **Recommend P0** (client has as P1) | Scoring model, Task 10.5 | **Needs client sign-off on priority promotion (Q2)** |
| 20 | Intent | *New column — Q3* | Informational / commercial / transactional / navigational | **Blocked — needs client confirmation this is wanted** |
| 21 | Confidence | *New column — Q3* | Engine's confidence in its own action recommendation, 0–1 | **Blocked — needs client confirmation this is wanted** |
| 22 | Topic UID | *Hidden column* | Stable key for merge-on-write (Task 11.4); not for human consumption | Ready to build |

### 10.2 Task: AI search opportunity logic (column 17)

Mark **Yes** only when *all four* of these hold:
1. SERP shows an AI Overview or People Also Ask block, **and**
2. Intent is informational or commercial-investigational, **and**
3. The topic is answerable in a structured, extractable way (definition, comparison, list, how-to), **and**
4. We have plausible topical authority — meaning we already rank in the top 20 for something in the same category.

Condition 4 exists specifically to prevent false positives: being cited in an AI answer for a topic where the site has zero existing standing is not realistic, and marking such rows "Yes" would send a content team after work that structurally cannot pay off.

### 10.3 Task: Estimated impact — proposed method (answers Q1, pending client sign-off)

```
Δtraffic = total_search_volume × (CTR(target_position) − CTR(current_position))
Δconv    = Δtraffic × conversion_rate(category)
Δvalue   = Δconv × value_per_conversion
```

- `CTR(position)` — a configurable position→CTR curve, **ideally fitted to the client's own GSC data** rather than a generic industry table, since we already have the raw data to do this properly and a client-specific curve is far more defensible than a borrowed one.
- `target_position` — set by action type: new content → position 8 (adjustable), optimise → current position minus a realistic gain band, merge → best of the merged set minus a gain band.
- `conversion_rate` and `value_per_conversion` — come from GA4; `value_per_conversion` additionally needs a client-supplied number (Q12) if the output is to be expressed in revenue rather than clicks.

**Recommended default behaviour:** show Estimated Impact as **monthly incremental clicks** by default; show a revenue figure only once GA4 is actually connected and a `value_per_conversion` figure is supplied. Do not show a revenue number derived from an *inferred* conversion rate — that's a false-precision problem that will undermine trust in the whole sheet the first time someone checks the math.

### 10.4 Task: Priority score formula (answers Q2, pending client sign-off on P0 promotion)

Normalised weighted sum, 0–100, all weights per-client via `ScoringWeights`:

```
score = 100 × (
    w_volume     × log_scaled(total_search_volume)
  + w_position   × position_opportunity
  + w_conversion × conversion_factor
  + w_difficulty × (1 − normalised_difficulty)
  + w_signal     × signal_weight
  + w_market     × market_weight
)
```

- `log_scaled` volume — the gap between 100 and 1,000 monthly searches matters more to prioritisation than the gap between 40,000 and 41,000.
- `position_opportunity` — peaks in the quick-win band; a topic sitting at position 11 has more available upside per unit of effort than one at position 2 (little room to gain) or position 90 (too far to realistically close).
- `signal_weight` defaults, directly encoding the client's own stated framing that a decaying page "already has authority, it's losing ground, not starting from zero": decay 1.0, quick win 0.95, competitor gap 0.8, conversion-proven category 0.9, cross-market proven 0.75, keyword research (cold) 0.6.
- `market_weight` — lets the client prioritise, e.g., UK over NL (or the reverse) without touching any other part of the scoring model.

Scores should be computed within a run and also rank-normalised, so the top of the queue is always meaningfully "the top of the queue" regardless of absolute score drift between runs caused by, e.g., seasonal volume changes.

---

## 11. Google Sheets export

### 11.1 Task: sheet structure

One spreadsheet per client, with these tabs:

| Tab | Contents |
|---|---|
| `Opportunities` | The core deliverable. All actionable rows, all markets, sorted by priority score descending |
| `Ignored` | Rows resolved to Ignore, with reason (answers Q5) — audit trail without cluttering the working queue |
| `Cannibalisation` | Merge candidates expanded, one row per affected URL (answers Q6) |
| `Run log` | Run date, settings snapshot, row counts, cost, per-source data availability |
| `Reference` | Column definitions and method notes, including the volume-deduplication caveat from Task 7.5 |

Market filtering should be a filter view on the market column, not separate tabs per market — this keeps cross-market comparison possible in one place, which matters directly for the cross-market propagation feature (Task 9.4).

### 11.2 Task: merge-on-write logic (answers Q9 — this is a hard requirement, not optional polish)

The sheet **will** be edited by humans between runs — assigned owners, status notes, manual overrides. A naive full rewrite on every run destroys that work and, per the risk register (Section 13), is flagged as a **High-impact risk that a single occurrence will destroy client trust in the tool.** Build it correctly the first time:

1. Read the current sheet into a dataframe keyed by the hidden `topic_uid` column.
2. Partition columns into **engine-owned** (columns 1–19/21, always overwritten on each run) and **human-owned** (any column added to the right of the engine block — never touched by the export job).
3. For each incoming row from the current run:
   - Existing UID found → update engine-owned columns in place, leave human-owned columns untouched.
   - New UID → append as a new row, visually highlighted as new.
   - A UID present in the sheet but absent from this run's output → move that row to an `Archived` tab with a stated reason, rather than silently deleting it.
4. Write via a single `batch_update` call to stay within Google Sheets API quota limits.

### 11.3 Task: formatting

Conditional formatting on the Action and Priority-score columns/bands, frozen header row, data validation on the Action column so manual human overrides stay within the allowed value set, number formatting on volume columns, and a `Last updated` timestamp per row to make staleness visible at a glance.

---

## 12. API surface (small — the sheet + Django admin are the primary interfaces)

```
POST   /api/v1/clients/{id}/runs/          trigger a run  {run_type, markets}
GET    /api/v1/runs/{id}/                  status, stage progress, cost
GET    /api/v1/runs/{id}/opportunities/    paginated rows, filterable
POST   /api/v1/runs/{id}/rescore/          re-run stages 8–9 with changed weights
POST   /api/v1/runs/{id}/export/           re-export to Sheets
GET    /api/v1/clients/{id}/settings/      resolved effective settings
PATCH  /api/v1/clients/{id}/settings/      update thresholds
GET    /api/v1/topics/{uid}/history/       how this topic has moved across runs
```

**Why `/rescore` matters specifically:** it's the direct payoff of separating stages 1–3 (paid, external) from stages 4–9 (free, internal — see Section 5's structural note). It turns threshold tuning into a seconds-long, zero-external-cost operation, which is what makes the "thresholds must be adjustable per client" requirement genuinely usable in practice rather than technically-present-but-expensive-to-use.

---

## 13. Non-functional requirements

### 13.1 Cost per run (indicative — one client, 4 markets, 5 competitors each)

| Item | Volume | Est. cost |
|---|---|---|
| Labs keyword research | ~80k items | $8–12 |
| Labs competitor gaps | ~60k items | $6–10 |
| Labs relevant pages | ~10k items | $1–2 |
| Bulk difficulty | ~40k keywords | $2–4 |
| SERP calls (shortlist only) | ~4k keywords | $8–15 |
| GSC / GA4 | — | free |
| LLM labelling (cached) | ~500 clusters | < $1 |
| **Total per full run** | | **~$25–45** |

Weekly GSC-delta runs are near-free by comparison. `max_spend_per_run_usd` (from `EngineSettings`) must **halt the run and report partial completion** — not silently overspend — and this guardrail should exist before the first live client run, not be retrofitted after a surprise invoice.

### 13.2 Performance target

Full run for one client, four markets, in under 45 minutes. Clustering is the dominant cost. Mitigations, all covered above: blocking (Task 7.2), sparse distance matrices, per-market parallelism across Celery workers, HNSW index on embeddings. Markets are independent of each other until Stage 7 (cross-market), so they can fan out across workers cleanly.

### 13.3 Security

Per-client credential isolation, encrypted at rest (`django-fernet-fields` or the platform secret store). OAuth refresh tokens must never appear in logs. Google service accounts scoped read-only for GSC/GA4, write-access restricted to the specific target spreadsheet only. Structured logs must redact credentials by processor logic, not by convention/discipline.

### 13.4 Observability

Bind `run_id` and `client_id` to every log line. Per-stage record counts in/out make silent data loss visible — a stage that ingests 80k keywords and emits 200 topics has either clustered very aggressively (possibly correctly) or broken, and only the recorded counts distinguish the two cases. Alert on: run failure, stage duration exceeding 2× the rolling median, spend above threshold, row count deviating more than 40% from the previous run, and any external API returning sustained errors.

### 13.5 Testing strategy

- Unit tests on every decision rule and scoring component individually — these encode client-agreed business logic and must not silently drift.
- VCR.py cassettes for all external API calls, so the full test suite runs offline and deterministically.
- A **golden-dataset test**: a fixed keyword set with hand-labelled correct clusters, asserting clustering quality doesn't regress below a set threshold when parameters change. This is the test that actually protects the engine's real-world output quality — prioritise building this early, not as an afterthought in Phase 7.
- End-to-end test against a seeded fixture client, writing to a scratch spreadsheet.

---

## 14. Delivery phasing

| Phase | Scope | Output |
|---|---|---|
| **1 — Foundations** | Django project skeleton (Task 4.1), all core models (Section 6), admin, Celery, connector interface, DataForSEO + GSC ingest, `RawFetch` storage | Data flowing in, visible in Django admin |
| **2 — Core engine** | Normalisation, clustering (Section 7), existing-page matching, decision matrix (Section 9), basic scoring | Topics with assigned actions in the database |
| **3 — Output** | Sheet export, merge-on-write (Task 11.2), all P0 columns, run log | A sheet the client can actually use for the first time |
| **4 — Enrichment** | P1 columns: difficulty, page type, AI opportunity, intent, confidence, slugs | Full column set populated |
| **5 — Commercial signal** | GA4 integration, data-driven conversion potential, estimated impact | Commercially weighted queue |
| **6 — Multi-market** | Cross-market propagation (check 8), DE/FR/NL language handling, per-market overrides | All four markets live simultaneously |
| **7 — Hardening** | Cost guardrails, alerting, golden-dataset tests, tuning against real client feedback | Production-ready |

**Why this order specifically:** Phases 1–3 alone produce a usable, if basic, deliverable — the client sees a real working sheet before any of the enrichment work lands. For a system whose entire value proposition depends on human trust in its output, getting *something concrete and correct* in front of the client early is more valuable than delivering a fuller column set later with no working checkpoint in between.

---

## 15. Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| **Clustering quality is inherently subjective**, and the client disagrees with the engine's topic boundaries | High — undermines confidence in the entire output | Agree a golden dataset with the client early; keep thresholds adjustable; ship Phase 2 output for client review *before* building further phases on top of it |
| GA4 connection delayed indefinitely | Medium — commercial weighting is the client's stated differentiator | Graceful degradation already designed in from Task 8.3; `conversion_basis` field keeps the sheet honest about which cells are inferred vs. measured |
| DataForSEO cost overruns at scale | Medium | Per-run spend cap (Task 13.1), request caching by hash (Task 6.3), SERP fetching hard-restricted to shortlist only (Task 8.1) |
| GSC data gaps for low-volume queries | Medium — quick wins and decay checks both depend on it | Treat absence as *unknown*, never as *zero*; supplement with DataForSEO's own ranked-keyword data for the client's domain |
| Sheet grows beyond usability | Medium | `max_rows_per_run` setting, ignored rows separated to their own tab, priority sort as the default view |
| Human edits lost on re-export | **High** — one single occurrence destroys trust in the entire tool | Merge-on-write with stable UIDs (Task 11.2); archive rather than delete |
| German/Dutch compound-word handling degrades clustering quality | Medium | Language-specific normalisation (Task 7.1); per-market golden datasets, not just an English one |
| Requirements drift as future engines (2–N) get specified | Medium | Connector and stage boundaries kept strictly clean (Section 4.1); Engine 1's `Opportunity` model is the interface other future engines will consume — treat it as a stable contract |

---

## 16. What must come from the client before / during the build

This list is the action-item version of Section 0 — use it as the actual outreach checklist:

1. **Category taxonomy** — how the site is organised, mapped to URL patterns. Blocks column 3 entirely (Q11).
2. **Competitor lists per market** — engineering can suggest a list via DataForSEO, but the client's own view should take precedence (Q14).
3. **Seed keywords or category terms** per market, to anchor keyword research (Q15).
4. **GSC and GA4 access**, plus confirmation of the auth model — service account vs. OAuth (Q13).
5. **Confirmation on the four open items** from Section 2.1 of the design doc: estimated impact method (Q1), priority score promotion to P0 (Q2), and whether intent/confidence are in or out (Q3).
6. **Run cadence and market rollout order** (Q8, Q16).
7. **`value_per_conversion` figure** — needed before Estimated Impact can be expressed in revenue terms rather than raw traffic (Q12).

---

## 17. Summary for a coding agent picking this up fresh

If you are an AI assistant being handed this document to implement:

1. **Do not start writing pipeline logic before the models in Section 6 exist** — every stage in Section 5 reads from and writes to these tables, and getting the schema right first avoids expensive rework.
2. **Build Stages 1–3 (ingest/normalise/enrich) and Stages 4–9 (cluster through export) as genuinely separable code paths** — this is not a stylistic preference, it's the property that makes `/rescore` (Task 12) possible and keeps the expensive, billable part of the system isolated from the free, iterable part.
3. **Treat Section 7 (clustering) as the highest-effort, highest-scrutiny part of the codebase.** Write the golden-dataset test (Task 13.5) early enough to actually guide clustering development, not after the fact.
4. **Never let the LLM touch clustering, scoring, or the action decision** (Task 3.4) — those three subsystems must stay deterministic and traceable via `decision_trace`.
5. **Check Section 0 before starting any task** — several tasks have a stated blocking question; where a safe default is given, proceed with it and flag the assumption in code comments and PR descriptions; where no default exists (Q11, Q12, Q13, Q14, Q15, Q16), that task should be stubbed with a clear TODO rather than guessed at.
6. **The sheet is the whole product from the client's point of view.** Every other part of this system — however architecturally elegant — is only as valuable as the row it eventually produces in the `Opportunities` tab. When in doubt about where to spend implementation effort, bias toward whatever most directly improves the correctness or trustworthiness of that final row.

---

*End of handover document. Source materials: client requirements doc (3-page draft, "ENGINE 1 — Find Opportunities — Requirements") and engineering technical design response v0.1, August 2026.*

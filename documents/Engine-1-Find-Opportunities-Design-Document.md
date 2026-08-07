# Engine 1 — Find Opportunities
## Technical Design Document

| | |
|---|---|
| **Version** | 0.1 — for internal review |
| **Status** | Draft, responding to client requirements doc "ENGINE 1 — Find Opportunities — Requirements" |
| **Date** | August 2026 |
| **Scope** | Discovery and prioritisation engine only. Content generation, publishing and reporting are out of scope. |

---

## 1. What we are building

A scheduled pipeline that pulls SEO data from three external providers plus the client's own sitemap, groups keywords into **topics**, checks each topic against pages that already exist, decides an **action** for it (create / optimise / merge / ignore), scores it, and writes the result to a **single Google Sheet — one row per topic per market**.

The engine is a decision system, not a data dump. Its output is a work queue. That framing drives most of the design choices below: raw data is cheap to collect and expensive to act on, so the hard parts of this build are clustering, deduplication, and scoring — not the API integrations.

### 1.1 The nine checks, and where each one actually comes from

The requirements list nine checks against "three sources." In practice there are four sources, and one check is derived rather than fetched. Restating explicitly:

| # | Check | Source | Notes |
|---|---|---|---|
| 1 | Keyword research | DataForSEO Labs | Seeded from client's own terms, categories and competitor sets |
| 2 | Competitor gaps | DataForSEO Labs | Domain Intersection with intersections disabled |
| 3 | Competitor top pages | DataForSEO Labs | Relevant Pages endpoint |
| 4 | Search Console performance | Google Search Console | Query × Page × Country dimensions |
| 5 | Sitemap / existing pages | **Client sitemap (4th source)** | Crawled, not an API |
| 6 | Ranking decay | Google Search Console | Requires historical snapshots we store ourselves |
| 7 | Quick wins | Google Search Console | Positions 7–20 band |
| 8 | Working in other markets | **Derived internally** | Cross-market join over our own topic table — no external source |
| 9 | Conversion performance | GA4 | Landing-page level conversion, rolled up to category |

Two consequences worth naming now:

- **Check 6 (decay) cannot work on day one.** Search Console gives us up to 16 months of history, so we can backfill a baseline on first run — but "previous position" is only trustworthy once we have our own snapshot series. First run produces a baseline; decay rows start appearing from run two onward.
- **Check 8 needs the topic table to exist across markets before it can fire.** It runs as a second pass after all markets have been clustered, not as an ingestion step.

---

## 2. Gaps and decisions needed

These are things the requirements leave open. Each has a recommendation so the client can confirm or override rather than start from a blank page.

### 2.1 Open items already flagged by the client

**Estimated impact (column 18)** — left blank pending an agreed method. Recommendation in §10. Short version: use a position-to-CTR curve, so impact is `search volume × (CTR at target position − CTR at current position) × conversion rate`. This is calculable today for everything except the conversion rate, which needs GA4.

**Priority score (column 19) should be P0.** The client is right to question this. "Scored, not just collected" is listed as a rule, and the sheet is a work queue — a queue with an optional ordering column is not a queue. Recommend promoting to P0. The score is computed from data we always have (volume, position, signal type), with conversion weight degrading gracefully to a neutral value when GA4 is absent.

**Intent and confidence score** — recommend including both, at P1:
- *Intent* (informational / commercial / transactional / navigational) is nearly free. DataForSEO returns SERP feature composition, and intent is a strong input to both page type (column 13) and AI search opportunity (column 17). Dropping it means those two columns get guessed instead of derived.
- *Confidence* is how sure the engine is about its own action recommendation. Valuable specifically because the engine will sometimes be wrong about "New" vs "Optimise" — a confidence figure tells a human editor which rows to spot-check. Low cost, high trust value.

### 2.2 Gaps we found on review

| Gap | Why it matters | Recommendation |
|---|---|---|
| No definition of "topic" | The core unit of output. "Running shoes for flat feet" and "best shoes flat feet" — one topic or two? | Define by SERP overlap, not by wording. See §8. |
| "Ignore" action has no criteria | Rule says every row is actionable and *ignore* counts as an action — but a sheet full of ignores is noise | Ignored rows written to a separate tab, not the main one. Keeps the audit trail without polluting the queue. |
| Cannibalisation is a stated concern but has no output | Rule 1 says checking prevents cannibalisation, but there's no column that surfaces it when it already exists | The `Merge` action plus a multi-value Target URL covers this. Flag when 2+ of our URLs rank for the same topic. |
| Decay trigger "drop out of top 3–5" is ambiguous | Is the trigger leaving position 3, or position 5? | Model as: was ≤ `decay_from` (default 5) in baseline window, now > `decay_to` (default 5) plus a minimum drop of N positions to filter noise. Both adjustable. |
| No refresh cadence specified | Affects cost and infrastructure | Recommend monthly full run, weekly GSC-only delta run. Configurable per client. |
| Sheet is written by us but edited by humans | Next run would overwrite an editor's notes | Stable row keys plus merge-on-write. See §11. |
| No stated language handling for DE/FR/NL | Stemming, stopwords and normalisation are language-specific | Per-market language config with spaCy models. See §8.1. |
| "Category" (column 3) source undefined | Could be from URL structure, GA4, or a client-supplied taxonomy | Recommend client-supplied taxonomy mapped to URL path patterns, with a fallback classifier. Needs client input. |

---

## 3. Architecture

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

### 3.1 Pipeline stages

The run is a linear chain of stages. Each stage is idempotent and restartable from the previous stage's persisted output — this matters because DataForSEO calls cost money and we do not want a transform bug to force a re-fetch.

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

Stages 1–3 are the only ones that touch external APIs. Stages 4–9 run entirely from the database, so the expensive part of the pipeline runs once and the analytical part can be re-run freely while we tune thresholds. That separation is deliberate and is the single most important structural decision in this design.

---

## 4. Technology stack

You asked for Python and leaned Django — that is the right call here, and not only on preference. This system is config-heavy (per-client thresholds, market definitions, competitor lists, weights), and Django admin gives us a usable back office for that on day one without building a UI. The parts of the problem that are actually hard are data-shaped, and Python owns that ecosystem.

### 4.1 Core

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Match to the data/ML libraries this needs |
| Framework | Django 5.x | ORM, migrations, admin, auth. Admin alone covers the entire per-client settings requirement |
| API | Django REST Framework | Run triggering, status, row inspection |
| Database | PostgreSQL 16 + `pgvector` | JSONB for raw payloads, vectors for semantic clustering, one engine instead of two |
| Task queue | Celery 5 + Redis | Long-running, retryable, rate-limited external calls. Chains and chords map directly onto the stage model |
| Scheduling | `django-celery-beat` | DB-backed schedules so per-client cadence is editable in admin, not in code |
| Results | `django-celery-results` | Task history persisted for debugging failed runs |

### 4.2 Data and processing

| Concern | Choice | Why |
|---|---|---|
| Transform | **Polars** (pandas fallback) | 100k+ keyword rows per client per run; Polars is materially faster and its lazy API keeps memory flat |
| External payload validation | **Pydantic v2** | DataForSEO responses are deeply nested and change occasionally. Parse at the boundary, fail loudly, keep the rest of the codebase clean |
| HTTP | **httpx** + **tenacity** | Async pooling for bulk fetches, declarative retry/backoff |
| Clustering | **scikit-learn** (agglomerative) + **scipy** sparse | SERP-overlap distance matrix, see §8 |
| Embeddings | `sentence-transformers` with a multilingual model (e.g. `paraphrase-multilingual-mpnet-base-v2`), stored in pgvector | Must handle EN/DE/FR/NL in one vector space. Self-hosted keeps per-run cost at zero |
| Language processing | **spaCy** with per-language models | Lemmatisation and stopwords for `en_core_web_sm`, `de_`, `fr_`, `nl_` |
| Sitemap crawl | `advertools` or `ultimate-sitemap-parser` + httpx | Handles sitemap indexes, gzip, nested sets |
| Sheets | **gspread** + `google-auth` (service account) | Batch writes, named ranges, formatting |
| Google APIs | `google-api-python-client` (GSC) + `google-analytics-data` (GA4) | Official clients |

### 4.3 Operations

| Concern | Choice |
|---|---|
| Config | `django-environ`, 12-factor, secrets in the platform secret store |
| Logging | `structlog` → JSON, with `run_id` and `client_id` bound to every line |
| Errors | Sentry |
| Task visibility | Flower, plus a Django admin run dashboard |
| Containers | Docker + Compose for local, same images in deployment |
| Testing | `pytest` + `pytest-django`, `factory_boy`, **VCR.py cassettes** for external APIs |
| Quality | `ruff`, `mypy` (strict on the domain layer), `pre-commit` |

### 4.4 Where an LLM is and is not used

An LLM is genuinely useful in three narrow places and a liability everywhere else:

1. **Topic labelling** — turning a cluster of keywords into "the subject of the page, in plain language" (column 1). Deterministic labelling gives ugly output here.
2. **Category classification** — where no taxonomy mapping exists.
3. **Slug suggestion** — column 14, with deterministic post-processing to enforce format.

It is explicitly *not* used for clustering, action decisions, scoring or difficulty. Those must be reproducible and explainable — an editor needs to be able to ask "why is this row here" and get an answer from the `why_flagged` field, not from a model's mood. Every LLM call is cached by input hash so re-runs cost nothing.

### 4.5 Alternatives considered

- **FastAPI + SQLModel** — leaner, but we would rebuild admin CRUD for config. Django's admin is worth more here than FastAPI's ergonomics.
- **Airflow / Dagster** for orchestration — better DAG visibility, but heavy for a nine-stage linear chain and adds a second deployment surface. Celery chains cover it. Revisit if engines 2–N add branching dependencies.
- **DuckDB** as the analytical layer — attractive for the transform stage, but splitting state across two engines complicates the "re-run stages 4–9 freely" property. Postgres holds it all.

---

## 5. Django project layout

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
│   ├── pages/              existing page index, embeddings, cannibalisation matching
│   ├── opportunities/      decision rules, scoring, Opportunity model
│   ├── exports/            sheet builder, gspread writer, merge-on-write
│   ├── runs/               Run, RunStage, orchestration tasks, status API
│   └── api/                DRF viewsets and serializers
└── tests/
```

The `connectors` boundary matters: every connector returns a `RawFetch` row and nothing else. No connector knows what a Topic is. That keeps provider changes contained and makes it possible to add a fifth source later without touching the pipeline.

---

## 6. Data model

Abbreviated to the fields that carry design weight.

### 6.1 Client and configuration

```python
class Client(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    primary_domain = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)


class Market(models.Model):
    """One row per (client, country, language). UK, DE, FR, NL to start."""
    client = models.ForeignKey(Client, related_name="markets", on_delete=models.CASCADE)
    code = models.CharField(max_length=10)              # "UK", "DE"
    country_iso = models.CharField(max_length=2)        # "GB", "DE"
    language_code = models.CharField(max_length=5)      # "en", "de"
    dataforseo_location_code = models.IntegerField()    # 2826 = United Kingdom
    gsc_property = models.CharField(max_length=255)     # sc-domain:... or URL prefix
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

### 6.2 Settings — the adjustable thresholds

The requirements call out three adjustable settings. Building only those three would be short-sighted; every threshold in the engine will be argued about eventually. One settings row per client, with per-market override.

```python
class EngineSettings(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    market = models.ForeignKey(Market, null=True, blank=True, on_delete=models.CASCADE)
    # null market = client-wide default

    # --- explicitly required by the client ---
    quick_win_min_position = models.FloatField(default=7.0)
    quick_win_max_position = models.FloatField(default=20.0)
    decay_baseline_max_position = models.FloatField(default=5.0)   # "was in top 3-5"
    decay_current_min_position = models.FloatField(default=5.0)    # "has dropped out"
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
    """Priority score weights. Separated so they can be tuned without touching thresholds."""
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

Resolution order at runtime: market override → client default → code default. Wrapped in a `SettingsResolver` so no call site reads settings directly.

### 6.3 Ingestion

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

`request_hash` enables a cache window: if an identical request was made within `cache_ttl` for this source, reuse the payload instead of paying again. Keyword volume data does not change daily; this alone cuts repeat-run cost substantially.

```python
class KeywordObservation(models.Model):
    """One keyword as seen by one source in one market at one point in time."""
    run = models.ForeignKey("runs.Run", on_delete=models.CASCADE)
    market = models.ForeignKey(Market, on_delete=models.CASCADE)
    keyword = models.CharField(max_length=500, db_index=True)
    keyword_normalised = models.CharField(max_length=500, db_index=True)
    source = models.CharField(max_length=32)
    signal = models.CharField(max_length=40)          # maps to why_flagged

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
    serp_top_urls = models.JSONField(default=list)    # top 10 for clustering
    intent = models.CharField(max_length=20, blank=True)
    embedding = VectorField(dimensions=768, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["run", "market", "keyword_normalised"]),
            models.Index(fields=["market", "signal"]),
        ]
```

One keyword can produce several observations — found in keyword research, ranking in GSC, and held by a competitor. That is intentional: the union of signals per keyword is exactly what column 11 (`Why flagged`) needs, and collapsing early would lose it.

### 6.4 Existing pages

```python
class ExistingPage(models.Model):
    market = models.ForeignKey(Market, on_delete=models.CASCADE)
    url = models.URLField(max_length=1000)
    path = models.CharField(max_length=1000, db_index=True)
    title = models.CharField(max_length=500, blank=True)
    h1 = models.CharField(max_length=500, blank=True)
    meta_description = models.TextField(blank=True)
    category = models.CharField(max_length=200, blank=True)
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

### 6.5 Topics and opportunities

```python
class Topic(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    market = models.ForeignKey(Market, on_delete=models.CASCADE)
    topic_uid = models.CharField(max_length=64, unique=True)  # stable across runs
    label = models.CharField(max_length=300)                  # plain language, column 1
    primary_keyword = models.CharField(max_length=500)
    primary_keyword_volume = models.IntegerField(default=0)
    total_search_volume = models.IntegerField(default=0)
    category = models.CharField(max_length=200, blank=True)
    intent = models.CharField(max_length=20, blank=True)
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
    """One row in the output sheet."""
    run = models.ForeignKey("runs.Run", on_delete=models.CASCADE)
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE)
    market = models.ForeignKey(Market, on_delete=models.CASCADE)

    action = models.CharField(max_length=20)                # new|optimise|merge|ignore
    target_urls = models.JSONField(default=list)            # >1 implies merge
    why_flagged = models.JSONField(default=list)            # signals, ordered by strength

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
    estimated_impact = models.JSONField(null=True)
    priority_score = models.FloatField(null=True)
    confidence = models.FloatField(null=True)
    decision_trace = models.JSONField(default=dict)   # why the engine chose this action

    class Meta:
        unique_together = [("run", "topic")]
        indexes = [models.Index(fields=["run", "-priority_score"])]
```

`decision_trace` is not decoration. When an editor disputes a row, this field is the answer — which rule fired, which thresholds applied, which competing action was runner-up. It is also what makes the scoring model tunable, because we can replay decisions against changed weights.

### 6.6 Runs

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

`settings_snapshot` means every sheet is reproducible. Six weeks later, when someone asks why a topic scored 84 in June and 61 in July, the answer is recoverable.

---

## 7. Ingestion detail

### 7.1 DataForSEO

All DataForSEO Labs endpoints run in live mode. Billing is roughly **$0.01 per task plus $0.0001 per returned item**, so cost scales with how much we ask for, not how often. That shapes the fetch plan below.

| Check | Endpoint | Key params |
|---|---|---|
| Keyword research | `/v3/dataforseo_labs/google/keyword_ideas/live`, `.../keyword_suggestions/live`, `.../related_keywords/live` | seed keywords, `location_code`, `language_code`, `limit` |
| Competitor discovery | `/v3/dataforseo_labs/google/competitors_domain/live` | our domain — validates the client's competitor list |
| Competitor gaps | `/v3/dataforseo_labs/google/domain_intersection/live` with intersections disabled | `target1` = competitor, `target2` = us → keywords they rank for and we do not |
| Competitor top pages | `/v3/dataforseo_labs/google/relevant_pages/live` | competitor domain, ordered by estimated traffic |
| Difficulty | `/v3/dataforseo_labs/google/bulk_keyword_difficulty/live` | up to 1,000 keywords per call |
| SERP features + top URLs | `/v3/serp/google/organic/live/advanced` | **shortlisted keywords only** |

Two notes worth building around:

- The Labs responses now include a `keyword_properties` object by default, carrying `keyword_difficulty`, `core_keyword` and a synonym clustering algorithm marker. `core_keyword` is a free first-pass clustering signal and should be used as a pre-grouping hint before we spend anything on SERP calls.
- **SERP fetching is the cost centre.** A live advanced SERP call is orders of magnitude more expensive per keyword than a Labs item. We therefore never fetch SERPs for the full keyword universe. Only the shortlist survives to §8 stage C — typically 5–15% of raw keywords. `max_serp_calls_per_run` enforces this as a hard stop.

Rate limiting is handled with a Celery per-queue rate limit plus a token bucket in Redis, so parallel workers cannot collectively exceed the account limit.

### 7.2 Google Search Console

Auth via service account with domain-wide delegation, or per-client OAuth refresh token — the client's Google Workspace policy decides which. Pull `searchAnalytics.query` with dimensions `[query, page, country, date]`.

Constraints to design around:
- 25,000 rows per request, paginated by `startRow`.
- 16 months of history available — enough for a first-run decay baseline.
- Data is incomplete for low-volume queries (anonymised), so absence of a query is not evidence of zero impressions.
- There is a 2–3 day reporting lag; comparison windows must be offset accordingly or every run will show a phantom decline at the recent edge.

We store our own daily aggregates in `PositionSnapshot` rather than relying on GSC's history at query time. Decay detection then reads from our table, which is faster and lets us define the baseline window ourselves.

### 7.3 GA4

`runReport` on the Data API with dimensions `[landingPagePlusQueryString, sessionDefaultChannelGroup]` and metrics `[sessions, conversions, purchaseRevenue, keyEvents]`, filtered to organic search. Rolled up from landing page to category using the same URL-pattern taxonomy that populates column 3.

The requirement is explicit that GA4 is a dependency, not a blocker. Concretely, when GA4 is absent:
- `conversion_potential` is set from a rules-based inference (intent + category + page type) and `conversion_basis` is marked `inferred`, so the sheet is honest about which cells are data and which are judgement.
- `w_conversion` is redistributed proportionally across the remaining scoring weights rather than treated as zero — otherwise every topic gets penalised equally and the ordering silently changes character.

### 7.4 Sitemap

Fetch `robots.txt` → sitemap index → all child sitemaps, handling gzip and nesting. For each URL, fetch title, H1 and meta description (throttled, respecting robots). Discovered pages are cross-checked against GSC pages: anything ranking but absent from the sitemap is flagged as an orphan, which is itself a small opportunity signal.

---

## 8. Topic clustering

This is the heart of the engine and the requirement that will most affect perceived quality. "One row per topic, not one row per keyword" is easy to state and hard to do well. Cluster too aggressively and distinct pages get merged into one unusable row; too loosely and the sheet becomes the keyword list the client explicitly said they did not want.

**Definition we propose:** two keywords belong to the same topic if a single page could rank for both. The operational proxy for that is SERP overlap — if Google returns substantially the same results for both queries, Google considers them the same intent, and one page can serve both.

### 8.1 Stage A — normalisation (per language)

Lowercase, strip punctuation and extra whitespace, remove language-specific stopwords, lemmatise with the market's spaCy model, sort tokens for a canonical form. Language specifics that matter:

- **German** — compound nouns are the main hazard (`Laufschuhe` vs `Lauf Schuhe`). Compound splitting is applied so both reach the same normalised form. Umlauts are preserved and also indexed in transliterated form (`ü` ↔ `ue`), because searchers type both.
- **French** — accent-insensitive matching, elision handling (`l'`, `d'`).
- **Dutch** — compounding as in German, plus `ij` normalisation.

Output: `keyword_normalised`, plus a token set for blocking.

### 8.2 Stage B — blocking

Comparing every keyword to every other is O(n²) and unaffordable at 100k+ keywords. Instead, generate candidate pairs only within blocks:

1. Group by `core_keyword` where DataForSEO supplied one.
2. Group by shared rare token (inverse-document-frequency weighted, so "best" and "shoes" do not create one giant block).
3. Group by approximate-nearest-neighbour lookup on the embedding vector, using a pgvector HNSW index, top-k = 50.

Union of the three gives a candidate pair set typically 2–4 orders of magnitude smaller than the full cross product.

### 8.3 Stage C — similarity

For each candidate pair, compute a blended similarity:

```
sim(a, b) = w_serp · serp_overlap(a, b)
          + w_sem  · cosine(emb_a, emb_b)
          + w_lex  · jaccard(tokens_a, tokens_b)
```

`serp_overlap` is the count of shared URLs in the top 10, normalised. It is the strongest signal and gets the highest weight — but it requires a SERP call per keyword, so it is only available for the shortlist. The shortlist is selected by volume, by whether we already rank, and by whether the keyword arrived from a high-value signal (decay, quick win, competitor gap).

Where SERP data is missing, `w_serp` is set to zero and the remaining weights renormalise. Pairs resolved without SERP evidence carry a lower confidence, which propagates to the row's `confidence` field.

### 8.4 Stage D — clustering

Agglomerative clustering with complete linkage on distance `1 − sim`, cut at `1 − semantic_similarity_threshold`. Complete linkage specifically, not single linkage — single linkage chains, and chaining is exactly the failure mode that produces a 400-keyword "topic" spanning three unrelated subjects.

Post-cluster validation splits any cluster where the maximum internal distance exceeds a ceiling, or where two distinct dominant intents are present.

### 8.5 Stage E — selection and labelling

- **Primary keyword** — highest search volume in the cluster. Ties broken by shortest form, then by best existing position.
- **Secondary keywords** — the rest, ordered by volume, capped at a configurable number for sheet readability.
- **Total search volume** — sum across the cluster, **deduplicated by normalised form** so near-identical variants do not inflate the figure. This is worth stating in the sheet's documentation tab, because summed volume is routinely overstated in SEO tooling and the client may compare our number against another tool's.
- **Label** — LLM-generated plain-language phrase from the keyword set, cached by cluster hash.

### 8.6 Stage F — stable identity across runs

A topic must keep the same identity between runs, or the sheet cannot be diffed and human edits cannot be preserved. `topic_uid` is derived from the sorted set of the top-N keywords plus market. On each run, new clusters are matched to existing topics by centroid similarity and keyword overlap before a new UID is minted. Merges and splits between runs are recorded so the change is visible rather than silent.

---

## 9. Matching, cannibalisation, and the action decision

### 9.1 Matching a topic to existing pages

Three independent matchers, run in order, each producing a score:

1. **Ranking match** — does any of our URLs already rank for the primary or a secondary keyword, per GSC? Strongest signal; a page ranking for a keyword *is* a page about that topic.
2. **Content match** — cosine similarity between the topic centroid and each `ExistingPage` embedding (built from title + H1 + meta), via pgvector.
3. **Slug match** — normalised path tokens against topic tokens. Weakest, used as a tiebreaker.

Combined match score above `existing_page_match_threshold` means the page is a candidate target.

### 9.2 Cannibalisation

If two or more of our URLs match the same topic above threshold **and** both rank for overlapping keywords in GSC, that is cannibalisation. It produces a `merge` action with multiple entries in `target_urls`, and the row carries a note identifying which URL should be the canonical survivor — chosen by combined clicks, conversions and inbound authority.

This is one of the higher-value outputs of the engine and the requirements only imply it. Worth calling out to the client explicitly.

### 9.3 Decision matrix

| Existing page match | Our current position | Additional condition | → Action | Target URL |
|---|---|---|---|---|
| None | — | Volume ≥ min, difficulty ≤ max | **New content** | blank |
| None | — | Volume < min threshold | **Ignore** | blank |
| None | — | Topic already queued this run | **Ignore** (duplicate) | blank |
| One | 1–3 | No decay detected | **Ignore** (already winning) | matched URL |
| One | 1–3 | Decay detected | **Optimise** | matched URL |
| One | 4–20 | — | **Optimise** | matched URL |
| One | > 20 | Page thin or off-intent | **Optimise** | matched URL |
| One | > 20 | Page strongly off-intent | **New content** | blank, note existing |
| Two or more | any | Overlapping rankings | **Merge** | all matched URLs |
| Two or more | any | No ranking overlap | **Optimise** best match | best URL |

Every path writes to `decision_trace`. Rows resolving to `ignore` go to a separate sheet tab as agreed in §2.2.

### 9.4 Cross-market propagation (check 8)

After all markets are clustered, topics are grouped into `cross_market_group` by translating the primary keyword to a pivot language and matching on the multilingual embedding. A topic performing well in one market — good position, meaningful traffic, converting — that has no counterpart topic in another market generates a `new content` row in that market, flagged `proven_in_other_market`.

Guardrail: the source market's performance must clear a minimum bar, and the target market must have non-trivial search volume for the translated keyword set. Otherwise we would propagate UK topics into DE with no German demand behind them.

---

## 10. Column derivation map

Every column in the requirements, with its source and priority. Where our recommendation differs from the document, it is marked.

| # | Column | P | Derivation |
|---|---|---|---|
| 1 | Topic | P0 | Cluster label, LLM-generated from the keyword set (§8.5) |
| 2 | Market | P0 | `Market.code` — every row belongs to exactly one market |
| 3 | Category | P1 | URL-pattern taxonomy on the matched page; classifier fallback for new content. **Needs client taxonomy input** |
| 4 | Primary keyword | P0 | Highest-volume cluster member, formatted `keyword (1,200)` |
| 5 | Secondary keywords | P0 | Remaining members with volumes, volume-ordered, capped |
| 6 | Total search volume | P0 | Deduplicated cluster sum (§8.5) |
| 7 | Current position | P0 | Weighted average GSC position for cluster keywords, last 28 days. `—` where no page exists |
| 8 | Previous position | P1 | Same measure over the baseline window. Populated for decay rows; blank on first run |
| 9 | Action | P0 | Decision matrix (§9.3) |
| 10 | Target URL | P0 | Matched page(s). Multiple → merge. Blank for new content |
| 11 | Why flagged | P1 | Ordered signal list from `KeywordObservation.signal`. **Recommend P0** — this is the row's justification and reviewers will need it |
| 12 | Difficulty | P1 | DataForSEO keyword difficulty for the primary keyword, adjusted by competitor domain strength in the SERP; bucketed Low / Medium / High |
| 13 | Page type | P1 | Derived from intent + SERP result composition (what format is currently ranking) |
| 14 | Suggested slug | P1 | Slugified primary keyword, language-appropriate, validated against existing paths for collisions |
| 15 | Conversion potential | P1 | GA4 conversion rate of the matched page or category vs site median → High/Med/Low. `conversion_basis` records whether this was data or inference |
| 16 | Competitor URL | P1 | Best-ranking competitor URL for the primary keyword |
| 17 | AI search opportunity | P1 | Yes/No — see §10.1 |
| 18 | Estimated impact | — | Blank per requirements. Method proposed in §10.2 |
| 19 | Priority score | P1 | Scoring model §10.3. **Recommend P0** |
| 20 | Intent | *new* | Informational / commercial / transactional / navigational |
| 21 | Confidence | *new* | Engine's confidence in the action recommendation, 0–1 |
| 22 | Topic UID | *hidden* | Stable key for merge-on-write. Hidden column, not for human consumption |

### 10.1 AI search opportunity

Yes when the SERP shows an AI Overview or People Also Ask block **and** intent is informational or commercial-investigational **and** the topic is answerable in a structured, extractable way (definition, comparison, list, how-to) **and** we have plausible topical authority — meaning we already rank in the top 20 for something in the same category.

The last condition matters. Being cited in an AI answer for a topic where the site has no standing is not realistic, and marking such rows Yes would send writers after work that cannot pay off.

### 10.2 Estimated impact — proposed method for the open item

```
Δtraffic = total_search_volume × (CTR(target_position) − CTR(current_position))
Δconv    = Δtraffic × conversion_rate(category)
Δvalue   = Δconv × value_per_conversion
```

Where:
- `CTR(position)` comes from a configurable position→CTR curve, ideally fitted to the client's own GSC data rather than an industry table. We have that data, so we should use it — a client-specific curve is defensible in a way a borrowed one is not.
- `target_position` is set by action: new content → 8, optimise → current minus a realistic gain band, merge → best of the merged set minus a gain band. Adjustable.
- `conversion_rate` and `value_per_conversion` come from GA4. Without GA4 the model still produces Δtraffic, which is useful on its own.

Recommend the sheet carry `Estimated impact` as monthly incremental clicks by default, with revenue shown only once GA4 is connected. Presenting a revenue figure derived from an inferred conversion rate would be a false precision problem.

### 10.3 Priority score

Normalised weighted sum, 0–100, all weights per-client:

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

- `log_scaled` volume, because the difference between 100 and 1,000 searches matters more than between 40,000 and 41,000.
- `position_opportunity` peaks in the quick-win band. A topic at position 11 has more available upside per unit of effort than one at position 2 or one at position 90.
- `signal_weight` defaults: decay 1.0, quick win 0.95, competitor gap 0.8, conversion-proven category 0.9, cross-market proven 0.75, keyword research 0.6. These encode the client's own framing — a decaying page "already has authority, it's losing ground, not starting from zero."
- `market_weight` lets the client prioritise UK over NL, or the reverse, without touching anything else.

Scores are computed within a run and also rank-normalised, so the top of the queue is always the top of the queue regardless of absolute score drift between runs.

---

## 11. Google Sheets export

### 11.1 Structure

One spreadsheet per client. Tabs:

| Tab | Contents |
|---|---|
| `Opportunities` | The deliverable. All actionable rows, all markets, sorted by priority score descending |
| `Ignored` | Rows resolved to ignore, with reason. Audit trail without queue noise |
| `Cannibalisation` | Merge candidates expanded, one row per affected URL |
| `Run log` | Run date, settings snapshot, row counts, cost, data-source availability |
| `Reference` | Column definitions and method notes, including the volume-deduplication caveat |

Market filtering is done with a filter view on the market column rather than separate tabs, so cross-market comparison stays possible in one place.

### 11.2 Merge-on-write

The sheet will be edited by humans — assigned owners, status, notes. A naive full rewrite each run destroys that. Approach:

1. Read the current sheet into a dataframe keyed by hidden `topic_uid`.
2. Partition columns into **engine-owned** (1–19, always overwritten) and **human-owned** (any column added to the right of the engine block, never touched).
3. For each incoming row: existing UID → update engine columns in place, preserve human columns. New UID → append, highlighted as new. UID absent from this run → move to an `Archived` tab with the reason, rather than deleting.
4. Write via a single `batch_update` to stay within Sheets API quota.

### 11.3 Formatting

Conditional formatting on action and priority bands, frozen header, data validation on the action column so manual overrides stay in the allowed set, number formatting on volumes. A `Last updated` stamp per row makes staleness visible.

---

## 12. API surface

Small, because the primary interface is the sheet plus Django admin.

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

The `rescore` endpoint is the payoff for separating stages 1–3 from 4–9. Threshold tuning becomes a seconds-long operation with no external spend, which is what makes the "adjustable per client" requirement genuinely usable rather than nominally supported.

---

## 13. Non-functional considerations

### 13.1 Cost per run

Indicative, for one client with 4 markets and 5 competitors each:

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

Weekly GSC-delta runs are near-free. `max_spend_per_run_usd` halts the run and reports partial completion rather than silently overspending — a guardrail worth having before the first client, not after the first invoice.

### 13.2 Performance

Target: full run for one client, four markets, under 45 minutes. The clustering stage dominates. Mitigations: blocking (§8.2), sparse distance matrices, per-market parallelism across Celery workers, HNSW index on embeddings. Markets are independent until stage 7, so they fan out cleanly.

### 13.3 Security

Per-client credential isolation, encrypted at rest via `django-fernet-fields` or the platform secret store. OAuth refresh tokens never logged. Google service account scoped read-only for GSC and GA4, write only to the specific spreadsheet. Structured logs redact credentials by processor, not by convention.

### 13.4 Observability

`run_id` and `client_id` bound to every log line. Per-stage record counts in/out make silent data loss visible — a stage that takes 80k keywords and emits 200 topics has either worked very well or broken, and only the counts distinguish them. Alerts on: run failure, stage duration beyond 2× rolling median, cost above threshold, row count deviating more than 40% from the previous run, and any external API returning sustained errors.

### 13.5 Testing

- Unit tests on every decision rule and scoring component — these encode client-agreed business logic and must not drift.
- VCR.py cassettes for all external APIs, so the suite runs offline and deterministically.
- A golden-dataset test: a fixed keyword set with hand-labelled correct clusters, asserting clustering quality does not regress below a threshold when parameters change. This is the test that protects the engine's actual quality.
- End-to-end test against a seeded fixture client writing to a scratch spreadsheet.

---

## 14. Delivery phases

| Phase | Scope | Output |
|---|---|---|
| **1 — Foundations** | Django project, models, admin, Celery, connector interface, DataForSEO + GSC ingest, RawFetch storage | Data flowing in, visible in admin |
| **2 — Core engine** | Normalisation, clustering, existing-page matching, decision matrix, basic scoring | Topics with actions in the database |
| **3 — Output** | Sheet export, merge-on-write, all P0 columns, run log | A sheet the client can use |
| **4 — Enrichment** | P1 columns, difficulty, page type, AI opportunity, intent, confidence, slugs | Full column set |
| **5 — Commercial signal** | GA4 integration, conversion potential from data, estimated impact | Commercially weighted queue |
| **6 — Multi-market** | Cross-market check, language handling for DE/FR/NL, per-market overrides | All four markets live |
| **7 — Hardening** | Cost guardrails, alerting, golden-dataset tests, tuning against client feedback | Production |

Phases 1–3 produce a usable deliverable — the client sees a working sheet before the enrichment work lands, which is the right order for a system whose value depends on human trust in its output.

---

## 15. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Clustering quality is subjective** and the client disagrees with our topic boundaries | High — undermines confidence in the whole output | Golden dataset agreed with client early; thresholds adjustable; ship phase 2 output for review before building on it |
| GA4 delayed indefinitely | Medium — commercial weighting is the client's stated differentiator | Graceful degradation already designed in; `conversion_basis` keeps the sheet honest about which cells are inferred |
| DataForSEO cost overruns at scale | Medium | Per-run spend cap, request caching by hash, SERP fetching restricted to shortlist |
| GSC data gaps for low-volume queries | Medium — quick wins and decay both rely on it | Treat absence as unknown, never as zero; supplement with DataForSEO ranked-keyword data for our own domain |
| Sheet grows beyond usability | Medium | `max_rows_per_run`, ignored rows separated, priority sort as default |
| Human edits lost on re-export | High — one occurrence destroys trust in the tool | Merge-on-write with stable UIDs, archive rather than delete |
| German/Dutch compound handling degrades clustering | Medium | Language-specific normalisation; per-market golden datasets, not just English |
| Requirements drift as engines 2–N are specified | Medium | Connector and stage boundaries kept clean; Engine 1's output contract (`Opportunity`) is the interface other engines consume |

---

## 16. What we need from the client

1. **Category taxonomy** — how the site is organised, mapped to URL patterns. Blocks column 3.
2. **Competitor lists per market** — we can suggest them from DataForSEO, but the client's view should lead.
3. **Seed keywords or category terms** per market, to anchor keyword research.
4. **GSC and GA4 access**, and confirmation of the auth model (service account vs OAuth).
5. **Confirmation on the four open items** in §2.1 — estimated impact method, priority score promotion to P0, and whether intent and confidence are in or out.
6. **Run cadence and market rollout order.**
7. **Decision on `value_per_conversion`** — needed before estimated impact can be expressed in revenue terms.

---

*Draft for review. Sections 8, 9 and 10 carry the design decisions most worth challenging before build starts.*

# Engine 1 - Progress Tracker and Next-Step Plan

> **Living source of truth.** Reconciled against the handover, implementation plan, current code, migrations, and SQLite data.
>
> **Last reviewed:** 2026-08-10
>
> Status vocabulary:
> - **Verified** - implemented and supported by a test or real run evidence.
> - **Prototype** - code exists and executes, but required data or quality validation is missing.
> - **Not started** - required by the plan but no implementation exists.
> - **Blocked** - needs credentials or a client decision.

---

## 1. Executive status

Engine 1 is currently a **working local Django/SQLite prototype through Stage 8**. It can ingest cached or live DataForSEO data, normalise it, cluster keywords, create opportunities, and score them.

It is **not yet a complete or client-ready Engine 1**. The most important gap is that there are no existing-page records, GSC observations, or GA4 observations. Consequently, the current decision engine has no evidence with which to recommend `optimise`, `merge`, quick-win, or decay actions: all 90 current opportunities are `new_content`.

### Current build position

| Area | Status | Honest assessment |
|---|---|---|
| Django models and admin | Verified | Core configuration and pipeline records are usable locally. |
| DataForSEO ingestion and cache | Verified | Four pipeline endpoints have real RawFetch evidence. |
| Normalisation | Verified | Idempotent, database-constrained, bulk difficulty applied. |
| Enrichment | Prototype | Intent works when present; difficulty labels are not persisted; Advanced SERP is optional and not validated in the pipeline. |
| Clustering | Prototype | Market-isolated and rerun-safe, but still heuristic SQLite clustering, not the handover's SERP/embedding design. |
| Match, Decide, Score | Prototype | Code runs, but meaningful matching is impossible while `ExistingPage` is empty. |
| Cross-market Stage 7 | Not started | No implementation file and no multi-market validation dataset. |
| Google Sheets Stage 9 | In progress | Flat builder and CSV export verified; Sheets API and merge-on-write remain. |
| GSC, sitemap, GA4 connectors | Sitemap implemented; GSC/GA4 blocked | Sitemap connector and Stage 1b pass offline tests. The configured Nike URL returns 404 and must be corrected. |
| Production infrastructure | Not started | SQLite and synchronous command only; Celery package is installed but not wired. |
| Automated testing | In progress | Five stabilization tests pass when `apps.runs` is targeted; wider planned suite is absent. |

### Progress against the full handover plan

These percentages describe **full Engine 1 scope**, not just the DataForSEO demo:

| Delivery phase | Progress | Notes |
|---|---:|---|
| Phase 1 - Foundations | ~68% | DataForSEO and sitemap code complete; GSC, Celery/Redis, structured logging, and broader connector fixtures remain. |
| Phase 2 - Core engine | ~55% | Stages 4/5/6/8 exist, but matching and decisions lack page/GSC data and clustering is provisional. |
| Phase 3 - Output | ~20% | Builder and CSV command verified; Google Sheets integration remains. |
| Phase 4 - Enrichment | ~15% | Some intent/SERP fields exist; most P1 derivations remain. |
| Phase 5 - Commercial signal | 0% | GA4 and conversion-weighted scoring are absent. |
| Phase 6 - Multi-market | <10% | Market isolation exists; language handling and Stage 7 do not. |
| Phase 7 - Hardening | ~10% | Cache and initial idempotency tests exist; production operations do not. |

**Overall full-plan position: approximately 35-40%.**

---

## 2. What has been built

### 2.1 Models and admin

| Component | Status | Evidence |
|---|---|---|
| Client, Market, Competitor | Verified | `apps/clients/models.py` and admin registrations |
| EngineSettings, ScoringWeights | Verified | Configurable through Django admin |
| RawFetch, KeywordObservation | Verified | Populated by real DataForSEO runs |
| ExistingPage | Built, no data | Model/admin exist; database count is zero |
| Topic, TopicKeyword | Verified prototype | Current database contains topic clusters and assignments |
| Opportunity | Verified prototype | Current database contains scored opportunities |
| Run, RunStage | Verified | Stage counts, costs, partial/failure semantics, and errors tracked |

### 2.2 DataForSEO connector

| Capability | Status |
|---|---|
| Base connector and request hashing | Verified |
| 24-hour cache lookup | Verified |
| Cross-run cached RawFetch copy | Verified |
| Keyword Ideas | Verified in pipeline |
| Domain Intersection | Verified in pipeline |
| Relevant Pages | Verified in pipeline |
| Bulk Keyword Difficulty | Verified and now applied during normalisation |
| Competitor Discovery | Implemented, not wired into normal pipeline |
| Advanced SERP | Implemented and optional, not fully pipeline-validated |

### 2.3 Pipeline stages

| Stage | Status | Current limitation |
|---|---|---|
| 0 - PLAN | Verified | Settings resolver hierarchy is still simplified. |
| 1 - INGEST | Verified | DataForSEO only; partial-run semantics now preserved. |
| 2 - NORMALISE | Verified | DataForSEO only; full NLP normalisation is deferred. |
| 3 - ENRICH | Prototype | Difficulty bucket is counted but not stored in a dedicated field. |
| 4 - CLUSTER | Prototype | Jaccard/core-keyword heuristic, not SERP + embedding agglomerative clustering. |
| 5 - MATCH | Prototype | No `ExistingPage` data, so it cannot demonstrate real matches. |
| 6 - DECIDE | Prototype | All current outputs resolve to `new_content`; other decision branches are unvalidated. |
| 7 - CROSS_MARKET | Not started | Required for cross-market propagation. |
| 8 - SCORE | Prototype | Scores exist, but GSC/GA4 factors are unavailable. |
| 9 - EXPORT | Not started | Required client-facing Google Sheet is absent. |

### 2.4 Stabilisation completed on 2026-08-10

- Added database-backed KeywordObservation uniqueness.
- Added migration-time cleanup for historical duplicate observations.
- Made normalisation idempotent with `update_or_create`.
- Applied bulk keyword-difficulty responses to observations.
- Made malformed DataForSEO payloads fail loudly and record a stage error.
- Partitioned clustering by market.
- Reconciled stable topics and added `last_seen_run` tracking.
- Updated Match and Decide to consume topics seen in the current run.
- Added TopicKeyword uniqueness.
- Cleared stale run errors on restart/success.
- Defined complete, partial, and failed run behavior.
- Replaced Windows-incompatible pipeline console icons in stages 0-4 and final status output.
- Added five focused regression tests covering idempotency, uniqueness, payload validation, market isolation, and stable topic reconciliation.

---

## 3. Current database evidence

Snapshot at the time of this tracker update:

| Record | Count / state |
|---|---:|
| Runs | 2 complete |
| RawFetch | 8 total, two per pipeline endpoint across Runs 1 and 2 |
| KeywordObservation | 270 total; Run 2 contains 160 unique observations |
| ExistingPage | **0** |
| Topic | 90 |
| TopicKeyword | 230 |
| Opportunity | 90 |
| Opportunity actions | **90 `new_content`; 0 optimise; 0 merge; 0 ignore** |
| Run 1 recorded cost | $0.0792 |
| Run 2 recorded cost | $0.0000 due to cache reuse |

### Important data-quality flag

Run 2's saved stage counts are not a clean single-run baseline:

- Cluster records 71 output topics.
- Later Match, Decide, and Score records show 90 inputs/outputs.
- The current database also contains 90 topics and 90 opportunities.

This reflects development-time reruns while the clustering/topic reconciliation behavior was changing. It does **not** prove a clean Stage 0-8 execution. Before output work is accepted, create a fresh validation run after page ingestion and confirm that every stage's input/output counts reconcile.

---

## 4. Phase exit criteria

### Phase 1 - Foundations

- [x] Core Django models and migrations
- [x] Django admin configuration screens
- [x] DataForSEO connector and cache
- [x] Raw response cost/audit storage
- [x] DataForSEO normalisation into typed observations
- [x] Real DataForSEO run evidence
- [x] Idempotency and malformed-payload regression tests
- [x] Sitemap connector, recursive index parsing, gzip support, audit storage, and idempotent ExistingPage synchronization
- [ ] Populate the live validation database after correcting the configured Nike sitemap URL
- [ ] GSC connector with pagination and date-window handling
- [ ] Celery + Redis execution and scheduling
- [ ] Structured logging with run/client context
- [ ] Offline connector fixtures/cassettes
- [ ] PostgreSQL production configuration

**Phase 1 is not fully complete according to the implementation plan.**

### Phase 2 - Core engine

- [x] Basic keyword normalisation
- [x] Basic market-isolated clustering
- [x] Stable topic UID reconciliation
- [x] Match stage code
- [x] Decision stage code and decision traces
- [x] Basic scoring stage code
- [ ] Populate ExistingPage from sitemap/GSC
- [ ] Validate optimise, merge, ignore, and already-winning decision branches
- [ ] Implement ranking/content/slug match hierarchy from the handover
- [ ] Implement cannibalisation detection with real overlapping rankings
- [ ] Add SERP/embedding blended similarity and complete-linkage clustering
- [ ] Add cluster-quality golden dataset
- [ ] Implement Stage 7 cross-market propagation
- [ ] Complete clean Stage 0-8 validation run

**Phase 2 code exists, but Phase 2 exit criteria have not been met.**

### Phase 3 - Output

- [x] Opportunity row builder with the 10 requested columns
- [x] `export_run --run-id <id> --format csv` command
- [x] Run 2 CSV generated with 90 opportunity rows
- [ ] Google service-account authentication
- [ ] Google Sheets writer
- [ ] Stable-UID merge-on-write
- [ ] Preserve human-owned columns
- [ ] Archive removed topics instead of deleting them
- [ ] Opportunities, Ignored, Cannibalisation, Run log, and Reference tabs
- [ ] Formatting, validation, and conditional formatting
- [ ] End-to-end scratch-sheet test

**Phase 3 is in progress and the CSV milestone is complete. Further Sheets work is sequenced after sitemap ingestion and decision validation.**

---

## 5. NEXT OPERATIONAL STEP - Configure the correct Nike sitemap and populate ExistingPage

> **Sitemap implementation is complete.** The immediate step is selecting the appropriate public Nike sitemap index, updating `Market.sitemap_url`, and executing Stage 1b.

### Task 2.5.1 - Sitemap ingestion - IMPLEMENTED

**Goal:** Populate `ExistingPage` so Stage 5 can distinguish new topics from topics already covered by the client site.

Completed deliverables:

1. Create `apps/connectors/sitemap/` behind the existing connector interface.
2. Support sitemap indexes, nested sitemaps, gzip where practical, and duplicate URL removal.
3. Save the raw crawl/fetch evidence to `RawFetch` with `source="sitemap"`.
4. Normalise sitemap URLs into `ExistingPage` records per market.
5. Make sitemap ingestion idempotent.
6. Add offline fixtures and tests; do not make the test suite depend on a live site.
7. Register stage counts and actionable errors in `RunStage`.

Implementation evidence:

- `apps/connectors/sitemap/connector.py` supports URL sets, sitemap indexes, nested indexes, gzip, deduplication, last-modified dates, caching, size/count safety limits, and RawFetch audit rows.
- `apps/runs/stages/stage_1b_sitemap.py` synchronizes ExistingPage records without deleting stored analytics fields.
- `run_pipeline --stage sitemap` and the full pipeline include Stage 1b.
- Four offline sitemap tests pass; 11 focused project tests pass in total.
- Migration `runs.0004_alter_runstage_name` is applied.

Live validation result:

- Configured URL: `https://www.nike.com/sitemap.xml`
- Result: HTTP 404; failure correctly recorded on Run 2 and its sitemap RunStage.
- Nike's public `robots.txt` declares several specific sitemap indexes instead of `/sitemap.xml`.
- ExistingPage remains empty until the appropriate index is selected and configured.

Acceptance criteria:

- `ExistingPage` count is greater than zero for the validation client.
- Rerunning sitemap ingestion does not duplicate pages.
- URLs are assigned to the correct market.
- A malformed/unavailable sitemap records a clear failure.
- Stage 5 produces at least one realistic page match on a controlled fixture.

## 6. REQUIRED FOLLOW-UP - Fresh decision validation

### Task 2.5.2 - Rerun and validate Match -> Decide -> Score

After sitemap ingestion:

1. Create a fresh Run #3 rather than reusing development Run #2.
2. Execute stages 0-8 from a clean state.
3. Confirm stage input/output counts reconcile.
4. Inspect decision traces for every action branch produced.
5. Verify current Opportunities are replaced/associated correctly without orphaning stable topics.
6. Confirm the result includes credible action diversity where the fixture supports it.

Expected result: not necessarily every action type, but the engine must no longer produce `new_content` solely because its page inventory is empty.

### Why this remains mandatory before client acceptance

The CSV proves the export mechanism, but all 90 current rows are `new_content`. The handover defines Engine 1 as a decision system, not a data collection system. Page inventory and meaningful action validation remain mandatory before presenting the Sheet as a trustworthy client deliverable.

---

## 7. Confirmed build sequence

Recommended sequence:

1. **Correct `Market.sitemap_url` and run Stage 1b - ACTIVE NEXT TASK.** Code is complete; the current generic Nike URL returns 404.
2. **Fresh Stage 0-8 validation run** - validate Match, Decide, and Score with real page inventory.
3. **Decision-rule and golden-dataset tests** - protect the engine's core quality.
4. **Google Sheets Task 3.2** - reuse the verified builder and CSV contract after recommendations are meaningful.
5. **Sheets merge-on-write** - preserve human edits after decision output is validated.
6. **GSC connector - BLOCKED until post-contract client credentials.** Unlocks positions, quick wins, decay, ranking matches, and cannibalisation evidence.
7. **GA4 connector - BLOCKED until post-contract client credentials.** Unlocks conversion potential and commercial weighting.
8. **Stage 7 and language handling** - validate UK first, then DE/FR/NL.
9. **Celery/Redis/PostgreSQL/observability** - production hardening before deployment.

---

## 8. Client decisions and external blockers

| Input needed | Blocks | Status |
|---|---|---|
| Actual client domain and sitemap URL | Real page inventory | Test Nike data exists; production client not confirmed |
| Category taxonomy / URL pattern mapping | Category and page classification | Client input required |
| GSC auth model and credentials | Positions, quick wins, decay, ranking matching | **Blocked until the client signs and supplies private credentials** |
| GA4 auth and property | Conversion weighting and commercial impact | **Blocked until the client signs and supplies private credentials** |
| Google Sheets service account and destination | Phase 3 delivery | Blocked, but builder can be developed with fixtures |
| Approved competitors per market | Production gap research | Client confirmation required |
| Seed keywords per market | Production research quality | Client confirmation required |
| Estimated-impact method and value per conversion | Revenue/click impact | Client decision required |
| Priority score P0 decision | Final output contract | Client decision required |
| Market rollout order and cadence | Multi-market delivery | Client decision required |

---

## 9. Known technical risks

1. **No existing-page data:** current actions are structurally biased toward `new_content`.
2. **Development-run contamination:** Run 2 stage counts reflect repeated implementation-time runs; use a fresh Run 3 for acceptance.
3. **Clustering quality:** current Jaccard/core-keyword heuristic is not the final handover design.
4. **Stable identity during cluster evolution:** exact UID reuse works for unchanged clusters; merge/split history and fuzzy reconciliation remain future work.
5. **Limited test discovery:** targeted suites now run 11 tests, while bare `manage.py test` currently discovers zero tests due to project layout.
6. **Windows console compatibility:** stages 0-4 were made ASCII-safe, but newly added Stage 5/6/8 command headings still use generated Unicode glyphs and should be normalised.
7. **Synchronous SQLite execution:** suitable for local prototyping, not the target production architecture.
8. **No cost ceiling enforcement:** settings contain a spend limit, but ingestion does not yet halt before exceeding it.

---

## 10. Definition of the next meaningful quality milestone

The next milestone is complete when:

- Existing pages are populated idempotently from a sitemap fixture or approved test sitemap.
- A fresh run executes stages 0-8 without stale or contradictory stage counts.
- Topic counts entering Match equal the current run's clustered topic count.
- Opportunities have evidence-backed decision traces.
- At least one controlled test proves an existing topic can resolve to an action other than `new_content`.
- All focused tests and Django checks pass.

Google Sheets integration may proceed in parallel, but this quality milestone is required before client acceptance.

---

*Last updated: 2026-08-10 - after pipeline stabilization review and reconciliation with the handover and implementation plan.*

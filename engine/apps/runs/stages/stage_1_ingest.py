"""
Stage 1 — INGEST

What it does:
  - Reads the plan from Run.settings_snapshot (set by Stage 0)
  - For each market, calls DataForSEO endpoints through the connector
  - Each API call is automatically saved to RawFetch (the connector does this)
  - Tracks the total cost spent and updates Run.total_cost_usd
  - Creates a RunStage record when complete

Key design rules enforced here:
  - We SKIP Advanced SERP in this stage (too expensive — only for shortlisted keywords in Stage 3)
  - We respect the cache — if identical calls were made in a recent run, RawFetch is reused
  - If any single market fails, we continue with the others and mark the stage "partial"
"""
import logging
from django.utils import timezone

from apps.clients.models import Market
from apps.connectors.dataforseo.connector import DataForSEOConnector
from apps.runs.models import Run, RunStage
from django.conf import settings

logger = logging.getLogger(__name__)


def run_stage_ingest(run: Run) -> dict:
    """
    Execute Stage 1 — INGEST.
    Returns a summary dict with counts of RawFetch rows created per market.
    """
    logger.info(f"[Stage 1 — INGEST] Starting for Run #{run.pk}")

    snapshot = run.settings_snapshot
    if not snapshot:
        raise RuntimeError(
            f"Run #{run.pk} has no settings_snapshot. Did Stage 0 (PLAN) run first?"
        )

    seed_keywords = snapshot.get("seed_keywords", [])
    markets_config = snapshot.get("markets", [])

    total_raw_fetches = 0
    market_results = {}
    any_failure = False

    for market_cfg in markets_config:
        market_code = market_cfg["market_code"]
        market_id = market_cfg["market_id"]
        competitors = market_cfg.get("competitors", [])

        try:
            market = Market.objects.get(pk=market_id)
        except Market.DoesNotExist:
            logger.error(f"[Stage 1 — INGEST] Market #{market_id} not found, skipping.")
            any_failure = True
            continue

        logger.info(f"[Stage 1 — INGEST] Processing market: {market_code}")

        # Initialise the connector (reads credentials from Django settings)
        connector = DataForSEOConnector(
            run=run,
            market=market,
            login=settings.DATAFORSEO_LOGIN,
            password=settings.DATAFORSEO_PASSWORD,
        )

        fetches_this_market = 0

        try:
            # ── CHECK 1: Keyword Ideas ──────────────────────────────────────
            # Discover keywords related to our seed terms
            logger.info(f"  [{market_code}] Calling keyword_ideas with {len(seed_keywords)} seed keywords...")
            keyword_ideas = connector.get_keyword_ideas(keywords=seed_keywords, limit=100)
            fetches_this_market += 1
            logger.info(f"  [{market_code}] ✅ keyword_ideas → {len(keyword_ideas)} keywords returned")

            # ── CHECK 2 & 3: Competitor Gaps + Top Pages ────────────────────
            for competitor_domain in competitors:
                # Gap analysis: keywords the competitor ranks for that we don't
                logger.info(f"  [{market_code}] Calling domain_intersection for {competitor_domain}...")
                gap_keywords = connector.get_domain_intersection(
                    target1=competitor_domain,
                    target2=market.client.primary_domain,
                    limit=50,
                )
                fetches_this_market += 1
                logger.info(f"  [{market_code}] ✅ domain_intersection ({competitor_domain}) → {len(gap_keywords)} gap keywords")

                # Top pages: which pages drive the most traffic for this competitor
                logger.info(f"  [{market_code}] Calling relevant_pages for {competitor_domain}...")
                top_pages = connector.get_relevant_pages(target_domain=competitor_domain, limit=10)
                fetches_this_market += 1
                logger.info(f"  [{market_code}] ✅ relevant_pages ({competitor_domain}) → {len(top_pages)} pages")

            # ── Bulk Difficulty for the keywords we discovered ──────────────
            # Collect all unique keywords from ideas + gap analysis for batch difficulty lookup
            all_discovered_keywords = [item.keyword for item in keyword_ideas]
            # Limit to 100 keywords for the difficulty call (avoid excessive cost on first test run)
            difficulty_keywords = all_discovered_keywords[:100]

            if difficulty_keywords:
                logger.info(f"  [{market_code}] Calling bulk_keyword_difficulty for {len(difficulty_keywords)} keywords...")
                difficulty_items = connector.get_bulk_keyword_difficulty(keywords=difficulty_keywords)
                fetches_this_market += 1
                logger.info(f"  [{market_code}] ✅ bulk_keyword_difficulty → {len(difficulty_items)} results")

            # NOTE: Advanced SERP is intentionally skipped here.
            # It is the most expensive endpoint (~$0.01 per keyword).
            # We only call it in Stage 3 (ENRICH) for a shortlisted set of ~5-15% of keywords.

            market_results[market_code] = {
                "status": "complete",
                "raw_fetches_created": fetches_this_market,
            }
            total_raw_fetches += fetches_this_market

        except Exception as e:
            logger.error(f"[Stage 1 — INGEST] ❌ Market {market_code} failed: {e}")
            market_results[market_code] = {
                "status": "failed",
                "error": str(e),
                "raw_fetches_created": fetches_this_market,
            }
            any_failure = True
            total_raw_fetches += fetches_this_market  # count what we did get

    # --- Tally cost from all RawFetch rows created in this run ---
    from django.db.models import Sum
    from apps.ingestion.models import RawFetch
    cost_total = RawFetch.objects.filter(run=run).aggregate(
        total=Sum("cost_usd")
    )["total"] or 0

    run.total_cost_usd = cost_total
    run.save(update_fields=["total_cost_usd"])

    # --- Record stage completion ---
    stage_status = "partial" if any_failure else "complete"
    if any_failure and not market_results:
        raise RuntimeError("No configured markets could be loaded for ingestion.")
    completed_markets = [
        code for code, result in market_results.items()
        if result["status"] == "complete"
    ]
    if any_failure and not completed_markets:
        raise RuntimeError("DataForSEO ingestion failed for every configured market.")
    stage_errors = "; ".join(
        f"{code}: {result['error']}"
        for code, result in market_results.items()
        if result["status"] == "failed"
    )
    RunStage.objects.update_or_create(
        run=run,
        name="ingest",
        defaults={
            "status": stage_status,
            "records_in": len(seed_keywords),
            "records_out": total_raw_fetches,
            "started_at": timezone.now(),
            "finished_at": timezone.now(),
            "error": stage_errors,
        },
    )

    summary = {
        "total_raw_fetches": total_raw_fetches,
        "total_cost_usd": float(cost_total),
        "markets": market_results,
        "stage_status": stage_status,
    }

    logger.info(f"[Stage 1 — INGEST] {stage_status.upper()}. Summary: {summary}")
    return summary

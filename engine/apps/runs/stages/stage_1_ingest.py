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
from urllib.parse import urlsplit
from django.db.models import Max, Sum
from django.utils import timezone

from apps.clients.models import Competitor, Market
from apps.connectors.dataforseo.connector import DataForSEOConnector
from apps.ingestion.models import RawFetch
from apps.runs.models import Run, RunStage
from apps.runs.stages.settings_snapshot import engine_settings_for_market
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_SPEND_PER_RUN_USD = 50.0

# These domains rank across almost every industry but are not useful direct
# competitors for content-gap analysis. Marketplaces use prefix matching below
# so country variants such as amazon.co.uk are covered dynamically.
GENERIC_DISCOVERY_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "pinterest.com",
    "reddit.com", "tiktok.com", "twitter.com", "wikipedia.org", "x.com",
    "youtube.com", "etsy.com", "aliexpress.com", "temu.com",
    "freeads.co.uk",
}
GENERIC_DISCOVERY_PREFIXES = ("amazon.", "ebay.")


class BudgetGuardrailExceeded(RuntimeError):
    """Raised after a paid API receipt reaches the run's spending limit."""


def _max_spend_from_snapshot(snapshot):
    """Resolve a safe run-wide budget from flat or per-market snapshots.

    The guardrail is per Run, so when markets have different limits we enforce
    the strictest one across all selected markets.
    """
    budgets = []
    market_codes = [
        market.get("market_code")
        for market in (snapshot.get("markets") or [])
        if market.get("market_code")
    ]
    if not market_codes:
        market_codes = [None]

    for market_code in market_codes:
        configured = engine_settings_for_market(
            snapshot, market_code
        ).get("max_spend_per_run_usd")
        if configured is None:
            budgets.append(DEFAULT_MAX_SPEND_PER_RUN_USD)
            continue
        try:
            budgets.append(float(configured))
        except (TypeError, ValueError):
            logger.warning(
                "Invalid max_spend_per_run_usd value %r for market %s; "
                "using safe $%.2f fallback.",
                configured,
                market_code or "legacy",
                DEFAULT_MAX_SPEND_PER_RUN_USD,
            )
            budgets.append(DEFAULT_MAX_SPEND_PER_RUN_USD)

    return min(budgets) if budgets else DEFAULT_MAX_SPEND_PER_RUN_USD


def _latest_dataforseo_fetch_pk(run):
    return (
        RawFetch.objects.filter(run=run, source="dataforseo")
        .aggregate(latest=Max("pk"))["latest"]
        or 0
    )


def _add_new_fetch_cost(run, after_pk, total_spent, maximum_spend):
    """Add only receipts created by the immediately preceding API call."""
    new_cost = RawFetch.objects.filter(
        run=run,
        source="dataforseo",
        pk__gt=after_pk,
    ).aggregate(total=Sum("cost_usd"))["total"] or 0
    total_spent += float(new_cost)

    if total_spent >= maximum_spend:
        persisted_total = RawFetch.objects.filter(run=run).aggregate(
            total=Sum("cost_usd")
        )["total"] or 0
        message = (
            f"Budget guardrail hit for Run #{run.pk}: spent "
            f"${total_spent:.4f} against the ${maximum_spend:.2f} limit. "
            "No further DataForSEO API calls were made."
        )
        logger.critical(message)
        run.total_cost_usd = persisted_total
        run.error = message
        run.save(update_fields=["total_cost_usd", "error"])
        raise BudgetGuardrailExceeded(message)

    return total_spent


def _deduplicate_discovered_keywords(*result_sets):
    """Return non-blank keywords once, case-insensitively, in first-seen order."""
    keywords = []
    seen = set()
    for result_set in result_sets:
        for item in result_set:
            keyword = (item.keyword or "").strip()
            identity = " ".join(keyword.lower().split())
            if not identity or identity in seen:
                continue
            seen.add(identity)
            keywords.append(keyword)
    return keywords


def _normalise_domain(value):
    candidate = (value or "").lower().strip()
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    return (parsed.hostname or "").removeprefix("www.").rstrip(".")


def _is_generic_discovery_domain(domain):
    return (
        domain in GENERIC_DISCOVERY_DOMAINS
        or domain.startswith(GENERIC_DISCOVERY_PREFIXES)
    )


def _is_overly_broad_domain(item):
    """Reject domains whose total footprint dwarfs the relevant overlap."""
    intersections = getattr(item, "intersections", None) or 0
    metrics = getattr(item, "full_domain_metrics", None)
    organic = getattr(metrics, "organic", None) if metrics else None
    organic_count = organic.get("count") if isinstance(organic, dict) else None
    return bool(
        intersections > 0
        and organic_count is not None
        and organic_count / intersections > 1000
    )


def _select_auto_discovered_competitors(items, client_domain, limit=3):
    """Select credible direct domains, excluding broad web platforms."""
    own_domain = _normalise_domain(client_domain)
    ranked = sorted(
        enumerate(items),
        key=lambda pair: (
            -(pair[1].intersections if pair[1].intersections is not None else -1),
            pair[0],
        ),
    )
    selected = []
    seen = set()
    for _, item in ranked:
        domain = _normalise_domain(item.domain)
        if (
            not domain
            or domain == own_domain
            or domain in seen
            or _is_generic_discovery_domain(domain)
            or _is_overly_broad_domain(item)
        ):
            continue
        seen.add(domain)
        selected.append(domain)
        if len(selected) == limit:
            break
    return selected


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
    max_spend_per_run_usd = _max_spend_from_snapshot(snapshot)

    total_raw_fetches = 0
    total_spent_so_far = 0.0
    market_results = {}
    any_failure = False
    budget_error = None

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

        # Runs created by the earlier auto-discovery implementation persisted
        # generated domains as if an admin had configured them. Re-discover
        # those once with the corrected filtering, then keep auto selections
        # only in the Run snapshot so manual configuration stays distinct.
        if (
            market_cfg.get("competitors_auto_discovered")
            and market_cfg.get("competitor_discovery_version") != 2
        ):
            Competitor.objects.filter(
                market=market,
                domain__in=competitors,
                is_primary=False,
            ).delete()
            competitors = []

        # Initialise the connector (reads credentials from Django settings)
        connector = DataForSEOConnector(
            run=run,
            market=market,
            login=settings.DATAFORSEO_LOGIN,
            password=settings.DATAFORSEO_PASSWORD,
        )

        fetches_this_market = 0
        competitors_auto_discovered = False

        def guarded_api_call(operation):
            """Execute one API call and account for its newly saved receipt."""
            nonlocal fetches_this_market, total_spent_so_far
            before_pk = _latest_dataforseo_fetch_pk(run)
            try:
                result = operation()
            finally:
                # The connector saves its RawFetch before parsing the response,
                # so account for the charge even when response validation fails.
                fetches_this_market += 1
                total_spent_so_far = _add_new_fetch_cost(
                    run,
                    after_pk=before_pk,
                    total_spent=total_spent_so_far,
                    maximum_spend=max_spend_per_run_usd,
                )
            return result

        try:
            # ── CHECK 1: Keyword Ideas ──────────────────────────────────────
            # Use all three Labs discovery strategies from the handover.
            logger.info(f"  [{market_code}] Calling keyword_ideas with {len(seed_keywords)} seed keywords...")
            keyword_ideas = guarded_api_call(
                lambda: connector.get_keyword_ideas(
                    keywords=seed_keywords, limit=100
                )
            )
            logger.info(f"  [{market_code}] ✅ keyword_ideas → {len(keyword_ideas)} keywords returned")

            logger.info(
                f"  [{market_code}] Calling keyword_suggestions with "
                f"{len(seed_keywords)} seed keywords..."
            )
            keyword_suggestions = []
            for seed_keyword in seed_keywords:
                keyword_suggestions.extend(guarded_api_call(
                    lambda seed_keyword=seed_keyword: connector.get_keyword_suggestions(
                        keyword=seed_keyword, limit=100
                    )
                ))
            logger.info(
                f"  [{market_code}] ✅ keyword_suggestions → "
                f"{len(keyword_suggestions)} keywords returned"
            )

            logger.info(
                f"  [{market_code}] Calling related_keywords with "
                f"{len(seed_keywords)} seed keywords..."
            )
            related_keywords = []
            for seed_keyword in seed_keywords:
                related_keywords.extend(guarded_api_call(
                    lambda seed_keyword=seed_keyword: connector.get_related_keywords(
                        keyword=seed_keyword, limit=100
                    )
                ))
            logger.info(
                f"  [{market_code}] ✅ related_keywords → "
                f"{len(related_keywords)} keywords returned"
            )

            # If neither the Run nor Django admin supplied competitors, use
            # DataForSEO's relevance-ranked discovery and retain the top three.
            if not competitors:
                logger.info(
                    f"  [{market_code}] No configured competitors; "
                    f"calling competitors_domain for {market.client.primary_domain}..."
                )
                discovered = guarded_api_call(
                    lambda: connector.get_competitor_domains(
                        target_domain=market.client.primary_domain,
                        limit=50,
                    )
                )
                competitors = _select_auto_discovered_competitors(
                    discovered,
                    market.client.primary_domain,
                    limit=3,
                )
                market_cfg["competitors"] = competitors
                market_cfg["competitors_auto_discovered"] = True
                market_cfg["competitor_discovery_version"] = 2
                run.settings_snapshot = snapshot
                run.save(update_fields=["settings_snapshot"])
                competitors_auto_discovered = True
                logger.info(
                    f"  [{market_code}] Auto-discovered {len(competitors)} "
                    f"competitors: {competitors}"
                )

            # ── CHECK 2 & 3: Competitor Gaps + Top Pages ────────────────────
            for competitor_domain in competitors:
                # Gap analysis: keywords the competitor ranks for that we don't
                logger.info(f"  [{market_code}] Calling domain_intersection for {competitor_domain}...")
                gap_keywords = guarded_api_call(
                    lambda: connector.get_domain_intersection(
                        target1=competitor_domain,
                        target2=market.client.primary_domain,
                        limit=50,
                    )
                )
                logger.info(f"  [{market_code}] ✅ domain_intersection ({competitor_domain}) → {len(gap_keywords)} gap keywords")

                # Top pages: which pages drive the most traffic for this competitor
                logger.info(f"  [{market_code}] Calling relevant_pages for {competitor_domain}...")
                top_pages = guarded_api_call(
                    lambda: connector.get_relevant_pages(
                        target_domain=competitor_domain, limit=10
                    )
                )
                logger.info(f"  [{market_code}] ✅ relevant_pages ({competitor_domain}) → {len(top_pages)} pages")

            # ── Bulk Difficulty for the keywords we discovered ──────────────
            # Send the unique union from all three discovery endpoints to the
            # provider's bulk difficulty endpoint (supports up to 1,000).
            difficulty_keywords = _deduplicate_discovered_keywords(
                keyword_ideas,
                keyword_suggestions,
                related_keywords,
            )

            logger.info(
                f"  [{market_code}] Discovery union → "
                f"{len(difficulty_keywords)} unique keywords"
            )

            if difficulty_keywords:
                logger.info(f"  [{market_code}] Calling bulk_keyword_difficulty for {len(difficulty_keywords)} keywords...")
                difficulty_items = guarded_api_call(
                    lambda: connector.get_bulk_keyword_difficulty(
                        keywords=difficulty_keywords
                    )
                )
                logger.info(f"  [{market_code}] ✅ bulk_keyword_difficulty → {len(difficulty_items)} results")

            # NOTE: Advanced SERP is intentionally skipped here.
            # It is the most expensive endpoint (~$0.01 per keyword).
            # We only call it in Stage 3 (ENRICH) for a shortlisted set of ~5-15% of keywords.

            market_results[market_code] = {
                "status": "complete",
                "raw_fetches_created": fetches_this_market,
                "competitors": competitors,
                "competitors_auto_discovered": competitors_auto_discovered,
            }
            total_raw_fetches += fetches_this_market

        except BudgetGuardrailExceeded as e:
            logger.error(f"[Stage 1 — INGEST] Market {market_code} stopped: {e}")
            market_results[market_code] = {
                "status": "failed",
                "error": str(e),
                "raw_fetches_created": fetches_this_market,
            }
            any_failure = True
            budget_error = str(e)
            total_raw_fetches += fetches_this_market
            break
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
    cost_total = RawFetch.objects.filter(run=run).aggregate(
        total=Sum("cost_usd")
    )["total"] or 0

    run.total_cost_usd = cost_total
    run.save(update_fields=["total_cost_usd"])

    # --- Record stage completion/failure before propagating fatal errors ---
    completed_markets = [
        code for code, result in market_results.items()
        if result["status"] == "complete"
    ]
    if any_failure and not completed_markets:
        stage_status = "failed"
    elif any_failure:
        stage_status = "partial"
    else:
        stage_status = "complete"

    stage_errors = "; ".join(
        f"{code}: {result['error']}"
        for code, result in market_results.items()
        if result["status"] == "failed"
    )
    if any_failure and not market_results:
        stage_errors = "No configured markets could be loaded for ingestion."

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

    if any_failure and not market_results:
        raise RuntimeError("No configured markets could be loaded for ingestion.")
    if budget_error:
        raise BudgetGuardrailExceeded(budget_error)
    if stage_status == "failed":
        raise RuntimeError("DataForSEO ingestion failed for every configured market.")

    summary = {
        "total_raw_fetches": total_raw_fetches,
        "total_cost_usd": float(cost_total),
        "markets": market_results,
        "stage_status": stage_status,
    }

    logger.info(f"[Stage 1 — INGEST] {stage_status.upper()}. Summary: {summary}")
    return summary

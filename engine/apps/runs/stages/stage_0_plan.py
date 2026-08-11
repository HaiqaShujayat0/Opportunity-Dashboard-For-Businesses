"""
Stage 0 — PLAN

What it does:
  - Reads the Run record from the database
  - Resolves which markets and competitors to process
  - Validates that seed keywords exist
  - Snapshots the current EngineSettings + ScoringWeights into Run.settings_snapshot
    (so we can always reproduce the exact config that produced any given output)
  - Creates a RunStage record to record that planning succeeded

This stage never calls any external API. It just organises the config.
"""
import logging
from django.utils import timezone

from apps.clients.models import EngineSettings, ScoringWeights
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)


def run_stage_plan(run: Run) -> dict:
    """
    Execute Stage 0 — PLAN.
    Returns a summary dict describing what was resolved.
    Raises ValueError if the run is misconfigured (no seed keywords, no markets, etc.)
    """
    logger.info(f"[Stage 0 — PLAN] Starting for Run #{run.pk} / Client: {run.client.name}")

    # --- 1. Validate seed keywords exist ---
    seed_keywords = run.get_seed_keywords_list()
    if not seed_keywords:
        raise ValueError(
            f"Run #{run.pk} has no seed keywords. "
            "Please add comma-separated keywords in the 'Seed Keywords' field."
        )

    # --- 2. Resolve markets ---
    # Run.markets is a JSON list of market codes e.g. ["UK", "DE"]
    # If the list is empty, default to ALL active markets for this client
    if run.markets:
        active_markets = list(
            run.client.markets.filter(code__in=run.markets, is_active=True)
        )
    else:
        active_markets = list(run.client.markets.filter(is_active=True))

    if not active_markets:
        raise ValueError(
            f"Run #{run.pk} has no active markets configured. "
            "Please create at least one Market for this client in the admin."
        )

    logger.info(f"[Stage 0 — PLAN] Resolved {len(active_markets)} market(s): "
                f"{[m.code for m in active_markets]}")

    # --- 3. Resolve competitors per market ---
    market_data = []
    for market in active_markets:
        competitors = list(market.competitors.values_list("domain", flat=True))
        # If the run has explicit competitor_domains, use those instead
        run_competitors = run.get_competitor_domains_list()
        if run_competitors:
            competitors = run_competitors
        market_data.append({
            "market_id": market.pk,
            "market_code": market.code,
            "location_code": market.dataforseo_location_code,
            "language_code": market.language_code,
            "competitors": competitors,
        })

    # --- 4. Snapshot EngineSettings ---
    try:
        engine_settings = EngineSettings.objects.filter(
            client=run.client, market=None
        ).first()
        settings_dict = {}
        if engine_settings:
            settings_dict = {
                "min_search_volume": engine_settings.min_search_volume,
                "max_keyword_difficulty": engine_settings.max_keyword_difficulty,
                "max_spend_per_run_usd": float(engine_settings.max_spend_per_run_usd),
                "max_serp_calls_per_run": engine_settings.max_serp_calls_per_run,
                "quick_win_min_position": engine_settings.quick_win_min_position,
                "quick_win_max_position": engine_settings.quick_win_max_position,
                "decay_baseline_max_position": engine_settings.decay_baseline_max_position,
                "decay_current_min_position": engine_settings.decay_current_min_position,
                "decay_min_drop": engine_settings.decay_min_drop,
                "decay_baseline_days": engine_settings.decay_baseline_days,
                "decay_comparison_days": engine_settings.decay_comparison_days,
                "serp_overlap_threshold": engine_settings.serp_overlap_threshold,
                "semantic_similarity_threshold": engine_settings.semantic_similarity_threshold,
            }
    except Exception:
        settings_dict = {}

    # --- 5. Snapshot ScoringWeights ---
    try:
        weights = ScoringWeights.objects.get(client=run.client)
        weights_dict = {
            "w_volume": weights.w_volume,
            "w_position_opportunity": weights.w_position_opportunity,
            "w_conversion": weights.w_conversion,
            "w_difficulty": weights.w_difficulty,
            "w_signal": weights.w_signal,
            "w_market": weights.w_market,
        }
    except ScoringWeights.DoesNotExist:
        weights_dict = {}

    # --- 6. Save snapshot into the Run record ---
    settings_snapshot = {
        "seed_keywords": seed_keywords,
        "markets": market_data,
        "engine_settings": settings_dict,
        "scoring_weights": weights_dict,
        "snapshot_taken_at": timezone.now().isoformat(),
    }

    run.settings_snapshot = settings_snapshot
    run.save(update_fields=["settings_snapshot"])

    # --- 7. Record this stage as complete ---
    RunStage.objects.update_or_create(
        run=run,
        name="plan",
        defaults={
            "status": "complete",
            "records_in": 0,
            "records_out": len(active_markets),
            "started_at": timezone.now(),
            "finished_at": timezone.now(),
        },
    )

    summary = {
        "seed_keywords": seed_keywords,
        "markets": [m["market_code"] for m in market_data],
        "has_engine_settings": bool(settings_dict),
        "has_scoring_weights": bool(weights_dict),
    }

    logger.info(f"[Stage 0 — PLAN] ✅ Complete. Summary: {summary}")
    return summary

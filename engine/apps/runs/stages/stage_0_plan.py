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
from decimal import Decimal

from django.utils import timezone

from apps.clients.models import EngineSettings, ScoringWeights
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)


def _engine_settings_to_dict(instance):
    """Serialize every configurable field into JSON-safe snapshot values."""
    if instance is None:
        return {}
    values = {}
    for field in instance._meta.concrete_fields:
        if field.name in {"id", "client", "market"}:
            continue
        value = getattr(instance, field.name)
        values[field.name] = float(value) if isinstance(value, Decimal) else value
    return values


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

    # --- 4. Snapshot default + per-market EngineSettings ---
    default_record = EngineSettings.objects.filter(
        client=run.client, market=None
    ).first()
    default_settings = _engine_settings_to_dict(default_record)
    market_settings = {}
    has_engine_settings = bool(default_record)
    for market in active_markets:
        override = EngineSettings.objects.filter(
            client=run.client, market=market
        ).first()
        has_engine_settings = has_engine_settings or bool(override)
        merged = dict(default_settings)
        if override:
            override_settings = _engine_settings_to_dict(override)
            merged.update({
                key: value
                for key, value in override_settings.items()
                if value is not None and value != ""
            })
        market_settings[market.code] = merged

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
        "engine_settings": market_settings,
        "scoring_weights": weights_dict,
        "google_sheets_spreadsheet_id": (
            run.client.google_sheets_spreadsheet_id or ""
        ),
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
        "has_engine_settings": has_engine_settings,
        "has_scoring_weights": bool(weights_dict),
    }

    logger.info(f"[Stage 0 — PLAN] ✅ Complete. Summary: {summary}")
    return summary

"""Stage 1c: fetch configured GSC and GA4 sources into immutable RawFetch rows."""

import logging

from django.conf import settings
from django.utils import timezone

from apps.clients.models import Market
from apps.connectors.ga4 import GA4Connector
from apps.connectors.google_auth import ga4_live_executor, gsc_live_executor
from apps.connectors.gsc import GSCConnector
from apps.ingestion.models import RawFetch
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)


def run_stage_google_ingest(
    run: Run,
    gsc_connector_class=GSCConnector,
    ga4_connector_class=GA4Connector,
) -> dict:
    stage, _ = RunStage.objects.update_or_create(
        run=run,
        name="google_ingest",
        defaults={"status": "running", "started_at": timezone.now(), "finished_at": None, "error": ""},
    )
    market_configs = (run.settings_snapshot or {}).get("markets", [])
    if not market_configs:
        stage.status = "failed"
        stage.error = f"Run #{run.pk} has no planned markets. Run PLAN first."
        stage.finished_at = timezone.now()
        stage.save(update_fields=["status", "error", "finished_at"])
        raise RuntimeError(stage.error)

    results = {}
    failures = []
    configured_sources = successful_sources = 0
    raw_before = RawFetch.objects.filter(run=run, source__in=["gsc", "ga4"]).count()

    for config in market_configs:
        market = Market.objects.get(pk=config["market_id"])
        market_result = {"gsc": "skipped", "ga4": "skipped"}
        for source, configured, connector_class in (
            ("gsc", bool(market.gsc_property), gsc_connector_class),
            ("ga4", bool(market.ga4_property_id), ga4_connector_class),
        ):
            if not configured:
                continue
            configured_sources += 1
            try:
                connector_kwargs = {}
                if not getattr(settings, "GOOGLE_USE_DUMMY_DATA", True):
                    connector_kwargs = {
                        "executor": (
                            gsc_live_executor if source == "gsc" else ga4_live_executor
                        ),
                        "use_dummy": False,
                    }
                records = connector_class(
                    run=run, market=market, **connector_kwargs
                ).fetch()
                successful_sources += 1
                market_result[source] = {"status": "complete", "records": len(records)}
            except Exception as exc:
                logger.exception("%s ingestion failed for %s", source.upper(), market.code)
                failures.append(f"{market.code}/{source}: {exc}")
                market_result[source] = {"status": "failed", "error": str(exc)}
        results[market.code] = market_result

    raw_after = RawFetch.objects.filter(run=run, source__in=["gsc", "ga4"]).count()
    raw_created = raw_after - raw_before
    if configured_sources == 0:
        stage.status = "skipped"
    elif successful_sources == 0:
        stage.status = "failed"
    elif failures:
        stage.status = "partial"
    else:
        stage.status = "complete"
    stage.records_in = configured_sources
    stage.records_out = raw_created
    stage.error = "; ".join(failures)
    stage.finished_at = timezone.now()
    stage.save(update_fields=[
        "status", "records_in", "records_out", "error", "finished_at"
    ])

    if stage.status == "failed":
        raise RuntimeError(f"Google ingestion failed for every configured source: {stage.error}")
    return {
        "stage_status": stage.status,
        "configured_sources": configured_sources,
        "successful_sources": successful_sources,
        "raw_fetches_created": raw_created,
        "markets": results,
    }

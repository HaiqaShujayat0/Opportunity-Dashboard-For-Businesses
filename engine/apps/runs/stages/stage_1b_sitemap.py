"""Stage 1b: ingest public sitemaps into the ExistingPage inventory."""

import logging
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

from apps.clients.models import Market
from apps.connectors.sitemap import SitemapConnector
from apps.pages.models import ExistingPage
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)


def _belongs_to_market(url, market):
    return not market.url_pattern or urlparse(url).path.startswith(market.url_pattern)


@transaction.atomic
def _sync_market_pages(market, records):
    urls_seen = set()
    created = updated = 0
    for record in records:
        url = record["url"]
        if not _belongs_to_market(url, market):
            continue
        urls_seen.add(url)
        _, was_created = ExistingPage.objects.update_or_create(
            market=market, url=url,
            defaults={"path": urlparse(url).path or "/", "last_modified": record.get("last_modified"), "in_sitemap": True},
        )
        created += int(was_created)
        updated += int(not was_created)
    stale = ExistingPage.objects.filter(market=market, in_sitemap=True)
    if urls_seen:
        stale = stale.exclude(url__in=urls_seen)
    stale_count = stale.update(in_sitemap=False)
    return created, updated, stale_count, len(urls_seen)


def run_stage_sitemap(run: Run, connector_class=SitemapConnector):
    stage, _ = RunStage.objects.update_or_create(
        run=run, name="sitemap",
        defaults={"status": "running", "started_at": timezone.now(), "finished_at": None, "error": ""},
    )
    market_configs = (run.settings_snapshot or {}).get("markets", [])
    if not market_configs:
        raise RuntimeError(f"Run #{run.pk} has no planned markets. Run PLAN first.")
    results, failures = {}, []
    total_created = total_updated = total_stale = total_pages = 0
    for config in market_configs:
        market_code = config["market_code"]
        try:
            market = Market.objects.get(pk=config["market_id"])
            records = connector_class(run=run, market=market).fetch()
            created, updated, stale, page_count = _sync_market_pages(market, records)
            total_created += created
            total_updated += updated
            total_stale += stale
            total_pages += page_count
            results[market_code] = {"status": "complete", "pages": page_count, "created": created, "updated": updated, "marked_not_in_sitemap": stale}
        except Exception as exc:
            logger.exception("Sitemap ingestion failed for %s", market_code)
            failures.append(f"{market_code}: {exc}")
            results[market_code] = {"status": "failed", "error": str(exc)}
    if not any(result["status"] == "complete" for result in results.values()):
        stage.status, stage.error, stage.finished_at = "failed", "; ".join(failures), timezone.now()
        stage.save(update_fields=["status", "error", "finished_at"])
        raise RuntimeError(f"Sitemap ingestion failed for every market: {stage.error}")
    stage.status = "partial" if failures else "complete"
    stage.records_in, stage.records_out = len(market_configs), total_pages
    stage.error, stage.finished_at = "; ".join(failures), timezone.now()
    stage.save(update_fields=["status", "records_in", "records_out", "error", "finished_at"])
    return {"stage_status": stage.status, "pages_total": total_pages, "pages_created": total_created, "pages_updated": total_updated, "pages_marked_not_in_sitemap": total_stale, "markets": results}

"""Stage 2b: normalise saved GSC and GA4 responses into durable engine data."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError

from apps.connectors.ga4.schemas import GA4RunReportResponse
from apps.connectors.gsc.schemas import GSCSearchAnalyticsResponse
from apps.ingestion.models import KeywordObservation, RawFetch
from apps.pages.models import ExistingPage, PositionSnapshot
from apps.runs.models import Run, RunStage
from apps.runs.stages.settings_snapshot import engine_settings_for_market


class AnalyticsPayloadError(ValueError):
    pass


def _normalise_keyword(keyword: str) -> str:
    return " ".join(keyword.lower().strip().split())


def _quick_win_range(run: Run, market_code: str) -> tuple[float, float]:
    settings = engine_settings_for_market(run, market_code)
    return (
        float(settings.get("quick_win_min_position", 7.0)),
        float(settings.get("quick_win_max_position", 20.0)),
    )


def _decay_settings(run: Run, market_code: str) -> dict:
    settings = engine_settings_for_market(run, market_code)
    return {
        "baseline_max": float(settings.get("decay_baseline_max_position", 5.0)),
        "current_min": float(settings.get("decay_current_min_position", 5.0)),
        "min_drop": float(settings.get("decay_min_drop", 3.0)),
        "baseline_days": int(settings.get("decay_baseline_days", 90)),
    }


def _baseline_position(market, keyword, page_url, current_start, baseline_days):
    rows = PositionSnapshot.objects.filter(
        market=market,
        keyword_normalised=keyword,
        page_url=page_url,
        observed_on__gte=current_start - timedelta(days=baseline_days),
        observed_on__lt=current_start,
    )
    weighted = weight_total = 0.0
    for row in rows.iterator():
        weight = row.impressions if row.impressions > 0 else 1.0
        weighted += row.position * weight
        weight_total += weight
    return weighted / weight_total if weight_total else None


def _canonical_path(value: str) -> str | None:
    if not value or value == "(not set)":
        return None
    path = urlsplit(value).path or "/"
    return path if path.startswith("/") else f"/{path}"


def _parse_fetch(raw, schema):
    try:
        return schema.model_validate(raw.payload)
    except ValidationError as exc:
        raise AnalyticsPayloadError(
            f"Malformed {raw.source} payload in RawFetch #{raw.pk}: {exc}"
        ) from exc


def _normalise_gsc(run: Run, fetches: list[RawFetch]) -> dict:
    snapshots = []
    observations = defaultdict(lambda: {
        "keyword": "", "clicks": 0.0, "impressions": 0.0,
        "weighted_position": 0.0, "position_weight": 0.0, "dates": set(),
    })
    pages = defaultdict(lambda: {"clicks": 0.0, "impressions": 0.0, "keywords": set()})

    for raw in fetches:
        response = _parse_fetch(raw, GSCSearchAnalyticsResponse)
        for row in response.rows:
            normalised = _normalise_keyword(row.query)
            if not normalised or not row.page:
                continue
            snapshots.append((raw.market, row, normalised))
            key = (raw.market_id, normalised, row.page)
            item = observations[key]
            weight = row.impressions if row.impressions > 0 else 1.0
            item["market"] = raw.market
            item["keyword"] = row.query
            item["clicks"] += row.clicks
            item["impressions"] += row.impressions
            item["weighted_position"] += row.position * weight
            item["position_weight"] += weight
            item["dates"].add(row.observed_on)
            page = pages[(raw.market_id, row.page)]
            page["market"] = raw.market
            page["clicks"] += row.clicks
            page["impressions"] += row.impressions
            page["keywords"].add(normalised)

    created_snapshots = updated_snapshots = created_observations = updated_observations = 0
    with transaction.atomic():
        for market, row, normalised in snapshots:
            _, created = PositionSnapshot.objects.update_or_create(
                market=market,
                observed_on=row.observed_on,
                keyword_normalised=normalised,
                page_url=row.page,
                country=row.country.lower(),
                defaults={
                    "last_seen_run": run,
                    "keyword": row.query,
                    "clicks": row.clicks,
                    "impressions": row.impressions,
                    "ctr": row.ctr,
                    "position": row.position,
                },
            )
            created_snapshots += int(created)
            updated_snapshots += int(not created)

        for (_, normalised, page_url), item in observations.items():
            quick_win_min, quick_win_max = _quick_win_range(
                run, item["market"].code
            )
            decay = _decay_settings(run, item["market"].code)
            position = item["weighted_position"] / item["position_weight"]
            previous_position = _baseline_position(
                item["market"], normalised, page_url, min(item["dates"]), decay["baseline_days"]
            )
            is_decay = (
                previous_position is not None
                and previous_position <= decay["baseline_max"]
                and position >= decay["current_min"]
                and position - previous_position >= decay["min_drop"]
            )
            signal = (
                "ranking_decay" if is_decay
                else "quick_win" if quick_win_min <= position <= quick_win_max
                else "existing_ranking"
            )
            impressions = int(round(item["impressions"]))
            clicks = int(round(item["clicks"]))
            KeywordObservation.objects.filter(
                run=run, market=item["market"], keyword_normalised=normalised,
                source="gsc", competitor_domain="", our_url=page_url,
            ).exclude(signal=signal).delete()
            _, created = KeywordObservation.objects.update_or_create(
                run=run,
                market=item["market"],
                keyword_normalised=normalised,
                source="gsc",
                signal=signal,
                competitor_domain="",
                our_url=page_url,
                defaults={
                    "keyword": item["keyword"],
                    "our_position": position,
                    "previous_position": previous_position,
                    "impressions": impressions,
                    "clicks": clicks,
                    "ctr": (item["clicks"] / item["impressions"]) if item["impressions"] else 0,
                },
            )
            created_observations += int(created)
            updated_observations += int(not created)

        for (_, page_url), item in pages.items():
            page, _ = ExistingPage.objects.get_or_create(
                market=item["market"],
                url=page_url,
                defaults={"path": urlsplit(page_url).path or "/", "in_sitemap": False},
            )
            page.total_clicks_28d = int(round(item["clicks"]))
            page.total_impressions_28d = int(round(item["impressions"]))
            page.ranking_keyword_count = len(item["keywords"])
            page.save(update_fields=[
                "total_clicks_28d", "total_impressions_28d", "ranking_keyword_count", "updated_at"
            ])

    return {
        "snapshots_created": created_snapshots,
        "snapshots_updated": updated_snapshots,
        "observations_created": created_observations,
        "observations_updated": updated_observations,
        "pages_updated": len(pages),
    }


def _normalise_ga4(run: Run, fetches: list[RawFetch]) -> dict:
    pages = defaultdict(lambda: {
        "sessions": Decimal("0"), "conversions": Decimal("0"), "revenue": Decimal("0")
    })
    skipped = 0
    for raw in fetches:
        response = _parse_fetch(raw, GA4RunReportResponse)
        for record in response.records():
            path = _canonical_path(record.get("landingPagePlusQueryString", ""))
            if not path:
                skipped += 1
                continue
            item = pages[(raw.market_id, path)]
            item["market"] = raw.market
            item["sessions"] += record.get("sessions", Decimal("0"))
            item["conversions"] += record.get("keyEvents", record.get("conversions", Decimal("0")))
            item["revenue"] += record.get("purchaseRevenue", Decimal("0"))

    with transaction.atomic():
        for (_, path), item in pages.items():
            page = ExistingPage.objects.filter(
                market=item["market"], path=path
            ).order_by("pk").first()
            if page is None:
                domain = item["market"].client.primary_domain.strip().rstrip("/")
                if not domain.startswith(("http://", "https://")):
                    domain = f"https://{domain}"
                page = ExistingPage.objects.create(
                    market=item["market"], url=f"{domain}{path}", path=path, in_sitemap=False
                )
            sessions = int(round(item["sessions"]))
            conversions = int(round(item["conversions"]))
            page.sessions_28d = sessions
            page.conversions_28d = conversions
            page.conversion_rate = conversions / sessions if sessions else 0
            page.revenue_28d = item["revenue"]
            page.save(update_fields=[
                "sessions_28d", "conversions_28d", "conversion_rate", "revenue_28d", "updated_at"
            ])

    return {"pages_updated": len(pages), "rows_skipped": skipped}


def run_stage_analytics(run: Run) -> dict:
    stage, _ = RunStage.objects.update_or_create(
        run=run,
        name="analytics",
        defaults={"status": "running", "started_at": timezone.now(), "finished_at": None, "error": ""},
    )
    gsc_fetches = list(
        RawFetch.objects.filter(run=run, source="gsc")
        .exclude(payload__has_key="error").select_related("market")
    )
    ga4_fetches = list(
        RawFetch.objects.filter(run=run, source="ga4")
        .exclude(payload__has_key="error").select_related("market")
    )
    try:
        gsc = _normalise_gsc(run, gsc_fetches)
        ga4 = _normalise_ga4(run, ga4_fetches)
    except Exception as exc:
        stage.status = "failed"
        stage.error = str(exc)
        stage.finished_at = timezone.now()
        stage.save(update_fields=["status", "error", "finished_at"])
        raise

    stage.status = "complete"
    stage.records_in = len(gsc_fetches) + len(ga4_fetches)
    stage.records_out = (
        gsc["observations_created"] + gsc["observations_updated"] + ga4["pages_updated"]
    )
    stage.finished_at = timezone.now()
    stage.error = ""
    stage.save(update_fields=["status", "records_in", "records_out", "finished_at", "error"])
    return {"gsc": gsc, "ga4": ga4, "raw_fetches_processed": stage.records_in}

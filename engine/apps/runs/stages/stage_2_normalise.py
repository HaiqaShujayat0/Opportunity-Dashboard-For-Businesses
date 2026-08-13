"""Stage 2: turn immutable DataForSEO responses into typed observations."""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.connectors.dataforseo.connector import (
    dataforseo_response_is_declared_failure,
)
from apps.ingestion.models import KeywordObservation, RawFetch
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)

KEYWORD_DISCOVERY_ENDPOINTS = (
    "keyword_ideas",
    "keyword_suggestions",
    "related_keywords",
)


class PayloadStructureError(ValueError):
    """A saved external response does not have the structure we require."""


def _normalise_keyword(keyword: str) -> str:
    return " ".join(keyword.lower().strip().split())


def _items(payload: dict, endpoint: str) -> list:
    """Extract items while distinguishing a valid empty result from malformed data."""
    try:
        tasks = payload["tasks"]
        task = tasks[0]
        results = task["result"]
        if not results:
            return []
        result = results[0]
        if result is None:
            return []
        items = result.get("items") or []
    except (KeyError, IndexError, TypeError) as exc:
        raise PayloadStructureError(
            f"Malformed DataForSEO payload for {endpoint}: expected "
            "tasks[0].result[0].items"
        ) from exc
    if not isinstance(items, list):
        raise PayloadStructureError(
            f"Malformed DataForSEO payload for {endpoint}: items must be a list"
        )
    return items


def _parse_keyword_ideas(payload: dict, endpoint: str) -> list[dict]:
    rows = []
    for item in _items(payload, endpoint):
        if not isinstance(item, dict):
            raise PayloadStructureError(f"Malformed item in {endpoint}: expected an object")
        # related_keywords wraps the same keyword fields in keyword_data;
        # ideas and suggestions return them at the item level.
        keyword_data = item.get("keyword_data") or item
        kw_info = keyword_data.get("keyword_info") or {}
        kw_props = keyword_data.get("keyword_properties") or {}
        rows.append({
            "keyword": keyword_data.get("keyword", ""),
            "signal": "keyword_research",
            "search_volume": kw_info.get("search_volume") or 0,
            "cpc": kw_info.get("cpc") or 0.0,
            "competition": kw_info.get("competition"),
            "keyword_difficulty": kw_props.get("keyword_difficulty"),
            "competitor_domain": "",
        })
    return rows


def _parse_domain_intersection(payload: dict, endpoint: str, competitor_domain="") -> list[dict]:
    rows = []
    for item in _items(payload, endpoint):
        if not isinstance(item, dict):
            raise PayloadStructureError(f"Malformed item in {endpoint}: expected an object")
        kw_data = item.get("keyword_data") or {}
        kw_info = kw_data.get("keyword_info") or {}
        kw_props = kw_data.get("keyword_properties") or {}
        competitor_result = item.get("first_domain_serp_element") or {}
        if not isinstance(competitor_result, dict):
            raise PayloadStructureError(
                f"Malformed item in {endpoint}: "
                "first_domain_serp_element must be an object"
            )

        # target1 is the competitor in Stage 1, so its ranking URL is stored in
        # first_domain_serp_element. Keep legacy top-level fallbacks so older
        # saved fixtures and receipts remain reprocessable.
        search_volume = kw_info.get("search_volume")
        if search_volume is None:
            search_volume = kw_data.get("search_volume")
        if search_volume is None:
            search_volume = item.get("search_volume")

        cpc = kw_info.get("cpc")
        if cpc is None:
            cpc = kw_data.get("cpc")
        if cpc is None:
            cpc = item.get("cpc")

        competition = kw_info.get("competition")
        if competition is None:
            competition = kw_data.get("competition")
        if competition is None:
            competition = item.get("competition")

        keyword_difficulty = kw_props.get("keyword_difficulty")
        if keyword_difficulty is None:
            keyword_difficulty = kw_data.get("keyword_difficulty")
        if keyword_difficulty is None:
            keyword_difficulty = item.get("keyword_difficulty")

        rows.append({
            "keyword": kw_data.get("keyword", ""),
            "signal": "competitor_gap",
            "search_volume": search_volume or 0,
            "cpc": cpc if cpc is not None else 0.0,
            "competition": competition,
            "keyword_difficulty": keyword_difficulty,
            "competitor_domain": competitor_domain,
            "competitor_url": competitor_result.get("url") or "",
        })
    return rows


def _parse_relevant_pages(payload: dict, endpoint: str, competitor_domain="") -> list[dict]:
    rows = []
    for item in _items(payload, endpoint):
        if not isinstance(item, dict):
            raise PayloadStructureError(f"Malformed item in {endpoint}: expected an object")
        page_address = item.get("page_address", "")
        if page_address:
            rows.append({
                "keyword": page_address,
                "signal": "competitor_top_page",
                "search_volume": None,
                "cpc": None,
                "competition": None,
                "keyword_difficulty": None,
                "competitor_domain": competitor_domain,
                "competitor_url": page_address,
            })
    return rows


def _difficulty_map(raw_fetches) -> dict[tuple[int, str], int]:
    difficulty = {}
    for raw in raw_fetches:
        if "bulk_keyword_difficulty" not in raw.endpoint:
            continue
        for item in _items(raw.payload, raw.endpoint):
            if not isinstance(item, dict):
                raise PayloadStructureError(
                    f"Malformed item in {raw.endpoint}: expected an object"
                )
            keyword = _normalise_keyword(item.get("keyword") or "")
            score = item.get("keyword_difficulty")
            if keyword and score is not None:
                difficulty[(raw.market_id, keyword)] = int(score)
    return difficulty


def _observation_defaults(row: dict) -> dict:
    cpc = row.get("cpc")
    return {
        "keyword": row["keyword"].strip(),
        "search_volume": row.get("search_volume"),
        "cpc": Decimal(str(cpc)) if cpc is not None else None,
        "competition": row.get("competition"),
        "keyword_difficulty": row.get("keyword_difficulty"),
        "competitor_url": row.get("competitor_url", ""),
    }


def run_stage_normalise(run: Run) -> dict:
    logger.info("[Stage 2 -- NORMALISE] Starting for Run #%s", run.pk)
    stage, _ = RunStage.objects.update_or_create(
        run=run,
        name="normalise",
        defaults={"status": "running", "started_at": timezone.now(), "finished_at": None, "error": ""},
    )
    candidate_fetches = list(
        RawFetch.objects.filter(run=run, source="dataforseo")
        .exclude(payload__has_key="error")
        .order_by("pk")
    )
    raw_fetches = [
        raw for raw in candidate_fetches
        if not dataforseo_response_is_declared_failure(raw.payload)
    ]
    failed_receipts = len(candidate_fetches) - len(raw_fetches)
    if failed_receipts:
        logger.warning(
            "[Stage 2 -- NORMALISE] Skipping %s failed DataForSEO task receipt(s).",
            failed_receipts,
        )
    if not raw_fetches:
        raise RuntimeError(f"Run #{run.pk} has no DataForSEO RawFetch rows. Did INGEST run first?")

    difficulty = _difficulty_map(raw_fetches)
    created = updated = skipped = 0
    try:
        for raw in raw_fetches:
            endpoint = raw.endpoint
            if "bulk_keyword_difficulty" in endpoint or "competitors_domain" in endpoint:
                continue
            if any(name in endpoint for name in KEYWORD_DISCOVERY_ENDPOINTS):
                rows = _parse_keyword_ideas(raw.payload, endpoint)
            elif "domain_intersection" in endpoint:
                rows = _parse_domain_intersection(raw.payload, endpoint, raw.request_params.get("target1", ""))
            elif "relevant_pages" in endpoint:
                rows = _parse_relevant_pages(raw.payload, endpoint, raw.request_params.get("target", ""))
            else:
                logger.warning(
                    "[Stage 2 -- NORMALISE] Skipping unknown endpoint %s "
                    "(RawFetch #%s). No keyword data expected.",
                    endpoint, raw.pk,
                )
                continue

            for row in rows:
                keyword = (row.get("keyword") or "").strip()
                if not keyword:
                    skipped += 1
                    continue
                normalised = _normalise_keyword(keyword)
                if row.get("keyword_difficulty") is None:
                    row["keyword_difficulty"] = difficulty.get((raw.market_id, normalised))
                _, was_created = KeywordObservation.objects.update_or_create(
                    run=run,
                    market=raw.market,
                    keyword_normalised=normalised,
                    source="dataforseo",
                    signal=row["signal"],
                    competitor_domain=row.get("competitor_domain", ""),
                    defaults=_observation_defaults(row),
                )
                created += int(was_created)
                updated += int(not was_created)
    except Exception as exc:
        stage.status = "failed"
        stage.error = str(exc)
        stage.finished_at = timezone.now()
        stage.save(update_fields=["status", "error", "finished_at"])
        raise

    output_count = KeywordObservation.objects.filter(run=run).count()
    stage.status = "complete"
    stage.records_in = len(raw_fetches)
    stage.records_out = output_count
    stage.finished_at = timezone.now()
    stage.error = ""
    stage.save(update_fields=["status", "records_in", "records_out", "finished_at", "error"])
    return {
        "raw_fetches_processed": len(raw_fetches),
        "observations_created": created,
        "observations_updated": updated,
        "observations_total": output_count,
        "observations_skipped": skipped,
    }

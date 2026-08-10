"""Stage 2: turn immutable DataForSEO responses into typed observations."""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.ingestion.models import KeywordObservation, RawFetch
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)


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
        result = results[0]
        items = result["items"]
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
        kw_info = item.get("keyword_info") or {}
        kw_props = item.get("keyword_properties") or {}
        rows.append({
            "keyword": item.get("keyword", ""),
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
        rows.append({
            "keyword": kw_data.get("keyword", ""),
            "signal": "competitor_gap",
            "search_volume": kw_data.get("search_volume") or item.get("search_volume") or 0,
            "cpc": kw_data.get("cpc") or item.get("cpc") or 0.0,
            "competition": kw_data.get("competition") if kw_data.get("competition") is not None else item.get("competition"),
            "keyword_difficulty": kw_data.get("keyword_difficulty") if kw_data.get("keyword_difficulty") is not None else item.get("keyword_difficulty"),
            "competitor_domain": competitor_domain,
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
    raw_fetches = list(
        RawFetch.objects.filter(run=run, source="dataforseo")
        .exclude(payload__has_key="error")
        .order_by("pk")
    )
    if not raw_fetches:
        raise RuntimeError(f"Run #{run.pk} has no DataForSEO RawFetch rows. Did INGEST run first?")

    difficulty = _difficulty_map(raw_fetches)
    created = updated = skipped = 0
    try:
        for raw in raw_fetches:
            endpoint = raw.endpoint
            if "bulk_keyword_difficulty" in endpoint:
                continue
            if "keyword_ideas" in endpoint:
                rows = _parse_keyword_ideas(raw.payload, endpoint)
            elif "domain_intersection" in endpoint:
                rows = _parse_domain_intersection(raw.payload, endpoint, raw.request_params.get("target1", ""))
            elif "relevant_pages" in endpoint:
                rows = _parse_relevant_pages(raw.payload, endpoint, raw.request_params.get("target", ""))
            else:
                raise PayloadStructureError(f"Unsupported DataForSEO endpoint in normalisation: {endpoint}")

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

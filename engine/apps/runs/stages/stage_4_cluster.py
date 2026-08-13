"""Stage 4: build market-isolated, stable topics from keyword observations."""
import hashlib
import logging
from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from apps.clients.models import Market
from apps.ingestion.models import KeywordObservation, RawFetch
from apps.runs.models import Run, RunStage
from apps.runs.stages.stage_2_normalise import KEYWORD_DISCOVERY_ENDPOINTS
from apps.runs.stages.settings_snapshot import engine_settings_for_market
from apps.topics.models import Topic, TopicKeyword

logger = logging.getLogger(__name__)


def _tokenize(keyword):
    return set(keyword.lower().strip().split())


def _jaccard(set_a, set_b):
    return len(set_a & set_b) / len(set_a | set_b) if set_a and set_b else 0.0


def _generate_topic_uid(market_code, keywords):
    # Preserve the UID algorithm used by the existing prototype. Changing its
    # casing or ordering would orphan downstream opportunities and sheet rows.
    canonical = sorted(set(keywords))[:3]
    raw = f"{market_code}:{'|'.join(canonical)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _extract_core_keywords(run, market):
    core_map = {}
    fetches = RawFetch.objects.filter(
        run=run,
        market=market,
        source="dataforseo",
    ).exclude(payload__has_key="error")
    for raw in fetches:
        if not any(name in raw.endpoint for name in KEYWORD_DISCOVERY_ENDPOINTS):
            continue
        try:
            items = raw.payload["tasks"][0]["result"][0]["items"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(
                f"Malformed keyword_ideas payload in RawFetch #{raw.pk}"
            ) from exc
        if not isinstance(items, list):
            raise ValueError(f"Malformed keyword_ideas items in RawFetch #{raw.pk}")
        for item in items:
            keyword_data = item.get("keyword_data") or item
            keyword = (keyword_data.get("keyword") or "").lower().strip()
            core = (
                (keyword_data.get("keyword_properties") or {}).get("core_keyword")
                or ""
            ).lower().strip()
            if keyword and core:
                core_map[keyword] = core
    return core_map


def _aggregate_keywords(observations):
    data = {}
    for obs in observations.iterator():
        if obs.keyword.lower().startswith(("http://", "https://")):
            continue
        norm = obs.keyword_normalised or obs.keyword.lower().strip()
        entry = data.setdefault(norm, {
            "keyword": obs.keyword,
            "normalised": norm,
            "search_volume": obs.search_volume or 0,
            "keyword_difficulty": obs.keyword_difficulty,
            "intent": obs.intent or "",
            "serp_urls": set(),
        })
        entry["serp_urls"].update(obs.serp_top_urls or [])
        if (obs.search_volume or 0) > entry["search_volume"]:
            entry["search_volume"] = obs.search_volume or 0
            entry["keyword"] = obs.keyword
        if entry["keyword_difficulty"] is None and obs.keyword_difficulty is not None:
            entry["keyword_difficulty"] = obs.keyword_difficulty
        if not entry["intent"] and obs.intent:
            entry["intent"] = obs.intent
    return data


def _cluster_keywords(keyword_data, core_map, threshold, serp_overlap_threshold=3):
    clusters = {}
    keyword_to_cluster = {}
    for norm in keyword_data:
        core = core_map.get(norm)
        if core and core in keyword_to_cluster:
            cluster_id = keyword_to_cluster[core]
            clusters[cluster_id].add(norm)
            keyword_to_cluster[norm] = cluster_id
        else:
            cluster_id = len(clusters)
            clusters[cluster_id] = {norm}
            keyword_to_cluster[norm] = cluster_id
            if core:
                keyword_to_cluster[core] = cluster_id

    tokens = {cid: set().union(*(_tokenize(k) for k in members)) for cid, members in clusters.items()}
    serp_urls = {
        cid: set().union(*(keyword_data[keyword]["serp_urls"] for keyword in members))
        for cid, members in clusters.items()
    }
    merged = True
    while merged:
        merged = False
        ids = list(clusters)
        for position, first in enumerate(ids):
            for second in ids[position + 1:]:
                if first not in clusters or second not in clusters:
                    continue
                shared_serp_urls = serp_urls[first] & serp_urls[second]
                if (
                    len(shared_serp_urls) >= serp_overlap_threshold
                    or _jaccard(tokens[first], tokens[second]) >= threshold
                ):
                    clusters[first].update(clusters.pop(second))
                    tokens[first].update(tokens.pop(second))
                    serp_urls[first].update(serp_urls.pop(second))
                    merged = True
    return list(clusters.values())


@transaction.atomic
def _persist_market_topics(run, market, keyword_data, clusters):
    topics_created = topics_updated = assignments = 0
    for members in clusters:
        member_data = [keyword_data[norm] for norm in members]
        member_data.sort(key=lambda item: (-item["search_volume"], len(item["keyword"]), item["keyword"]))
        primary = member_data[0]
        uid = _generate_topic_uid(market.code, [item["keyword"] for item in member_data])
        intents = defaultdict(int)
        for item in member_data:
            if item["intent"]:
                intents[item["intent"]] += 1
        dominant_intent = max(intents, key=intents.get) if intents else ""
        defaults = {
            "client": market.client,
            "market": market,
            "label": primary["keyword"].title(),
            "primary_keyword": primary["keyword"],
            "primary_keyword_volume": primary["search_volume"],
            "total_search_volume": sum(item["search_volume"] for item in member_data),
            "intent": dominant_intent,
            "last_seen_run": run,
        }
        topic, created = Topic.objects.update_or_create(
            topic_uid=uid,
            defaults=defaults,
        )
        if created:
            topic.first_seen_run = run
            topic.save(update_fields=["first_seen_run"])
            topics_created += 1
        else:
            topics_updated += 1
        topic.keywords.all().delete()
        TopicKeyword.objects.bulk_create([
            TopicKeyword(
                topic=topic,
                keyword=item["keyword"],
                search_volume=item["search_volume"],
                is_primary=index == 0,
                keyword_difficulty=item["keyword_difficulty"],
            )
            for index, item in enumerate(member_data)
        ])
        assignments += len(member_data)
    return topics_created, topics_updated, assignments


def run_stage_cluster(run, similarity_threshold=0.35):
    stage, _ = RunStage.objects.update_or_create(
        run=run,
        name="cluster",
        defaults={"status": "running", "started_at": timezone.now(), "finished_at": None, "error": ""},
    )
    observations = KeywordObservation.objects.filter(run=run)
    total = observations.count()
    if not total:
        raise RuntimeError(f"Run #{run.pk} has no KeywordObservation rows. Did NORMALISE run first?")

    created = updated = assignments = unique_count = 0
    market_summaries = {}
    try:
        market_ids = observations.values_list("market_id", flat=True).distinct()
        for market in Market.objects.filter(id__in=market_ids).select_related("client"):
            settings = engine_settings_for_market(run, market.code)
            serp_overlap_threshold = int(
                settings.get("serp_overlap_threshold", 3)
            )
            semantic_similarity_threshold = float(
                settings.get("semantic_similarity_threshold", similarity_threshold)
            )
            market_observations = observations.filter(market=market)
            keyword_data = _aggregate_keywords(market_observations)
            unique_count += len(keyword_data)
            clusters = _cluster_keywords(
                keyword_data,
                _extract_core_keywords(run, market),
                semantic_similarity_threshold,
                serp_overlap_threshold,
            ) if keyword_data else []
            market_created, market_updated, market_assignments = _persist_market_topics(
                run, market, keyword_data, clusters
            )
            created += market_created
            updated += market_updated
            assignments += market_assignments
            market_summaries[market.code] = {
                "unique_keywords": len(keyword_data),
                "topics": len(clusters),
                "serp_overlap_threshold": serp_overlap_threshold,
                "semantic_similarity_threshold": semantic_similarity_threshold,
            }
    except Exception as exc:
        stage.status = "failed"
        stage.error = str(exc)
        stage.finished_at = timezone.now()
        stage.save(update_fields=["status", "error", "finished_at"])
        raise

    stage.status = "complete"
    stage.records_in = total
    stage.records_out = created + updated
    stage.finished_at = timezone.now()
    stage.error = ""
    stage.save(update_fields=["status", "records_in", "records_out", "finished_at", "error"])
    return {
        "total_observations": total,
        "unique_keywords": unique_count,
        "topics_created": created,
        "topics_updated": updated,
        "topic_keywords_created": assignments,
        "markets": market_summaries,
    }

"""Stage 5: match topics to pages, preferring direct GSC ranking evidence."""

import logging
import re
from collections import defaultdict

from django.utils import timezone

from apps.ingestion.models import KeywordObservation
from apps.pages.models import ExistingPage
from apps.runs.models import RunStage
from apps.topics.models import Topic

logger = logging.getLogger(__name__)


def _normalise(value):
    return " ".join(value.lower().strip().split())


def _path_tokens(path):
    return set(filter(None, re.split(r"[^a-z0-9]+", path.lower())))


def _slug_score(keywords, path):
    path_lower = path.lower()
    path_set = _path_tokens(path)
    best = 0.0
    for keyword in keywords:
        slug = _normalise(keyword).replace(" ", "-")
        if slug and slug in path_lower:
            return 1.0
        keyword_set = set(_normalise(keyword).split())
        if keyword_set and path_set:
            best = max(best, len(keyword_set & path_set) / len(keyword_set | path_set))
    return best


def _ranking_match(run, topic, keywords):
    normalised = [_normalise(keyword) for keyword in keywords]
    observations = KeywordObservation.objects.filter(
        run=run,
        market=topic.market,
        source="gsc",
        keyword_normalised__in=normalised,
        our_url__gt="",
    )
    by_url = defaultdict(lambda: {
        "clicks": 0, "impressions": 0, "weighted_position": 0.0,
        "weighted_previous_position": 0.0, "previous_position_weight": 0.0,
        "position_weight": 0.0, "keywords": set(),
    })
    keyword_urls = defaultdict(set)
    for observation in observations.iterator():
        item = by_url[observation.our_url]
        impressions = observation.impressions or 0
        weight = impressions if impressions > 0 else 1
        item["clicks"] += observation.clicks or 0
        item["impressions"] += impressions
        item["weighted_position"] += (observation.our_position or 0) * weight
        item["position_weight"] += weight
        if observation.previous_position is not None:
            item["weighted_previous_position"] += observation.previous_position * weight
            item["previous_position_weight"] += weight
        item["keywords"].add(observation.keyword_normalised)
        keyword_urls[observation.keyword_normalised].add(observation.our_url)

    candidates = []
    for url, item in by_url.items():
        item["url"] = url
        item["position"] = (
            item["weighted_position"] / item["position_weight"]
            if item["position_weight"] else None
        )
        item["previous_position"] = (
            item["weighted_previous_position"] / item["previous_position_weight"]
            if item["previous_position_weight"] else None
        )
        item["keywords"] = sorted(item["keywords"])
        candidates.append(item)
    candidates.sort(key=lambda item: (-item["clicks"], -item["impressions"], item["position"] or 999))
    overlaps = sorted(keyword for keyword, urls in keyword_urls.items() if len(urls) >= 2)
    return candidates, overlaps


def run_stage_match(run):
    logger.info("[Stage 5 -- MATCH] Starting for Run #%s", run.pk)
    topics = Topic.objects.filter(last_seen_run=run).select_related("market")
    total_topics = topics.count()
    if total_topics == 0:
        raise RuntimeError(f"Run #{run.pk} has no Topics. Did Stage 4 (CLUSTER) run first?")

    matched = unmatched = cannibalisation = 0
    match_results = {}
    for topic in topics.iterator():
        keywords = list(topic.keywords.values_list("keyword", flat=True))
        ranking_candidates, overlapping_keywords = _ranking_match(run, topic, keywords)
        if ranking_candidates:
            matched_urls = [candidate["url"] for candidate in ranking_candidates]
            best = ranking_candidates[0]
            result = {
                "matched": True,
                "matched_url": best["url"],
                "matched_urls": matched_urls,
                "match_source": "gsc_ranking",
                "current_position": best["position"],
                "previous_position": best["previous_position"],
                "ranking_candidates": ranking_candidates,
                "overlapping_ranking_keywords": overlapping_keywords,
                "cannibalisation": len(overlapping_keywords) > 0,
            }
        else:
            best_page = None
            best_score = 0.0
            for page in ExistingPage.objects.filter(market=topic.market).iterator():
                score = _slug_score(keywords, page.path)
                if score > best_score:
                    best_page, best_score = page, score
            if best_page is not None and best_score >= 0.5:
                result = {
                    "matched": True,
                    "matched_url": best_page.url,
                    "matched_urls": [best_page.url],
                    "match_source": "slug",
                    "match_score": best_score,
                    "current_position": None,
                    "previous_position": None,
                    "ranking_candidates": [],
                    "overlapping_ranking_keywords": [],
                    "cannibalisation": False,
                }
            else:
                result = {
                    "matched": False, "matched_url": "", "matched_urls": [],
                    "match_source": "none", "current_position": None,
                    "previous_position": None,
                    "ranking_candidates": [], "overlapping_ranking_keywords": [],
                    "cannibalisation": False,
                }
        match_results[topic.pk] = result
        if result["matched"]:
            matched += 1
            cannibalisation += int(result["cannibalisation"])
        else:
            unmatched += 1

    RunStage.objects.update_or_create(
        run=run, name="match",
        defaults={
            "status": "complete", "records_in": total_topics, "records_out": matched,
            "started_at": timezone.now(), "finished_at": timezone.now(), "error": "",
        },
    )
    return {
        "total_topics": total_topics, "matched": matched, "unmatched": unmatched,
        "cannibalisation": cannibalisation, "match_results": match_results,
    }

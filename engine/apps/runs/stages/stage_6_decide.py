"""Stage 6: apply the auditable Engine 1 action decision matrix."""

import logging

from django.utils import timezone

from apps.ingestion.models import KeywordObservation
from apps.opportunities.models import Opportunity
from apps.pages.models import ExistingPage
from apps.runs.models import RunStage
from apps.topics.models import Topic

logger = logging.getLogger(__name__)

DIFFICULTY_BUCKETS = [(0, 20, "Easy"), (21, 40, "Medium"), (41, 60, "Hard"), (61, 100, "Very Hard")]


def _get_difficulty_label(score):
    if score is None:
        return "Unknown"
    return next((label for low, high, label in DIFFICULTY_BUCKETS if low <= score <= high), "Unknown")


def _suggest_page_type(intent, search_volume):
    if intent == "transactional":
        return "category_page"
    if intent in {"commercial", "informational"}:
        return "category_page" if intent == "commercial" and search_volume > 5000 else "blog_post"
    if intent == "navigational":
        return "landing_page"
    return "category_page" if search_volume > 10000 else "blog_post"


def _suggest_slug(primary_keyword):
    slug = "".join(character for character in primary_keyword.lower().strip().replace(" ", "-") if character.isalnum() or character == "-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"/{slug}"


def _settings(run):
    values = (run.settings_snapshot or {}).get("engine_settings", {})
    return {
        "min_volume": int(values.get("min_search_volume", 50)),
        "max_difficulty": int(values.get("max_keyword_difficulty", 100)),
    }


def _conversion_evidence(topic, target_urls):
    pages = list(ExistingPage.objects.filter(market=topic.market, url__in=target_urls))
    sessions = sum(page.sessions_28d for page in pages)
    conversions = sum(page.conversions_28d for page in pages)
    if sessions > 0:
        rate = conversions / sessions
        market_rates = sorted(
            rate for rate in ExistingPage.objects.filter(
                market=topic.market, sessions_28d__gt=0, conversion_rate__isnull=False
            ).values_list("conversion_rate", flat=True)
        )
        median = market_rates[len(market_rates) // 2] if market_rates else rate
        if rate >= max(0.03, median * 1.5):
            potential = "High"
        elif rate >= max(0.01, median * 0.75):
            potential = "Medium"
        else:
            potential = "Low"
        return potential, "data", rate
    inferred = "High" if topic.intent == "transactional" else "Medium" if topic.intent == "commercial" else "Low"
    return inferred, "inferred", None


def run_stage_decide(run, match_results=None):
    topics = Topic.objects.filter(last_seen_run=run).select_related("market")
    total_topics = topics.count()
    if total_topics == 0:
        raise RuntimeError(f"Run #{run.pk} has no Topics. Did Stage 4 (CLUSTER) run first?")
    match_results = match_results or {}
    Opportunity.objects.filter(run=run).delete()
    limits = _settings(run)
    counts = {"new_content": 0, "optimise": 0, "merge": 0, "ignore": 0}

    for topic in topics.iterator():
        primary = topic.keywords.filter(is_primary=True).first() or topic.keywords.order_by("-search_volume").first()
        if primary is None:
            continue
        keywords = list(topic.keywords.values_list("keyword", flat=True))
        observations = KeywordObservation.objects.filter(run=run, market=topic.market, keyword__in=keywords)
        signals = sorted(set(observations.values_list("signal", flat=True)))
        difficulty_score = primary.keyword_difficulty
        difficulty_label = _get_difficulty_label(difficulty_score)
        match = match_results.get(topic.pk, {})
        target_urls = list(match.get("matched_urls") or ([match["matched_url"]] if match.get("matched_url") else []))
        position = match.get("current_position")
        previous_position = match.get("previous_position")
        has_decay = "ranking_decay" in signals

        if match.get("cannibalisation") and len(target_urls) >= 2:
            action, reason = "merge", "multiple pages rank for the same topic keyword"
        elif not match.get("matched"):
            if topic.total_search_volume < limits["min_volume"]:
                action, reason = "ignore", "search volume below configured minimum"
            elif difficulty_score is not None and difficulty_score > limits["max_difficulty"]:
                action, reason = "ignore", "difficulty above configured maximum"
            else:
                action, reason = "new_content", "no existing page match"
        elif position is not None and position <= 3 and not has_decay:
            action, reason = "ignore", "existing page already ranks in positions 1-3"
        else:
            action, reason = "optimise", "existing page has improvement opportunity"
        counts[action] += 1

        conversion_potential, conversion_basis, conversion_rate = _conversion_evidence(topic, target_urls)
        trace = {
            "rule": reason,
            "volume": topic.total_search_volume,
            "minimum_volume": limits["min_volume"],
            "difficulty": difficulty_score,
            "maximum_difficulty": limits["max_difficulty"],
            "match_source": match.get("match_source", "none"),
            "matched_urls": target_urls,
            "current_position": position,
            "previous_position": previous_position,
            "overlapping_ranking_keywords": match.get("overlapping_ranking_keywords", []),
            "signals": signals,
            "conversion_rate": conversion_rate,
        }
        Opportunity.objects.create(
            run=run, topic=topic, market=topic.market, action=action,
            target_urls=target_urls, why_flagged=signals,
            current_position=position, previous_position=previous_position,
            difficulty=difficulty_label,
            difficulty_score=difficulty_score, page_type=_suggest_page_type(topic.intent, topic.total_search_volume),
            suggested_slug=_suggest_slug(topic.primary_keyword),
            conversion_potential=conversion_potential, conversion_basis=conversion_basis,
            decision_trace=trace,
        )

    RunStage.objects.update_or_create(
        run=run, name="decide",
        defaults={
            "status": "complete", "records_in": total_topics,
            "records_out": total_topics - counts["ignore"],
            "started_at": timezone.now(), "finished_at": timezone.now(), "error": "",
        },
    )
    return {
        "total_topics": total_topics, **counts,
        "opportunities_created": sum(counts.values()),
    }

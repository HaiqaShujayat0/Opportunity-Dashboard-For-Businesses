"""Stage 6: apply the auditable Engine 1 action decision matrix."""

import logging

from django.utils import timezone

from apps.ingestion.models import KeywordObservation
from apps.opportunities.models import Opportunity
from apps.pages.models import ExistingPage
from apps.runs.models import RunStage
from apps.runs.stages.settings_snapshot import engine_settings_for_market
from apps.topics.models import Topic

logger = logging.getLogger(__name__)

DIFFICULTY_BUCKETS = [(0, 20, "Easy"), (21, 40, "Medium"), (41, 60, "Hard"), (61, 100, "Very Hard")]
CTR_BY_POSITION = {
    1: 0.32,
    2: 0.16,
    3: 0.10,
    4: 0.07,
    5: 0.05,
    6: 0.04,
    7: 0.03,
    8: 0.02,
    9: 0.015,
    10: 0.01,
}


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


def _settings(run, market_code):
    values = engine_settings_for_market(run, market_code)
    return {
        "min_volume": int(values.get("min_search_volume", 50)),
        "max_difficulty": int(values.get("max_keyword_difficulty", 100)),
        "new_content_target_position": int(
            values.get("new_content_target_position", 8)
        ),
        "position_gain": int(values.get("estimated_impact_position_gain", 3)),
    }


def _ctr(position):
    """Return the agreed generic CTR assumption for a ranking position."""
    if position is None:
        return 0.0
    position = max(1, int(position))
    return CTR_BY_POSITION.get(position, 0.001)


def _estimated_impact(topic, action, current_position, conversion_rate, limits):
    if action == "new_content":
        target_position = max(1, limits["new_content_target_position"])
    elif action in {"optimise", "merge"} and current_position is not None:
        target_position = max(1, int(current_position) - limits["position_gain"])
    else:
        target_position = None

    current_ctr = _ctr(current_position)
    target_ctr = _ctr(target_position) if target_position is not None else current_ctr
    delta_clicks = max(
        0.0,
        topic.total_search_volume * (target_ctr - current_ctr),
    )
    delta_conversions = (
        delta_clicks * conversion_rate if conversion_rate is not None else None
    )
    return {
        "monthly_clicks_gain": int(delta_clicks),
        "monthly_conversions_gain": (
            int(delta_conversions) if delta_conversions is not None else None
        ),
    }, {
        "total_search_volume": topic.total_search_volume,
        "current_position": current_position,
        "target_position": target_position,
        "current_ctr": current_ctr,
        "target_ctr": target_ctr,
        "position_gain_assumption": limits["position_gain"],
        "conversion_rate": conversion_rate,
    }


def _best_competitor_url(topic, observations):
    """Choose primary-keyword evidence first, then the highest-volume row."""
    primary_keyword = " ".join(topic.primary_keyword.lower().strip().split())
    candidates = [
        observation for observation in observations
        if (observation.competitor_url or "").strip()
    ]
    if not candidates:
        return "", None
    best = min(
        candidates,
        key=lambda observation: (
            0 if (
                " ".join(
                    (observation.keyword_normalised or observation.keyword)
                    .lower().strip().split()
                ) == primary_keyword
            ) else 1,
            -(observation.search_volume or 0),
            observation.pk,
        ),
    )
    return best.competitor_url.strip(), {
        "observation_id": best.pk,
        "keyword": best.keyword,
        "search_volume": best.search_volume,
        "competitor_domain": best.competitor_domain,
        "selection_rule": "primary_keyword_then_highest_search_volume",
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


AI_SEARCH_FEATURES = {"ai_overview", "people_also_ask"}
AI_SEARCH_INTENTS = {"informational", "commercial"}


def _ai_search_evidence(run, topic, current_position):
    """Evaluate the handover's four-part AI-search opportunity rule."""
    primary_keyword = " ".join(topic.primary_keyword.lower().strip().split())
    observation_scope = KeywordObservation.objects.filter(
        run=run, market=topic.market
    )
    primary_observations = list(
        observation_scope.filter(keyword=topic.primary_keyword).order_by("pk")
    )
    if not primary_observations:
        primary_observations = list(
            observation_scope.filter(
                keyword_normalised=primary_keyword
            ).order_by("pk")
        )

    # Prefer an observation that actually carries SERP evidence; otherwise use
    # the first primary-keyword observation for its intent/position evidence.
    primary_observation = next(
        (observation for observation in primary_observations if observation.serp_features),
        primary_observations[0] if primary_observations else None,
    )
    serp_features = sorted({
        str(feature).lower()
        for observation in primary_observations
        for feature in (observation.serp_features or [])
    })
    qualifying_features = sorted(AI_SEARCH_FEATURES.intersection(serp_features))
    has_ai_serp_feature = bool(qualifying_features)

    observation_intent = (
        (primary_observation.intent or "").lower()
        if primary_observation else ""
    )
    intent = observation_intent or (topic.intent or "").lower()
    has_eligible_intent = intent in AI_SEARCH_INTENTS

    # Per the agreed deterministic proxy: qualifying AI/PAA SERP evidence on
    # an eligible query indicates structured answerability.
    structured_answerable = has_ai_serp_feature and has_eligible_intent

    observed_positions = [
        observation.our_position
        for observation in primary_observations
        if observation.our_position is not None
    ]
    best_observation_position = min(observed_positions) if observed_positions else None
    authority_positions = [
        value for value in (best_observation_position, current_position)
        if value is not None
    ]
    best_authority_position = min(authority_positions) if authority_positions else None
    has_topical_authority = (
        best_authority_position is not None and best_authority_position <= 20
    )

    qualifies = all((
        has_ai_serp_feature,
        has_eligible_intent,
        structured_answerable,
        has_topical_authority,
    ))
    return qualifies, {
        "qualifies": qualifies,
        "primary_keyword": topic.primary_keyword,
        "primary_observation_id": primary_observation.pk if primary_observation else None,
        "serp_features": serp_features,
        "qualifying_features": qualifying_features,
        "has_ai_serp_feature": has_ai_serp_feature,
        "intent": intent,
        "has_eligible_intent": has_eligible_intent,
        "structured_answerable": structured_answerable,
        "observation_position": best_observation_position,
        "current_position": current_position,
        "best_authority_position": best_authority_position,
        "has_topical_authority": has_topical_authority,
    }


def run_stage_decide(run, match_results=None):
    topics = Topic.objects.filter(last_seen_run=run).select_related("market")
    total_topics = topics.count()
    if total_topics == 0:
        raise RuntimeError(f"Run #{run.pk} has no Topics. Did Stage 4 (CLUSTER) run first?")
    match_results = match_results or {}
    Opportunity.objects.filter(run=run).delete()
    counts = {"new_content": 0, "optimise": 0, "merge": 0, "ignore": 0}

    for topic in topics.iterator():
        limits = _settings(run, topic.market.code)
        primary = topic.keywords.filter(is_primary=True).first() or topic.keywords.order_by("-search_volume").first()
        if primary is None:
            continue
        keywords = list(topic.keywords.values_list("keyword", flat=True))
        observations = list(KeywordObservation.objects.filter(
            run=run, market=topic.market, keyword__in=keywords
        ))
        signals = sorted({observation.signal for observation in observations})
        competitor_url, competitor_url_trace = _best_competitor_url(
            topic, observations
        )
        difficulty_score = primary.keyword_difficulty
        difficulty_label = _get_difficulty_label(difficulty_score)
        match = match_results.get(topic.pk, {})
        target_urls = list(match.get("matched_urls") or ([match["matched_url"]] if match.get("matched_url") else []))
        position = match.get("current_position")
        previous_position = match.get("previous_position")
        has_decay = "ranking_decay" in signals
        ai_search_opportunity, ai_search_trace = _ai_search_evidence(
            run, topic, position
        )

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
        estimated_impact, impact_trace = _estimated_impact(
            topic, action, position, conversion_rate, limits
        )
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
            "ai_search_opportunity": ai_search_trace,
            "estimated_impact": impact_trace,
            "competitor_url": competitor_url_trace,
        }
        Opportunity.objects.create(
            run=run, topic=topic, market=topic.market, action=action,
            target_urls=target_urls, why_flagged=signals,
            current_position=position, previous_position=previous_position,
            difficulty=difficulty_label,
            difficulty_score=difficulty_score, page_type=_suggest_page_type(topic.intent, topic.total_search_volume),
            suggested_slug=_suggest_slug(topic.primary_keyword),
            conversion_potential=conversion_potential, conversion_basis=conversion_basis,
            ai_search_opportunity=ai_search_opportunity,
            estimated_impact=estimated_impact,
            competitor_url=competitor_url,
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

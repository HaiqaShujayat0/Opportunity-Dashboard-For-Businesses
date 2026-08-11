"""Stage 8: deterministic six-factor priority scoring with graceful GA4 degradation."""

import logging
import math

from django.utils import timezone

from apps.clients.models import ScoringWeights
from apps.opportunities.models import Opportunity
from apps.runs.models import RunStage

logger = logging.getLogger(__name__)

DEFAULT_SIGNAL_WEIGHTS = {
    "ranking_decay": 1.0,
    "quick_win": 0.95,
    "conversion_proven": 0.9,
    "competitor_gap": 0.8,
    "cross_market": 0.75,
    "keyword_research": 0.6,
    "existing_ranking": 0.7,
}


def _log_volume(volume, max_volume):
    if volume <= 0 or max_volume <= 0:
        return 0.0
    return min(1.0, math.log1p(volume) / math.log1p(max_volume))


def _difficulty(score):
    return 0.5 if score is None else max(0.0, min(1.0, (100 - score) / 100))


def _position(position):
    if position is None:
        return 0.5
    if position <= 3:
        return 0.1
    if position <= 10:
        return 1.0
    if position <= 20:
        return 0.85
    if position <= 50:
        return 0.45
    return 0.15


def _conversion(opportunity):
    rate = opportunity.decision_trace.get("conversion_rate")
    if opportunity.conversion_basis == "data" and rate is not None:
        return min(1.0, max(0.0, float(rate) / 0.05))
    return None


def _signal(signals, configured):
    weights = {**DEFAULT_SIGNAL_WEIGHTS, **(configured or {})}
    return max((float(weights.get(signal, 0.5)) for signal in signals), default=0.5)


def _weighted_score(components, weights):
    available = {name: value for name, value in components.items() if value is not None}
    total_weight = sum(weights[name] for name in available)
    if total_weight <= 0:
        return 0.0
    return sum(available[name] * weights[name] for name in available) / total_weight


def run_stage_score(run):
    opportunities = Opportunity.objects.filter(run=run).select_related("topic", "market")
    total = opportunities.count()
    if total == 0:
        raise RuntimeError(f"Run #{run.pk} has no Opportunities. Did Stage 6 (DECIDE) run first?")
    weights, _ = ScoringWeights.objects.get_or_create(client=run.client)
    max_volume = max(opportunity.topic.total_search_volume for opportunity in opportunities)
    weight_map = {
        "volume": weights.w_volume,
        "position": weights.w_position_opportunity,
        "conversion": weights.w_conversion,
        "difficulty": weights.w_difficulty,
        "signal": weights.w_signal,
        "market": weights.w_market,
    }
    scored = ignored = 0
    for opportunity in opportunities.iterator():
        if opportunity.action == "ignore":
            opportunity.priority_score = 0.0
            opportunity.confidence = 1.0
            opportunity.save(update_fields=["priority_score", "confidence"])
            ignored += 1
            continue
        market_score = float(weights.market_weights.get(opportunity.market.code, 1.0))
        components = {
            "volume": _log_volume(opportunity.topic.total_search_volume, max_volume),
            "position": _position(opportunity.current_position),
            "conversion": _conversion(opportunity),
            "difficulty": _difficulty(opportunity.difficulty_score),
            "signal": _signal(opportunity.why_flagged, weights.signal_weights),
            "market": max(0.0, min(1.0, market_score)),
        }
        final = round(100 * _weighted_score(components, weight_map), 1)
        source = opportunity.decision_trace.get("match_source")
        confidence = 0.9 if source == "gsc_ranking" else 0.8 if source == "slug" else 0.7
        trace = dict(opportunity.decision_trace)
        trace["scoring"] = {
            "components": components,
            "weights": weight_map,
            "missing_conversion_weight_redistributed": components["conversion"] is None,
        }
        opportunity.priority_score = final
        opportunity.confidence = confidence
        opportunity.decision_trace = trace
        opportunity.save(update_fields=["priority_score", "confidence", "decision_trace"])
        scored += 1

    RunStage.objects.update_or_create(
        run=run, name="score",
        defaults={
            "status": "complete", "records_in": total, "records_out": scored,
            "started_at": timezone.now(), "finished_at": timezone.now(), "error": "",
        },
    )
    return {"total_opportunities": total, "scored": scored, "ignored": ignored}

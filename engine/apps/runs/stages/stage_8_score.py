"""
Stage 8 -- SCORE

What it does (in plain English):
  This stage looks at all the Opportunities created in Stage 6 and gives
  each one a "Priority Score" out of 100.

  WHY this matters:
  A client doesn't just want a list of 500 things to do. They want to
  know what to do FIRST. This scoring system bubbles the biggest, easiest,
  most profitable wins to the very top of the list.

  How the score is calculated:
  The score is a weighted average of:
  - Search Volume (more volume = higher score)
  - Keyword Difficulty (easier = higher score)
  - Conversion Potential (transactional = higher score)
  - Signals (e.g., Quick Wins are worth more than general research)
"""
import logging
from django.utils import timezone

from apps.opportunities.models import Opportunity
from apps.clients.models import ScoringWeights
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)


def _normalize_volume(volume, max_volume):
    """Normalize volume to a 0.0 - 1.0 scale."""
    if not max_volume or volume <= 0:
        return 0.0
    return min(1.0, volume / max_volume)


def _normalize_difficulty(difficulty_score):
    """
    Normalize difficulty to a 0.0 - 1.0 scale where EASIER is better.
    A score of 10 (easy) becomes 0.9. A score of 90 (hard) becomes 0.1.
    """
    if difficulty_score is None:
        return 0.5  # neutral fallback
    return max(0.0, (100 - difficulty_score) / 100.0)


def _score_conversion_potential(potential):
    """Assign a 0.0 - 1.0 score based on conversion potential."""
    if potential == "High":
        return 1.0
    elif potential == "Medium":
        return 0.5
    return 0.1


def _score_signals(signals, signal_weights):
    """
    Assign a 0.0 - 1.0 score based on the highest value signal present.
    If no weights are defined, default to 0.5.
    """
    if not signals or not signal_weights:
        return 0.5
    
    max_val = 0.0
    for s in signals:
        val = signal_weights.get(s, 0.5)
        if val > max_val:
            max_val = val
    return max_val


def run_stage_score(run):
    """
    Execute Stage 8 -- SCORE.

    Assigns a priority_score (0-100) to each Opportunity based on
    the client's custom ScoringWeights.

    Returns:
        Summary dict with counts.
    """
    logger.info(f"[Stage 8 -- SCORE] Starting for Run #{run.pk}")

    opportunities = Opportunity.objects.filter(run=run)
    total_opps = opportunities.count()

    if total_opps == 0:
        raise RuntimeError(
            f"Run #{run.pk} has no Opportunities. Did Stage 6 (DECIDE) run first?"
        )

    # Fetch scoring weights for this client, or use defaults if missing
    try:
        weights = ScoringWeights.objects.get(client=run.client)
    except ScoringWeights.DoesNotExist:
        weights = ScoringWeights.objects.create(client=run.client)

    # Find the maximum volume in this run to normalize against
    max_vol_opp = opportunities.order_by("-topic__total_search_volume").first()
    max_volume = max_vol_opp.topic.total_search_volume if max_vol_opp else 1

    scored_count = 0
    ignored_count = 0

    for opp in opportunities.iterator():
        if opp.action == "ignore":
            opp.priority_score = 0.0
            opp.confidence = 1.0
            opp.save(update_fields=["priority_score", "confidence"])
            ignored_count += 1
            continue

        # 1. Normalize individual factors (0.0 to 1.0)
        vol_score = _normalize_volume(
            opp.topic.total_search_volume, max_volume
        )
        diff_score = _normalize_difficulty(
            opp.difficulty_score
        )
        conv_score = _score_conversion_potential(
            opp.conversion_potential
        )
        sig_score = _score_signals(
            opp.why_flagged, weights.signal_weights
        )

        # 2. Apply weights
        raw_score = (
            (vol_score * weights.w_volume) +
            (diff_score * weights.w_difficulty) +
            (conv_score * weights.w_conversion) +
            (sig_score * weights.w_signal)
        )

        # 3. Market modifier (if defined)
        market_mod = weights.market_weights.get(opp.market.code, 1.0)
        raw_score *= market_mod

        # 4. Convert to 0-100 scale and clamp
        final_score = round(min(100.0, max(0.0, raw_score * 100)), 1)
        
        # 5. Set confidence (placeholder for now)
        confidence = 0.8

        opp.priority_score = final_score
        opp.confidence = confidence
        opp.save(update_fields=["priority_score", "confidence"])
        scored_count += 1

    logger.info(
        f"[Stage 8 -- SCORE] Done. "
        f"Scored {scored_count} opportunities. "
        f"Ignored {ignored_count}."
    )

    # Record stage completion
    RunStage.objects.update_or_create(
        run=run,
        name="score",
        defaults={
            "status": "complete",
            "records_in": total_opps,
            "records_out": scored_count,
            "started_at": timezone.now(),
            "finished_at": timezone.now(),
        },
    )

    return {
        "total_opportunities": total_opps,
        "scored": scored_count,
        "ignored": ignored_count,
    }

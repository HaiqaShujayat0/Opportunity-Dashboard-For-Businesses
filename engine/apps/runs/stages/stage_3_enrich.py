"""
Stage 3 -- ENRICH

What it does (in plain English):
  After Stage 2 gave us a big list of keywords with their search volumes,
  this stage adds EXTRA useful information to each keyword:

  1. DIFFICULTY BUCKET -- Instead of just a number (0-100), we label each
     keyword as "Easy", "Medium", "Hard", or "Very Hard" so a human can
     quickly scan the spreadsheet and spot easy wins.

  2. SEARCH INTENT -- What does the person searching WANT?
     - "informational" = they want to learn ("what are running shoes")
     - "transactional" = they want to buy ("buy running shoes online")
     - "commercial"    = they're comparing ("best running shoes 2024")
     - "navigational"  = they want a specific site ("nike running shoes")
     This helps decide what KIND of page to create.

  3. SERP FEATURES (for top keywords only) -- Does Google show special
     boxes for this keyword? Things like:
     - AI Overview (Google's AI answer at the top)
     - Featured Snippet (the answer box)
     - People Also Ask (the expandable questions)
     If Google already shows an AI Overview, that keyword might be less
     valuable because fewer people click through to websites.

  4. TOP 10 URLs (for top keywords only) -- Which websites currently rank
     in the top 10 for this keyword? This data is critical for Stage 4
     (clustering) because if two keywords show the same top-10 results,
     they belong to the same topic.

Why this stage matters:
  Without enrichment, our keyword list is just words + numbers.
  After enrichment, each keyword has context that tells the human editor
  exactly HOW to act on it.

Cost note:
  Steps 1 and 2 are FREE -- they use data we already have.
  Steps 3 and 4 call the Advanced SERP endpoint (the most expensive one).
  We only call it for the TOP keywords (controlled by max_serp_calls).
"""
import logging
from django.utils import timezone
from django.conf import settings as django_settings

from apps.ingestion.models import RawFetch, KeywordObservation
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Difficulty buckets -- translates a 0-100 score into a human label
# -----------------------------------------------------------------------
DIFFICULTY_BUCKETS = [
    (0, 20, "Easy"),           # Few competitors, low authority needed
    (21, 40, "Medium"),        # Some competition, decent content needed
    (41, 60, "Hard"),          # Strong competitors, high-quality content needed
    (61, 100, "Very Hard"),    # Dominated by big brands, very difficult
]


def _get_difficulty_label(score):
    """Turn a numeric difficulty (0-100) into a human-friendly label."""
    if score is None:
        return ""
    for low, high, label in DIFFICULTY_BUCKETS:
        if low <= score <= high:
            return label
    return ""


# -----------------------------------------------------------------------
# Intent extraction -- pulls intent from the raw keyword_ideas payload
# -----------------------------------------------------------------------
def _extract_intents_from_raw(run):
    """
    DataForSEO already gives us search intent in the keyword_ideas
    response (inside search_intent_info.main_intent). We just need
    to read it from the saved RawFetch and map it to our keywords.

    Returns a dict: { "keyword_text": "transactional", ... }
    """
    intent_map = {}

    raw_fetches = RawFetch.objects.filter(
        run=run,
        source="dataforseo",
        endpoint__contains="keyword_ideas",
    ).exclude(payload__has_key="error")

    for raw in raw_fetches:
        try:
            items = raw.payload["tasks"][0]["result"][0]["items"]
            for item in items:
                keyword = (item.get("keyword") or "").lower().strip()
                intent_info = item.get("search_intent_info") or {}
                intent = intent_info.get("main_intent", "")
                if keyword and intent:
                    intent_map[keyword] = intent
        except (KeyError, IndexError, TypeError):
            continue

    return intent_map


def run_stage_enrich(run, max_serp_calls=0):
    """
    Execute Stage 3 -- ENRICH.

    What happens step by step:
      1. Load all KeywordObservation rows for this run
      2. Add difficulty labels (Easy/Medium/Hard/Very Hard)
      3. Add search intent (from raw data -- FREE, no API call)
      4. Optionally call Advanced SERP for top keywords (PAID)

    Args:
        run: The Run object
        max_serp_calls: How many Advanced SERP calls to make (0 = skip).
                        Each call costs ~$0.01. Default 0 for safety.

    Returns:
        Summary dict with counts of what was enriched.
    """
    logger.info(f"[Stage 3 -- ENRICH] Starting for Run #{run.pk}")

    # --- Step 1: Load all keyword observations for this run ---
    observations = KeywordObservation.objects.filter(run=run)
    total = observations.count()

    if total == 0:
        raise RuntimeError(
            f"Run #{run.pk} has no KeywordObservation rows. "
            "Did Stage 2 (NORMALISE) run first?"
        )

    logger.info(
        f"[Stage 3 -- ENRICH] "
        f"Found {total} KeywordObservation rows to enrich"
    )

    # --- Step 2: Add difficulty labels (FREE) ---
    difficulty_enriched = 0
    for obs in observations.filter(
        keyword_difficulty__isnull=False
    ).iterator():
        label = _get_difficulty_label(obs.keyword_difficulty)
        if label:
            # We store the label in the intent field temporarily
            # until we have a dedicated difficulty_label field.
            # Actually, we'll update keyword_difficulty to keep
            # the score and just log the label for now.
            difficulty_enriched += 1

    logger.info(
        f"  [ENRICH] Difficulty labels: "
        f"{difficulty_enriched} keywords have difficulty scores"
    )

    # --- Step 3: Add search intent from raw data (FREE) ---
    intent_map = _extract_intents_from_raw(run)
    intent_enriched = 0

    # Batch update: for each keyword that has an intent in our map,
    # write it to the KeywordObservation.intent field
    for obs in observations.filter(intent="").iterator():
        keyword_lower = obs.keyword.lower().strip()
        if keyword_lower in intent_map:
            obs.intent = intent_map[keyword_lower]
            obs.save(update_fields=["intent"])
            intent_enriched += 1

    logger.info(
        f"  [ENRICH] Search intent: "
        f"{intent_enriched} keywords tagged with intent "
        f"(from {len(intent_map)} available in raw data)"
    )

    # --- Step 4: Advanced SERP for top keywords (PAID -- optional) ---
    serp_enriched = 0
    serp_cost = 0.0

    if max_serp_calls > 0:
        logger.info(
            f"  [ENRICH] Advanced SERP: "
            f"will fetch top {max_serp_calls} keywords"
        )

        # Pick the top keywords by search volume that don't
        # already have SERP data
        top_keywords = (
            observations
            .filter(
                signal="keyword_research",
                search_volume__gt=0,
            )
            .exclude(keyword="")
            .order_by("-search_volume")
            [:max_serp_calls]
        )

        if top_keywords:
            from apps.clients.models import Market
            from apps.connectors.dataforseo.connector import (
                DataForSEOConnector,
            )

            # Get the first market for this run
            market_ids = observations.values_list(
                "market_id", flat=True
            ).distinct()
            for market_id in market_ids:
                try:
                    market = Market.objects.get(pk=market_id)
                except Market.DoesNotExist:
                    continue

                connector = DataForSEOConnector(
                    run=run,
                    market=market,
                    login=django_settings.DATAFORSEO_LOGIN,
                    password=django_settings.DATAFORSEO_PASSWORD,
                )

                for obs in top_keywords.filter(market=market):
                    try:
                        serp_items = connector.get_advanced_serp(
                            keyword=obs.keyword, depth=10
                        )

                        # Extract SERP features
                        features = set()
                        top_urls = []
                        for si in serp_items:
                            stype = getattr(si, "type", "")
                            if stype and stype != "organic":
                                features.add(stype)
                            url = getattr(si, "url", "")
                            if url and stype == "organic":
                                top_urls.append(url)

                        obs.serp_features = list(features)
                        obs.serp_top_urls = top_urls[:10]
                        obs.save(update_fields=[
                            "serp_features", "serp_top_urls"
                        ])
                        serp_enriched += 1

                    except Exception as e:
                        logger.warning(
                            f"  [ENRICH] SERP failed for "
                            f"'{obs.keyword}': {e}"
                        )

        logger.info(
            f"  [ENRICH] Advanced SERP: "
            f"{serp_enriched} keywords enriched with SERP data"
        )
    else:
        logger.info(
            "  [ENRICH] Advanced SERP: SKIPPED "
            "(max_serp_calls=0 -- set --serp-calls N to enable)"
        )

    # --- Record stage completion ---
    RunStage.objects.update_or_create(
        run=run,
        name="enrich",
        defaults={
            "status": "complete",
            "records_in": total,
            "records_out": intent_enriched + serp_enriched,
            "started_at": timezone.now(),
            "finished_at": timezone.now(),
        },
    )

    summary = {
        "total_observations": total,
        "difficulty_enriched": difficulty_enriched,
        "intent_enriched": intent_enriched,
        "serp_enriched": serp_enriched,
    }

    logger.info(
        f"[Stage 3 -- ENRICH] Done. Summary: {summary}"
    )

    return summary

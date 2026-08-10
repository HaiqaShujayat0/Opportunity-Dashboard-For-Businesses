"""
Stage 6 -- DECIDE

What it does (in plain English):
  Now that we know:
    - What topics exist (Stage 4)
    - Whether Nike already has a page for each topic (Stage 5)

  This stage makes the ACTUAL RECOMMENDATION. For each topic, it decides:

  1. ACTION — What should Nike do?
     - "new_content"  → Nike has NO page for this. Build one!
     - "optimise"     → Nike HAS a page but it could be better.
     - "ignore"       → This topic is too hard or too small to bother.

  2. PAGE TYPE — What kind of page should it be?
     - "category_page" → A product listing (e.g., nike.com/running-shoes)
     - "blog_post"     → An article (e.g., "Best Running Shoes for 2024")
     - "landing_page"  → A focused sales page
     - "product_page"  → A single product detail page

  3. SUGGESTED SLUG — What URL should the new page use?
     (e.g., /running-shoes-for-men)

  4. WHY FLAGGED — Which signals triggered this recommendation?
     (e.g., ["keyword_research", "competitor_gap"])

  5. DIFFICULTY — Easy / Medium / Hard / Very Hard

  The output of this stage is ONE Opportunity row per Topic.
  This is what eventually becomes one row in the client's Google Sheet.
"""
import logging
from django.utils import timezone

from apps.topics.models import Topic, TopicKeyword
from apps.ingestion.models import KeywordObservation
from apps.opportunities.models import Opportunity
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Difficulty buckets (same as Stage 3)
# -----------------------------------------------------------------------
DIFFICULTY_BUCKETS = [
    (0, 20, "Easy"),
    (21, 40, "Medium"),
    (41, 60, "Hard"),
    (61, 100, "Very Hard"),
]


def _get_difficulty_label(score):
    if score is None:
        return "Unknown"
    for low, high, label in DIFFICULTY_BUCKETS:
        if low <= score <= high:
            return label
    return "Unknown"


def _suggest_page_type(intent, search_volume):
    """
    Decide what kind of page to build based on the user's intent.

    Simple rules:
    - Transactional intent → category page (they want to buy)
    - Commercial intent    → blog post (they're comparing options)
    - Informational intent → blog post (they want to learn)
    - Navigational intent  → landing page (they want a specific brand)
    """
    if intent == "transactional":
        return "category_page"
    elif intent == "commercial":
        if search_volume > 5000:
            return "category_page"
        return "blog_post"
    elif intent == "informational":
        return "blog_post"
    elif intent == "navigational":
        return "landing_page"
    else:
        # Default: if volume is high, category page; otherwise blog
        if search_volume > 10000:
            return "category_page"
        return "blog_post"


def _suggest_slug(primary_keyword):
    """
    Turn a keyword into a URL-friendly slug.
    "best running shoes for men" → "/best-running-shoes-for-men"
    """
    slug = primary_keyword.lower().strip()
    slug = slug.replace(" ", "-")
    # Remove non-alphanumeric chars except hyphens
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    # Remove double hyphens
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"/{slug}"


def run_stage_decide(run, match_results=None):
    """
    Execute Stage 6 -- DECIDE.

    For each Topic, creates an Opportunity row with:
    - action (new_content / optimise / ignore)
    - page_type
    - suggested_slug
    - why_flagged signals
    - difficulty label

    Args:
        run: The Run object
        match_results: Dict from Stage 5 mapping topic_id -> match info.
                       If None, assumes no matches (all new_content).

    Returns:
        Summary dict with counts.
    """
    logger.info(f"[Stage 6 -- DECIDE] Starting for Run #{run.pk}")

    # Stable topics may have been created in an earlier run and reconciled.
    topics = Topic.objects.filter(last_seen_run=run)
    total_topics = topics.count()

    if total_topics == 0:
        raise RuntimeError(
            f"Run #{run.pk} has no Topics. Did Stage 4 (CLUSTER) run first?"
        )

    if match_results is None:
        match_results = {}

    # Clear old opportunities for this run (idempotent)
    old_count = Opportunity.objects.filter(run=run).count()
    if old_count > 0:
        Opportunity.objects.filter(run=run).delete()
        logger.info(
            f"  [DECIDE] Cleared {old_count} old opportunities"
        )

    new_content_count = 0
    optimise_count = 0
    ignore_count = 0

    for topic in topics.iterator():
        # Get the primary keyword data
        primary_tk = TopicKeyword.objects.filter(
            topic=topic, is_primary=True
        ).first()
        if not primary_tk:
            primary_tk = TopicKeyword.objects.filter(
                topic=topic
            ).order_by("-search_volume").first()

        if not primary_tk:
            continue

        # Collect all signals for this topic's keywords
        topic_keywords = TopicKeyword.objects.filter(
            topic=topic
        ).values_list("keyword", flat=True)

        signals = set()
        observations = KeywordObservation.objects.filter(
            run=run,
            keyword__in=list(topic_keywords),
        )
        for obs in observations.iterator():
            signals.add(obs.signal)

        # Get difficulty score
        difficulty_score = primary_tk.keyword_difficulty
        difficulty_label = _get_difficulty_label(difficulty_score)

        # --- DECISION LOGIC ---
        match_info = match_results.get(topic.pk, {})
        has_existing_page = match_info.get("matched", False)
        matched_url = match_info.get("matched_url", "")

        # Rule 1: If volume is tiny AND difficulty is very hard → ignore
        if topic.total_search_volume < 50 and difficulty_label == "Very Hard":
            action = "ignore"
            ignore_count += 1
        # Rule 2: If we already have a page → optimise
        elif has_existing_page:
            action = "optimise"
            optimise_count += 1
        # Rule 3: Everything else → new content
        else:
            action = "new_content"
            new_content_count += 1

        # Determine page type and slug
        page_type = _suggest_page_type(
            topic.intent, topic.total_search_volume
        )
        suggested_slug = _suggest_slug(topic.primary_keyword)

        # Build target_urls list
        target_urls = []
        if matched_url:
            target_urls = [matched_url]

        # Determine conversion potential (inferred without GA4)
        if topic.intent == "transactional":
            conversion_potential = "High"
        elif topic.intent == "commercial":
            conversion_potential = "Medium"
        else:
            conversion_potential = "Low"

        # Build decision trace (audit log of why we decided this)
        decision_trace = {
            "action_reason": (
                f"Volume={topic.total_search_volume}, "
                f"Difficulty={difficulty_label}, "
                f"HasPage={has_existing_page}, "
                f"Intent={topic.intent}"
            ),
            "signals": list(signals),
            "match_url": matched_url,
        }

        # Create the Opportunity row
        Opportunity.objects.create(
            run=run,
            topic=topic,
            market=topic.market,
            action=action,
            target_urls=target_urls,
            why_flagged=sorted(list(signals)),
            difficulty=difficulty_label,
            difficulty_score=difficulty_score,
            page_type=page_type,
            suggested_slug=suggested_slug,
            conversion_potential=conversion_potential,
            conversion_basis="inferred",
            decision_trace=decision_trace,
        )

    logger.info(
        f"[Stage 6 -- DECIDE] Done. "
        f"New={new_content_count}, Optimise={optimise_count}, "
        f"Ignore={ignore_count}"
    )

    # Record stage completion
    RunStage.objects.update_or_create(
        run=run,
        name="decide",
        defaults={
            "status": "complete",
            "records_in": total_topics,
            "records_out": new_content_count + optimise_count,
            "started_at": timezone.now(),
            "finished_at": timezone.now(),
        },
    )

    return {
        "total_topics": total_topics,
        "new_content": new_content_count,
        "optimise": optimise_count,
        "ignore": ignore_count,
        "opportunities_created": (
            new_content_count + optimise_count + ignore_count
        ),
    }

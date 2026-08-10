"""
Stage 5 -- MATCH

What it does (in plain English):
  After Stage 4 gave us a neat list of Topics (e.g., "Running Shoes",
  "Gym Clothes", "Cycling Shoes"), this stage asks one simple question
  for each Topic:

  "Does Nike ALREADY have a page on their website for this topic?"

  If YES → the action will be "Optimise" (improve the existing page)
  If NO  → the action will be "New Content" (build a new page)

  HOW it checks:
  Right now (Phase 2), we don't have GSC or a sitemap crawler yet.
  So we use a simpler method: we check the Relevant Pages data that
  DataForSEO already gave us in Stage 1. If one of Nike's existing
  pages mentions this keyword, we consider it a match.

  In Phase 4 (when we get GSC access), this stage will get much smarter
  by checking actual Google ranking data and sitemap URLs.
"""
import logging
from django.utils import timezone

from apps.topics.models import Topic, TopicKeyword
from apps.pages.models import ExistingPage
from apps.ingestion.models import KeywordObservation
from apps.runs.models import Run, RunStage

logger = logging.getLogger(__name__)


def run_stage_match(run):
    """
    Execute Stage 5 -- MATCH.

    For each Topic in this run, try to find an existing page on the
    client's website that already covers this topic.

    Returns:
        Summary dict with match counts.
    """
    logger.info(f"[Stage 5 -- MATCH] Starting for Run #{run.pk}")

    # Reconciled topics retain their original first_seen_run. last_seen_run is
    # the correct membership test for the current run.
    topics = Topic.objects.filter(last_seen_run=run)
    total_topics = topics.count()

    if total_topics == 0:
        raise RuntimeError(
            f"Run #{run.pk} has no Topics. Did Stage 4 (CLUSTER) run first?"
        )

    # --- Method 1: Check ExistingPage table (from sitemap/GSC) ---
    # This will be empty until Phase 4, but the code is ready.
    existing_pages = ExistingPage.objects.filter(
        market__client=run.client
    )
    existing_paths = set(
        ep.path.lower() for ep in existing_pages.iterator()
    )

    # --- Method 2: Check competitor_top_page observations ---
    # If DataForSEO showed us that the CLIENT's own pages rank for
    # these keywords, we know a page exists.
    our_ranking_keywords = set()
    client_observations = KeywordObservation.objects.filter(
        run=run,
        our_url__gt="",  # has a URL filled in
    )
    for obs in client_observations.iterator():
        our_ranking_keywords.add(obs.keyword.lower().strip())

    matched = 0
    unmatched = 0
    match_results = {}

    for topic in topics.iterator():
        # Get all keywords in this topic
        topic_keywords = TopicKeyword.objects.filter(
            topic=topic
        ).values_list("keyword", flat=True)

        found_match = False
        matched_url = ""

        # Check 1: Does any keyword appear in our existing pages?
        for kw in topic_keywords:
            kw_lower = kw.lower().strip()
            # Simple substring match against page paths
            for path in existing_paths:
                if kw_lower.replace(" ", "-") in path or \
                   kw_lower.replace(" ", "") in path:
                    found_match = True
                    matched_url = path
                    break
            if found_match:
                break

        # Check 2: Does our site already rank for any keyword?
        if not found_match:
            for kw in topic_keywords:
                if kw.lower().strip() in our_ranking_keywords:
                    found_match = True
                    # Get the URL we rank with
                    obs = KeywordObservation.objects.filter(
                        run=run,
                        keyword__iexact=kw,
                        our_url__gt="",
                    ).first()
                    if obs:
                        matched_url = obs.our_url
                    break

        match_results[topic.pk] = {
            "matched": found_match,
            "matched_url": matched_url,
        }

        if found_match:
            matched += 1
        else:
            unmatched += 1

    logger.info(
        f"[Stage 5 -- MATCH] Done. "
        f"Matched: {matched}, Unmatched: {unmatched}"
    )

    # Record stage completion
    RunStage.objects.update_or_create(
        run=run,
        name="match",
        defaults={
            "status": "complete",
            "records_in": total_topics,
            "records_out": matched,
            "started_at": timezone.now(),
            "finished_at": timezone.now(),
        },
    )

    return {
        "total_topics": total_topics,
        "matched": matched,
        "unmatched": unmatched,
        "match_results": match_results,
    }

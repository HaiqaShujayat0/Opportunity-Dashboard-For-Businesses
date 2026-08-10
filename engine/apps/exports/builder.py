"""Build flat, presentation-ready rows from pipeline opportunities."""

from apps.opportunities.models import Opportunity


EXPORT_COLUMNS = [
    "Topic",
    "Primary Keyword",
    "Search Volume",
    "Difficulty",
    "Intent",
    "Action",
    "Page Type",
    "Suggested URL",
    "Priority Score",
    "Why Flagged",
]


def _suggested_url(opportunity):
    """Prefer a known target page; otherwise return the proposed new slug."""
    if opportunity.target_urls:
        return opportunity.target_urls[0]
    return opportunity.suggested_slug or ""


def build_opportunity_rows(run):
    """
    Return all opportunities for ``run`` as a flat list of dictionaries.

    Rows are ordered by priority score (highest first), then topic and primary
    key for deterministic CSV and future Google Sheets output.
    """
    opportunities = (
        Opportunity.objects.filter(run=run)
        .select_related("topic")
        .order_by("-priority_score", "topic__label", "pk")
    )

    return [
        {
            "Topic": opportunity.topic.label,
            "Primary Keyword": opportunity.topic.primary_keyword,
            "Search Volume": opportunity.topic.total_search_volume,
            "Difficulty": opportunity.difficulty or "Unknown",
            "Intent": opportunity.topic.intent or "Unknown",
            "Action": opportunity.get_action_display(),
            "Page Type": opportunity.page_type or "",
            "Suggested URL": _suggested_url(opportunity),
            "Priority Score": (
                opportunity.priority_score
                if opportunity.priority_score is not None
                else ""
            ),
            "Why Flagged": ", ".join(opportunity.why_flagged or []),
        }
        for opportunity in opportunities
    ]

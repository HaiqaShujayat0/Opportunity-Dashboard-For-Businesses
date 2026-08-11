"""Stage 9: export a run to Google Sheets without losing human edits."""

from __future__ import annotations

import json
import re

from django.conf import settings
from django.utils import timezone

from apps.connectors.sheets import SheetsConnector
from apps.opportunities.models import Opportunity
from apps.runs.models import RunStage


TAB_NAMES = [
    "Opportunities",
    "Ignored",
    "Cannibalisation",
    "Run log",
    "Reference",
    "Archived",
]

# Exactly 19 engine-owned columns. Any column to the right is human-owned and
# is carried forward byte-for-byte by merge-on-write.
OPPORTUNITY_COLUMNS = [
    "Topic",
    "Market",
    "Category",
    "Primary Keyword",
    "Secondary Keywords",
    "Total Search Volume",
    "Current Position",
    "Previous Position",
    "Action",
    "Target URL",
    "Why Flagged",
    "Difficulty",
    "Page Type",
    "Suggested Slug",
    "Conversion Potential",
    "Competitor URL",
    "AI Search Opportunity",
    "Priority Score",
    "topic_uid",
]
ENGINE_COLUMN_COUNT = len(OPPORTUNITY_COLUMNS)

IGNORED_COLUMNS = OPPORTUNITY_COLUMNS + ["Decision Reason"]
CANNIBALISATION_COLUMNS = [
    "Topic",
    "Market",
    "Primary Keyword",
    "Affected URL",
    "Priority Score",
    "Reason",
    "topic_uid",
]
RUN_LOG_COLUMNS = [
    "Run ID",
    "Run Date",
    "Client",
    "Settings Snapshot",
    "Total Cost USD",
    "Opportunities",
    "Ignored",
    "Cannibalisation URLs",
    "Total Rows",
]
ARCHIVE_METADATA_COLUMNS = ["Archived Reason", "Archived At"]

REFERENCE_ROWS = [
    ["Column", "Definition"],
    ["Topic", "Plain-language label for the stable topic cluster."],
    ["Market", "Client market code for this recommendation."],
    ["Category", "Topic or matched-page category when available."],
    ["Primary Keyword", "Highest-volume keyword in the topic."],
    ["Secondary Keywords", "Other topic keywords, ordered by volume."],
    ["Total Search Volume", "Deduplicated sum across topic keywords."],
    ["Current Position", "Current measured search position, if available."],
    ["Previous Position", "Earlier measured position used for decay detection."],
    ["Action", "Engine recommendation: New Content or Optimise."],
    ["Target URL", "Existing page target(s); blank for new content."],
    ["Why Flagged", "Ordered discovery and performance signals."],
    ["Difficulty", "DataForSEO difficulty bucket."],
    ["Page Type", "Recommended content/page format."],
    ["Suggested Slug", "Proposed URL path for the recommendation."],
    ["Conversion Potential", "High/Medium/Low commercial potential."],
    ["Competitor URL", "Best known competitor result, when available."],
    ["AI Search Opportunity", "Whether available evidence supports AI-search targeting."],
    ["Priority Score", "Deterministic 0-100 work-queue score."],
    ["topic_uid", "Hidden stable key used to preserve human edits across runs."],
    ["Human columns", "Any columns from column 20 onward are never changed by the engine."],
]


def _sheet_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _opportunity_row(opportunity):
    secondary = [
        f"{keyword.keyword} ({keyword.search_volume:,})"
        for keyword in opportunity.topic.keywords.all()
        if not keyword.is_primary
    ]
    return [
        opportunity.topic.label,
        opportunity.market.code,
        opportunity.topic.category or "",
        opportunity.topic.primary_keyword,
        ", ".join(secondary),
        opportunity.topic.total_search_volume,
        _sheet_value(opportunity.current_position),
        _sheet_value(opportunity.previous_position),
        opportunity.get_action_display(),
        "\n".join(opportunity.target_urls or []),
        ", ".join(opportunity.why_flagged or []),
        opportunity.difficulty or "Unknown",
        opportunity.page_type or "",
        opportunity.suggested_slug or "",
        opportunity.conversion_potential or "",
        opportunity.competitor_url or "",
        _sheet_value(opportunity.ai_search_opportunity),
        _sheet_value(opportunity.priority_score),
        opportunity.topic.topic_uid,
    ]


def _decision_reason(opportunity):
    trace = opportunity.decision_trace or {}
    return trace.get("rule") or trace.get("reason") or json.dumps(
        trace, ensure_ascii=False, sort_keys=True
    )


def _spreadsheet_id(run, explicit_id=None):
    if explicit_id:
        return explicit_id
    snapshot = run.settings_snapshot or {}
    configured = (
        snapshot.get("google_sheets_spreadsheet_id")
        or snapshot.get("spreadsheet_id")
        or getattr(settings, "GOOGLE_SHEETS_SPREADSHEET_ID", "")
    )
    if configured:
        return str(configured)
    if run.sheet_url:
        match = re.search(r"/spreadsheets/d/([^/]+)", run.sheet_url)
        if match:
            return match.group(1)
    if getattr(settings, "USE_DUMMY_SHEETS", True):
        return f"dummy-{run.client.slug}"
    raise RuntimeError(
        "No Google spreadsheet ID configured. Set GOOGLE_SHEETS_SPREADSHEET_ID, "
        "put spreadsheet_id in the Run settings snapshot, or populate Run.sheet_url."
    )


def _remap_rows(rows, destination_headers):
    """Map existing archival data into a widened header without losing values."""
    if not rows:
        return []
    source_headers = list(rows[0])
    remapped = []
    for row in rows[1:]:
        values = {
            header: row[index] if index < len(row) else ""
            for index, header in enumerate(source_headers)
        }
        remapped.append([values.get(header, "") for header in destination_headers])
    return remapped


def _merge_actionable_rows(incoming_rows, existing_rows, existing_headers):
    human_headers = list(existing_headers[ENGINE_COLUMN_COUNT:])
    merged = []
    incoming_uids = set()
    for engine_values in incoming_rows:
        uid = str(engine_values[-1])
        incoming_uids.add(uid)
        old_values = existing_rows.get(uid, [])
        human_values = list(old_values[ENGINE_COLUMN_COUNT:])
        if len(human_values) < len(human_headers):
            human_values.extend([""] * (len(human_headers) - len(human_values)))
        merged.append(engine_values + human_values)
    stale = {
        uid: row for uid, row in existing_rows.items() if uid not in incoming_uids
    }
    return [OPPORTUNITY_COLUMNS + human_headers, *merged], stale, human_headers


def _archive_rows(existing_archive, stale_rows, existing_headers, human_headers, now):
    archive_headers = list(existing_archive[0]) if existing_archive else []
    previous_human_headers = [
        header
        for header in archive_headers[ENGINE_COLUMN_COUNT:]
        if header not in ARCHIVE_METADATA_COLUMNS
    ]
    all_human_headers = list(dict.fromkeys(human_headers + previous_human_headers))
    output_headers = OPPORTUNITY_COLUMNS + all_human_headers + ARCHIVE_METADATA_COLUMNS
    output = [output_headers, *_remap_rows(existing_archive, output_headers)]

    for row in stale_rows.values():
        source = {
            header: row[index] if index < len(row) else ""
            for index, header in enumerate(existing_headers)
        }
        archived = [source.get(header, "") for header in OPPORTUNITY_COLUMNS]
        archived.extend(source.get(header, "") for header in all_human_headers)
        archived.extend(["Not present in the latest actionable run", now])
        output.append(archived)
    return output


def run_stage_export(run, spreadsheet_id=None, connector=None):
    """Export all opportunities for ``run`` and return tab/merge counts."""
    stage, _ = RunStage.objects.update_or_create(
        run=run,
        name="export",
        defaults={
            "status": "running",
            "records_in": 0,
            "records_out": 0,
            "started_at": timezone.now(),
            "finished_at": None,
            "error": "",
        },
    )
    try:
        opportunities = list(
            Opportunity.objects.filter(run=run)
            .select_related("topic", "market")
            .prefetch_related("topic__keywords")
            .order_by("-priority_score", "topic__label", "pk")
        )
        if not opportunities:
            raise RuntimeError(
                f"Run #{run.pk} has no Opportunities. Did Stage 6 (DECIDE) run first?"
            )

        connector = connector or SheetsConnector()
        sheet_id = _spreadsheet_id(run, spreadsheet_id)
        existing = connector.read_existing_sheet(sheet_id)
        existing_headers = connector.existing_headers or OPPORTUNITY_COLUMNS
        existing_archive = connector.read_tab(sheet_id, "Archived")
        existing_run_log = connector.read_tab(sheet_id, "Run log")
        connector.create_tabs(sheet_id, TAB_NAMES)

        actionable = [
            opportunity
            for opportunity in opportunities
            if opportunity.action in {"new_content", "optimise"}
        ]
        ignored = [
            opportunity for opportunity in opportunities if opportunity.action == "ignore"
        ]
        merges = [
            opportunity for opportunity in opportunities if opportunity.action == "merge"
        ]

        actionable_rows, stale_rows, human_headers = _merge_actionable_rows(
            [_opportunity_row(opportunity) for opportunity in actionable],
            existing,
            existing_headers,
        )
        now = timezone.now().isoformat()
        archived_rows = _archive_rows(
            existing_archive, stale_rows, existing_headers, human_headers, now
        )
        ignored_rows = [
            IGNORED_COLUMNS,
            *[
                _opportunity_row(opportunity) + [_decision_reason(opportunity)]
                for opportunity in ignored
            ],
        ]
        cannibalisation_rows = [CANNIBALISATION_COLUMNS]
        for opportunity in merges:
            for url in opportunity.target_urls or []:
                cannibalisation_rows.append(
                    [
                        opportunity.topic.label,
                        opportunity.market.code,
                        opportunity.topic.primary_keyword,
                        url,
                        _sheet_value(opportunity.priority_score),
                        _decision_reason(opportunity),
                        opportunity.topic.topic_uid,
                    ]
                )

        log_rows = existing_run_log or [RUN_LOG_COLUMNS]
        if log_rows[0] != RUN_LOG_COLUMNS:
            log_rows = [RUN_LOG_COLUMNS]
        log_rows.append(
            [
                run.pk,
                run.created_at.isoformat(),
                run.client.name,
                json.dumps(run.settings_snapshot or {}, ensure_ascii=False, sort_keys=True),
                str(run.total_cost_usd),
                len(actionable),
                len(ignored),
                len(cannibalisation_rows) - 1,
                len(opportunities),
            ]
        )

        connector.write_batch(
            sheet_id,
            {
                "Opportunities": actionable_rows,
                "Ignored": ignored_rows,
                "Cannibalisation": cannibalisation_rows,
                "Run log": log_rows,
                "Reference": REFERENCE_ROWS,
                "Archived": archived_rows,
            },
        )

        if not getattr(settings, "USE_DUMMY_SHEETS", True):
            run.sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
            run.save(update_fields=["sheet_url"])

        summary = {
            "spreadsheet_id": sheet_id,
            "opportunities": len(actionable),
            "ignored": len(ignored),
            "cannibalisation_rows": len(cannibalisation_rows) - 1,
            "archived": len(stale_rows),
            "total_opportunities": len(opportunities),
        }
        stage.status = "complete"
        stage.records_in = len(opportunities)
        stage.records_out = (
            len(actionable) + len(ignored) + len(cannibalisation_rows) - 1
        )
        stage.finished_at = timezone.now()
        stage.error = ""
        stage.save(
            update_fields=[
                "status",
                "records_in",
                "records_out",
                "finished_at",
                "error",
            ]
        )
        return summary
    except Exception as exc:
        stage.status = "failed"
        stage.finished_at = timezone.now()
        stage.error = str(exc)
        stage.save(update_fields=["status", "finished_at", "error"])
        raise

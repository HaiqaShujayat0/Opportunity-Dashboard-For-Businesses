"""Canonical tabular exports shared by Google Sheets, CSV, and XLSX."""

from __future__ import annotations

import csv
import json
from io import BytesIO, StringIO

from django.utils import timezone

from apps.opportunities.models import Opportunity
from apps.runs.models import Run


TAB_NAMES = [
    "Opportunities",
    "Ignored",
    "Cannibalisation",
    "Run log",
    "Reference",
    "Archived",
]

# Exactly 19 engine-owned columns. Google Sheets columns to the right are
# human-owned and are carried forward only by Stage 9 merge-on-write.
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
    ["AI Search Opportunity", "Whether evidence supports AI-search targeting."],
    ["Priority Score", "Deterministic 0-100 work-queue score."],
    ["topic_uid", "Hidden stable key used to preserve human edits across runs."],
    ["Human columns", "Columns 20 onward exist only in the live Google Sheet."],
]


def _resolve_run(run_or_id):
    if isinstance(run_or_id, Run):
        return run_or_id
    return Run.objects.select_related("client").get(pk=run_or_id)


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


def _remap_rows(rows, destination_headers):
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


def build_export_tabs(
    run_or_id,
    *,
    existing_rows=None,
    existing_headers=None,
    existing_archive=None,
    existing_run_log=None,
    generated_at=None,
):
    """Build the canonical six delivery tabs from one Run.

    Existing Google Sheet values are optional. When omitted for an on-demand
    XLSX download, Archived contains headers only because human-owned values do
    not exist in the database.
    """
    run = _resolve_run(run_or_id)
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
    existing_rows = existing_rows or {}
    existing_headers = existing_headers or OPPORTUNITY_COLUMNS
    existing_archive = existing_archive or []
    existing_run_log = existing_run_log or []

    actionable_rows, stale_rows, human_headers = _merge_actionable_rows(
        [_opportunity_row(opportunity) for opportunity in actionable],
        existing_rows,
        existing_headers,
    )
    now = generated_at or timezone.now().isoformat()
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

    log_rows = list(existing_run_log) if existing_run_log else [RUN_LOG_COLUMNS]
    if not log_rows or log_rows[0] != RUN_LOG_COLUMNS:
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

    tabs = {
        "Opportunities": actionable_rows,
        "Ignored": ignored_rows,
        "Cannibalisation": cannibalisation_rows,
        "Run log": log_rows,
        "Reference": REFERENCE_ROWS,
        "Archived": archived_rows,
    }
    stats = {
        "opportunities": len(actionable),
        "ignored": len(ignored),
        "cannibalisation_rows": len(cannibalisation_rows) - 1,
        "archived": len(stale_rows),
        "total_opportunities": len(opportunities),
    }
    return tabs, stats


def _safe_download_value(value):
    """Prevent spreadsheet formula execution in user-controlled text cells."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def generate_csv(run_or_id):
    """Return an Excel-friendly UTF-8 CSV for the main Opportunities table only."""
    tabs, _ = build_export_tabs(run_or_id)
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    for row in tabs["Opportunities"]:
        writer.writerow([_safe_download_value(value) for value in row])
    return output.getvalue().encode("utf-8-sig")


def generate_excel(run_or_id):
    """Return an in-memory six-sheet XLSX snapshot for one Run."""
    import xlsxwriter

    run = _resolve_run(run_or_id)
    tabs, _ = build_export_tabs(run)
    output = BytesIO()
    workbook = xlsxwriter.Workbook(
        output,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )
    workbook.set_properties(
        {
            "title": f"{run.client.name} - SEO Opportunities - Run {run.pk}",
            "subject": "Engine 1 on-demand export",
            "author": "Engine 1",
        }
    )
    header_format = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#1F4E78",
            "border": 1,
            "text_wrap": True,
        }
    )
    body_format = workbook.add_format({"text_wrap": True, "valign": "top"})

    for tab_name in TAB_NAMES:
        rows = tabs[tab_name]
        worksheet = workbook.add_worksheet(tab_name)
        worksheet.freeze_panes(1, 0)
        for row_index, row in enumerate(rows):
            values = [_safe_download_value(value) for value in row]
            worksheet.write_row(
                row_index,
                0,
                values,
                header_format if row_index == 0 else body_format,
            )
        if rows and rows[0]:
            worksheet.autofilter(0, 0, max(0, len(rows) - 1), len(rows[0]) - 1)
            for column_index, header in enumerate(rows[0]):
                sample_width = max(
                    [len(str(header))]
                    + [
                        len(str(row[column_index]))
                        for row in rows[1:51]
                        if column_index < len(row)
                    ]
                )
                worksheet.set_column(
                    column_index,
                    column_index,
                    min(max(sample_width + 2, 12), 45),
                )
        if tab_name == "Archived":
            worksheet.write_comment(
                0,
                0,
                "Archived human notes live only in Google Sheets and are not "
                "available in this database-only XLSX snapshot.",
            )

    workbook.close()
    return output.getvalue()

"""Backward-compatible dictionary view of the canonical Opportunities tab."""

from apps.runs.exporters import OPPORTUNITY_COLUMNS, build_export_tabs


EXPORT_COLUMNS = OPPORTUNITY_COLUMNS


def build_opportunity_rows(run):
    """
    Return the main Opportunities table (new content + optimise only).

    This compatibility API now uses the same 19 engine-owned columns as Google
    Sheets, CSV downloads, and XLSX downloads.
    """
    rows, _ = build_export_tabs(run)
    return [dict(zip(EXPORT_COLUMNS, row)) for row in rows["Opportunities"][1:]]

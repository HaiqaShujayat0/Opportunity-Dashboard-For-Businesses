"""Google Sheets connector with a deterministic local JSON transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings


class SheetsConnectorError(RuntimeError):
    """Raised when a Sheets export cannot be configured or completed."""


class SheetsConnector:
    """
    Read and batch-write spreadsheet tabs.

    Dummy mode stores the same tab-shaped row matrices used by the live API in
    a JSON file. This keeps merge-on-write tests completely offline.
    """

    opportunities_tab = "Opportunities"

    def __init__(self, use_dummy=None, fixture_path=None, credentials_file=None):
        self.use_dummy = (
            getattr(settings, "USE_DUMMY_SHEETS", True)
            if use_dummy is None
            else use_dummy
        )
        configured_fixture = getattr(
            settings,
            "SHEETS_MOCK_FILE",
            settings.BASE_DIR / "tests" / "fixtures" / "sheets_mock.json",
        )
        self.fixture_path = Path(fixture_path or configured_fixture)
        self.credentials_file = credentials_file or getattr(
            settings, "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE", ""
        )
        self._spreadsheet_cache = {}
        self._tab_cache = {}
        self.existing_headers = []

    def _dummy_document(self) -> dict[str, Any]:
        if not self.fixture_path.exists():
            return {"spreadsheets": {}}
        try:
            payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetsConnectorError(
                f"Could not read Sheets fixture {self.fixture_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise SheetsConnectorError("Sheets fixture root must be a JSON object.")
        return payload

    @staticmethod
    def _dummy_tabs(document: dict[str, Any], spreadsheet_id: str, create=False):
        """Support both a multi-spreadsheet document and a simple tab mapping."""
        if "spreadsheets" in document:
            spreadsheets = document.setdefault("spreadsheets", {})
            if create:
                return spreadsheets.setdefault(str(spreadsheet_id), {})
            return spreadsheets.get(str(spreadsheet_id), {})
        return document

    def _save_dummy(self, document: dict[str, Any]):
        self.fixture_path.parent.mkdir(parents=True, exist_ok=True)
        self.fixture_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _spreadsheet(self, spreadsheet_id):
        if not spreadsheet_id:
            raise SheetsConnectorError("A Google Sheets spreadsheet ID is required.")
        if spreadsheet_id in self._spreadsheet_cache:
            return self._spreadsheet_cache[spreadsheet_id]
        if not self.credentials_file:
            raise SheetsConnectorError(
                "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE is required when "
                "USE_DUMMY_SHEETS=False."
            )
        try:
            import gspread

            client = gspread.service_account(filename=str(self.credentials_file))
            spreadsheet = client.open_by_key(spreadsheet_id)
        except Exception as exc:
            raise SheetsConnectorError(f"Could not open Google Sheet: {exc}") from exc
        self._spreadsheet_cache[spreadsheet_id] = spreadsheet
        return spreadsheet

    def read_tab(self, spreadsheet_id, tab_name):
        """Return a tab as a list of row lists, or an empty list if absent."""
        if self.use_dummy:
            document = self._dummy_document()
            rows = self._dummy_tabs(document, spreadsheet_id).get(tab_name, [])
            result = [list(row) for row in rows]
            self._tab_cache[(str(spreadsheet_id), tab_name)] = result
            return result
        spreadsheet = self._spreadsheet(spreadsheet_id)
        try:
            result = spreadsheet.worksheet(tab_name).get_all_values()
            self._tab_cache[(str(spreadsheet_id), tab_name)] = result
            return result
        except Exception as exc:
            # gspread's exception type is intentionally not imported in dummy mode.
            if exc.__class__.__name__ == "WorksheetNotFound":
                return []
            raise SheetsConnectorError(f"Could not read tab {tab_name!r}: {exc}") from exc

    def read_existing_sheet(self, spreadsheet_id):
        """Return Opportunities rows keyed by the stable hidden ``topic_uid``."""
        rows = self.read_tab(spreadsheet_id, self.opportunities_tab)
        if not rows:
            self.existing_headers = []
            return {}
        self.existing_headers = list(rows[0])
        normalised_headers = [str(value).strip().lower() for value in rows[0]]
        try:
            uid_index = normalised_headers.index("topic_uid")
        except ValueError:
            raise SheetsConnectorError(
                "The existing Opportunities tab has no topic_uid column; "
                "refusing an unsafe export that could detach human notes."
            )
        existing = {}
        for row in rows[1:]:
            if uid_index >= len(row):
                continue
            uid = str(row[uid_index]).strip()
            if uid:
                if uid in existing:
                    raise SheetsConnectorError(
                        f"The Opportunities tab contains duplicate topic_uid {uid!r}."
                    )
                existing[uid] = list(row)
        return existing

    def create_tabs(self, spreadsheet_id, tab_names):
        """Create all missing tabs, using one structural request in live mode."""
        names = list(dict.fromkeys(tab_names))
        if self.use_dummy:
            document = self._dummy_document()
            tabs = self._dummy_tabs(document, spreadsheet_id, create=True)
            for name in names:
                tabs.setdefault(name, [])
            self._save_dummy(document)
            return

        spreadsheet = self._spreadsheet(spreadsheet_id)
        existing = {worksheet.title for worksheet in spreadsheet.worksheets()}
        requests = [
            {"addSheet": {"properties": {"title": name}}}
            for name in names
            if name not in existing
        ]
        if requests:
            spreadsheet.batch_update({"requests": requests})

    def write_batch(self, spreadsheet_id, tab_data_dict):
        """Write every supplied tab in one Google Sheets batchUpdate call."""
        if self.use_dummy:
            document = self._dummy_document()
            tabs = self._dummy_tabs(document, spreadsheet_id, create=True)
            for tab_name, rows in tab_data_dict.items():
                tabs[tab_name] = [list(row) for row in rows]
            self._save_dummy(document)
            return

        spreadsheet = self._spreadsheet(spreadsheet_id)
        worksheets = {worksheet.title: worksheet for worksheet in spreadsheet.worksheets()}

        def cell(value):
            if isinstance(value, bool):
                entered = {"boolValue": value}
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                entered = {"numberValue": value}
            else:
                entered = {"stringValue": "" if value is None else str(value)}
            return {"userEnteredValue": entered}

        def grid_rows(rows, width=None):
            return [
                {"values": [cell(value) for value in list(row)[:width]]}
                for row in rows
            ]

        requests = []
        for tab_name, rows in tab_data_dict.items():
            if tab_name == self.opportunities_tab:
                continue
            worksheet = worksheets[tab_name]
            required_rows = max(1, len(rows))
            required_columns = max(1, max((len(row) for row in rows), default=1))
            row_count = max(worksheet.row_count, required_rows)
            column_count = max(worksheet.col_count, required_columns)
            if row_count != worksheet.row_count or column_count != worksheet.col_count:
                requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": worksheet.id,
                                "gridProperties": {
                                    "rowCount": row_count,
                                    "columnCount": column_count,
                                },
                            },
                            "fields": "gridProperties(rowCount,columnCount)",
                        }
                    }
                )
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": worksheet.id,
                            "startRowIndex": 0,
                            "endRowIndex": row_count,
                            "startColumnIndex": 0,
                            "endColumnIndex": column_count,
                        },
                        "rows": grid_rows(rows),
                        "fields": "userEnteredValue",
                    }
                }
            )

        opportunity_rows = [list(row) for row in tab_data_dict[self.opportunities_tab]]
        opportunity_sheet = worksheets[self.opportunities_tab]
        current_rows = self._tab_cache.get(
            (str(spreadsheet_id), self.opportunities_tab), []
        )
        if not current_rows:
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": opportunity_sheet.id,
                            "startRowIndex": 0,
                            "endRowIndex": max(opportunity_sheet.row_count, len(opportunity_rows)),
                            "startColumnIndex": 0,
                            "endColumnIndex": 19,
                        },
                        "rows": grid_rows(opportunity_rows, width=19),
                        "fields": "userEnteredValue",
                    }
                }
            )
        else:
            current_headers = [str(value).strip().lower() for value in current_rows[0]]
            current_uid_index = current_headers.index("topic_uid")
            current_positions = {
                str(row[current_uid_index]).strip(): index
                for index, row in enumerate(current_rows[1:], start=1)
                if current_uid_index < len(row) and str(row[current_uid_index]).strip()
            }
            desired = {str(row[18]): row for row in opportunity_rows[1:]}

            # Header and existing engine cells only: columns T onward are never sent.
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": opportunity_sheet.id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 19,
                        },
                        "rows": grid_rows([opportunity_rows[0]], width=19),
                        "fields": "userEnteredValue",
                    }
                }
            )
            for uid, row_index in current_positions.items():
                if uid not in desired:
                    continue
                requests.append(
                    {
                        "updateCells": {
                            "range": {
                                "sheetId": opportunity_sheet.id,
                                "startRowIndex": row_index,
                                "endRowIndex": row_index + 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": 19,
                            },
                            "rows": grid_rows([desired[uid]], width=19),
                            "fields": "userEnteredValue",
                        }
                    }
                )

            stale_indexes = sorted(
                (
                    row_index
                    for uid, row_index in current_positions.items()
                    if uid not in desired
                ),
                reverse=True,
            )
            for row_index in stale_indexes:
                requests.append(
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": opportunity_sheet.id,
                                "dimension": "ROWS",
                                "startIndex": row_index,
                                "endIndex": row_index + 1,
                            }
                        }
                    }
                )

            new_rows = [
                row for uid, row in desired.items() if uid not in current_positions
            ]
            if new_rows:
                requests.append(
                    {
                        "appendCells": {
                            "sheetId": opportunity_sheet.id,
                            "rows": grid_rows(new_rows, width=19),
                            "fields": "userEnteredValue",
                        }
                    }
                )
            if desired:
                requests.append(
                    {
                        "sortRange": {
                            "range": {
                                "sheetId": opportunity_sheet.id,
                                "startRowIndex": 1,
                                "endRowIndex": len(desired) + 1,
                            },
                            "sortSpecs": [
                                {"dimensionIndex": 17, "sortOrder": "DESCENDING"}
                            ],
                        }
                    }
                )

        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": opportunity_sheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": 18,
                        "endIndex": 19,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            }
        )
        try:
            spreadsheet.batch_update({"requests": requests})
        except Exception as exc:
            raise SheetsConnectorError(f"Google Sheets batch update failed: {exc}") from exc

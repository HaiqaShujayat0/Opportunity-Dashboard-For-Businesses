import csv
import json
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree

from django.contrib import admin
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings

from apps.clients.models import Client, Market
from apps.exports.builder import EXPORT_COLUMNS, build_opportunity_rows
from apps.opportunities.models import Opportunity
from apps.runs.models import Run
from apps.runs.admin import (
    RunAdmin,
    download_opportunities_csv,
    download_run_xlsx,
)
from apps.runs.exporters import TAB_NAMES, generate_csv, generate_excel
from apps.runs.stages.stage_0_plan import run_stage_plan
from apps.runs.stages.stage_9_export import (
    OPPORTUNITY_COLUMNS,
    _spreadsheet_id,
    run_stage_export,
)
from apps.topics.models import Topic


class OpportunityExportTests(TestCase):
    def setUp(self):
        client = Client.objects.create(
            name="Export Client", slug="export-client", primary_domain="example.com"
        )
        market = Market.objects.create(
            client=client,
            code="UK",
            country_iso="GB",
            language_code="en",
            dataforseo_location_code=2826,
            gsc_property="sc-domain:example.com",
            sitemap_url="https://example.com/sitemap.xml",
        )
        self.run = Run.objects.create(client=client)
        topic = Topic.objects.create(
            client=client,
            market=market,
            topic_uid="export-topic",
            label="Running Shoes",
            primary_keyword="running shoes",
            primary_keyword_volume=1000,
            total_search_volume=1500,
            intent="transactional",
            first_seen_run=self.run,
            last_seen_run=self.run,
        )
        Opportunity.objects.create(
            run=self.run,
            topic=topic,
            market=market,
            action="new_content",
            difficulty="Medium",
            page_type="category_page",
            suggested_slug="/running-shoes",
            priority_score=82.4,
            why_flagged=["competitor_gap", "keyword_research"],
        )
        ignored_topic = Topic.objects.create(
            client=client,
            market=market,
            topic_uid="ignored-export-topic",
            label="Already Winning",
            primary_keyword="already winning",
            primary_keyword_volume=500,
            total_search_volume=500,
            intent="commercial",
            first_seen_run=self.run,
            last_seen_run=self.run,
        )
        Opportunity.objects.create(
            run=self.run,
            topic=ignored_topic,
            market=market,
            action="ignore",
            priority_score=0,
            decision_trace={"rule": "already ranks top three"},
        )

    def test_builder_returns_requested_flat_columns(self):
        rows = build_opportunity_rows(self.run)
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows[0]), EXPORT_COLUMNS)
        self.assertEqual(rows[0]["Topic"], "Running Shoes")
        self.assertEqual(rows[0]["Action"], "New Content")
        self.assertEqual(rows[0]["Suggested Slug"], "/running-shoes")
        self.assertIn("Estimated Impact", rows[0])
        self.assertEqual(
            rows[0]["Why Flagged"], "competitor_gap, keyword_research"
        )

    def test_option_b_csv_contains_only_main_opportunities_table(self):
        rows = list(
            csv.DictReader(
                generate_csv(self.run).decode("utf-8-sig").splitlines()
            )
        )

        self.assertEqual(list(rows[0]), OPPORTUNITY_COLUMNS)
        self.assertEqual([row["Topic"] for row in rows], ["Running Shoes"])
        self.assertEqual(rows[0]["topic_uid"], "export-topic")

    def test_xlsx_contains_the_six_canonical_tabs(self):
        content = generate_excel(self.run)

        self.assertTrue(content.startswith(b"PK"))
        with zipfile.ZipFile(BytesIO(content)) as workbook_zip:
            workbook_xml = ElementTree.fromstring(
                workbook_zip.read("xl/workbook.xml")
            )
        namespace = {
            "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        }
        names = [
            sheet.attrib["name"]
            for sheet in workbook_xml.findall("main:sheets/main:sheet", namespace)
        ]
        self.assertEqual(names, TAB_NAMES)

    def test_export_run_writes_csv(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            output = Path(temp_directory) / "opportunities.csv"
            call_command(
                "export_run",
                run_id=self.run.pk,
                format="csv",
                output=str(output),
            )
            with output.open(encoding="utf-8-sig", newline="") as csv_file:
                rows = list(csv.DictReader(csv_file))

        self.assertEqual(rows[0]["Primary Keyword"], "running shoes")
        self.assertEqual(rows[0]["Priority Score"], "82.4")
        self.assertEqual(list(rows[0]), EXPORT_COLUMNS)

    def test_export_run_writes_xlsx(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            output = Path(temp_directory) / "run.xlsx"
            call_command(
                "export_run",
                run_id=self.run.pk,
                format="xlsx",
                output=str(output),
            )

            self.assertTrue(output.read_bytes().startswith(b"PK"))

    def test_admin_actions_return_download_responses_for_one_run(self):
        model_admin = RunAdmin(Run, admin.site)
        request = RequestFactory().post("/admin/runs/run/")
        queryset = Run.objects.filter(pk=self.run.pk)

        csv_response = download_opportunities_csv(
            model_admin, request, queryset
        )
        xlsx_response = download_run_xlsx(model_admin, request, queryset)

        self.assertEqual(csv_response.status_code, 200)
        self.assertTrue(csv_response.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn(
            f"run_{self.run.pk}_opportunities.csv",
            csv_response["Content-Disposition"],
        )
        self.assertEqual(xlsx_response.status_code, 200)
        self.assertTrue(xlsx_response.content.startswith(b"PK"))
        self.assertIn(
            f"run_{self.run.pk}_export.xlsx",
            xlsx_response["Content-Disposition"],
        )

    def test_admin_download_requires_exactly_one_run(self):
        second_run = Run.objects.create(client=self.run.client)
        model_admin = Mock()
        request = RequestFactory().post("/admin/runs/run/")

        response = download_opportunities_csv(
            model_admin,
            request,
            Run.objects.filter(pk__in=[self.run.pk, second_run.pk]),
        )

        self.assertIsNone(response)
        model_admin.message_user.assert_called_once()


class GoogleSheetsExportTests(TestCase):
    """Offline coverage for Stage 9's client-data safety contract."""

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.fixture_path = Path(self.temp_directory.name) / "sheets_mock.json"
        self.settings_override = override_settings(
            USE_DUMMY_SHEETS=True,
            SHEETS_MOCK_FILE=self.fixture_path,
        )
        self.settings_override.enable()
        self.client_record = Client.objects.create(
            name="Sheets Client", slug="sheets-client", primary_domain="example.com"
        )
        self.market = Market.objects.create(
            client=self.client_record,
            code="UK",
            country_iso="GB",
            language_code="en",
            dataforseo_location_code=2826,
            gsc_property="sc-domain:example.com",
            sitemap_url="https://example.com/sitemap.xml",
        )
        self.run = Run.objects.create(
            client=self.client_record,
            settings_snapshot={"engine_settings": {"min_search_volume": 50}},
            total_cost_usd="1.2500",
        )

    def tearDown(self):
        self.settings_override.disable()
        self.temp_directory.cleanup()

    @property
    def spreadsheet_id(self):
        return "dummy-sheets-client"

    def _opportunity(self, uid, action, score, urls=None, reason="test reason"):
        topic = Topic.objects.create(
            client=self.client_record,
            market=self.market,
            topic_uid=uid,
            label=uid.replace("-", " ").title(),
            primary_keyword=uid.replace("-", " "),
            primary_keyword_volume=100,
            total_search_volume=200,
            intent="commercial",
            first_seen_run=self.run,
            last_seen_run=self.run,
        )
        return Opportunity.objects.create(
            run=self.run,
            topic=topic,
            market=self.market,
            action=action,
            target_urls=urls or [],
            why_flagged=["quick_win"],
            difficulty="Medium",
            page_type="blog_post",
            suggested_slug=f"/{uid}",
            priority_score=score,
            decision_trace={"rule": reason},
        )

    def _seed_sheet(self, opportunities_rows, extra_tabs=None):
        tabs = {
            "Opportunities": opportunities_rows,
            **(extra_tabs or {}),
        }
        self.fixture_path.write_text(
            json.dumps({"spreadsheets": {self.spreadsheet_id: tabs}}),
            encoding="utf-8",
        )

    def _tabs(self):
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        return payload["spreadsheets"][self.spreadsheet_id]

    def test_stage_splits_rows_into_five_delivery_tabs(self):
        self._opportunity("uid-low", "new_content", 61)
        self._opportunity("uid-high", "optimise", 91, ["https://example.com/high"])
        self._opportunity("uid-ignore", "ignore", 0, reason="already ranks top three")
        self._opportunity(
            "uid-merge",
            "merge",
            72,
            ["https://example.com/a", "https://example.com/b"],
            reason="two pages overlap",
        )

        summary = run_stage_export(self.run)
        tabs = self._tabs()

        self.assertEqual(summary["opportunities"], 2)
        self.assertEqual(tabs["Opportunities"][1][-1], "uid-high")
        self.assertEqual(tabs["Opportunities"][2][-1], "uid-low")
        self.assertEqual(tabs["Ignored"][1][-1], "already ranks top three")
        self.assertEqual(len(tabs["Cannibalisation"]) - 1, 2)
        self.assertEqual(tabs["Run log"][1][2], "Sheets Client")
        self.assertGreater(len(tabs["Reference"]), 1)

    @override_settings(
        USE_DUMMY_SHEETS=False,
        GOOGLE_SHEETS_SPREADSHEET_ID="deployment-fallback",
    )
    def test_client_sheet_is_snapshotted_and_run_url_can_override_it(self):
        self.client_record.google_sheets_spreadsheet_id = "client-sheet-id"
        self.client_record.save(update_fields=["google_sheets_spreadsheet_id"])
        self.run.seed_keywords = "running shoes"
        self.run.markets = ["UK"]
        self.run.save(update_fields=["seed_keywords", "markets"])
        run_stage_plan(self.run)

        self.assertEqual(
            self.run.settings_snapshot["google_sheets_spreadsheet_id"],
            "client-sheet-id",
        )
        self.assertEqual(_spreadsheet_id(self.run), "client-sheet-id")

        self.run.sheet_url = (
            "https://docs.google.com/spreadsheets/d/run-specific-sheet-id/edit"
        )
        self.run.save(update_fields=["sheet_url"])
        self.assertEqual(_spreadsheet_id(self.run), "run-specific-sheet-id")
        self.assertEqual(
            _spreadsheet_id(self.run, explicit_id="command-line-sheet-id"),
            "command-line-sheet-id",
        )

    @override_settings(USE_DUMMY_SHEETS=False)
    def test_blank_client_sheet_is_provisioned_saved_and_reused(self):
        provisioner = Mock()
        provisioner.provision.return_value = "auto-created-sheet-id"

        result = _spreadsheet_id(self.run, provisioner=provisioner)

        self.client_record.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(result, "auto-created-sheet-id")
        self.assertEqual(
            self.client_record.google_sheets_spreadsheet_id,
            "auto-created-sheet-id",
        )
        self.assertEqual(
            self.run.settings_snapshot["google_sheets_spreadsheet_id"],
            "auto-created-sheet-id",
        )
        provisioner.provision.assert_called_once_with("Sheets Client")

        self.assertEqual(
            _spreadsheet_id(self.run, provisioner=provisioner),
            "auto-created-sheet-id",
        )
        provisioner.provision.assert_called_once()

    @override_settings(USE_DUMMY_SHEETS=False)
    def test_new_clients_receive_independent_spreadsheets(self):
        second_client = Client.objects.create(
            name="Second Client",
            slug="second-client",
            primary_domain="second.example.com",
        )
        second_run = Run.objects.create(client=second_client)
        provisioner = Mock()
        provisioner.provision.side_effect = ["first-sheet", "second-sheet"]

        first = _spreadsheet_id(self.run, provisioner=provisioner)
        second = _spreadsheet_id(second_run, provisioner=provisioner)

        self.assertEqual((first, second), ("first-sheet", "second-sheet"))
        saved = dict(
            Client.objects.filter(
                pk__in=[self.client_record.pk, second_client.pk]
            ).values_list("name", "google_sheets_spreadsheet_id")
        )
        self.assertEqual(saved["Sheets Client"], "first-sheet")
        self.assertEqual(saved["Second Client"], "second-sheet")

    @override_settings(USE_DUMMY_SHEETS=False)
    def test_provisioning_failure_leaves_client_unconfigured(self):
        provisioner = Mock()
        provisioner.provision.side_effect = RuntimeError("Drive unavailable")

        with self.assertRaisesMessage(RuntimeError, "Drive unavailable"):
            _spreadsheet_id(self.run, provisioner=provisioner)

        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.google_sheets_spreadsheet_id, "")

    def test_merge_updates_engine_columns_and_preserves_human_columns(self):
        opportunity = self._opportunity("uid-current", "optimise", 88)
        old_engine_values = ["Old engine value"] * len(OPPORTUNITY_COLUMNS)
        old_engine_values[-1] = opportunity.topic.topic_uid
        self._seed_sheet(
            [
                OPPORTUNITY_COLUMNS + ["Owner", "Editorial Notes"],
                old_engine_values + ["Amina", "Keep this client note"],
            ]
        )

        run_stage_export(self.run)
        row = self._tabs()["Opportunities"][1]

        self.assertEqual(row[0], "Uid Current")
        self.assertEqual(row[OPPORTUNITY_COLUMNS.index("Priority Score")], 88.0)
        self.assertEqual(
            row[len(OPPORTUNITY_COLUMNS):],
            ["Amina", "Keep this client note"],
        )

    def test_merge_preserves_human_columns_from_pre_impact_sheet_contract(self):
        opportunity = self._opportunity("uid-legacy", "optimise", 77)
        legacy_headers = [
            header for header in OPPORTUNITY_COLUMNS
            if header != "Estimated Impact"
        ]
        legacy_values = ["Old engine value"] * len(legacy_headers)
        legacy_values[legacy_headers.index("topic_uid")] = opportunity.topic.topic_uid
        self._seed_sheet([
            legacy_headers + ["Owner", "Status"],
            legacy_values + ["Amina", "In progress"],
        ])

        run_stage_export(self.run)
        row = self._tabs()["Opportunities"][1]

        self.assertEqual(
            row[len(OPPORTUNITY_COLUMNS):], ["Amina", "In progress"]
        )

    def test_missing_rows_are_moved_to_archived(self):
        self._opportunity("uid-current", "new_content", 80)
        current_old = ["Current old"] * len(OPPORTUNITY_COLUMNS)
        current_old[-1] = "uid-current"
        stale_old = ["Stale topic"] * len(OPPORTUNITY_COLUMNS)
        stale_old[-1] = "uid-stale"
        self._seed_sheet(
            [
                OPPORTUNITY_COLUMNS + ["Human Notes"],
                current_old + ["Current note"],
                stale_old + ["Archive me safely"],
            ]
        )

        summary = run_stage_export(self.run)
        tabs = self._tabs()
        opportunity_uids = {row[18] for row in tabs["Opportunities"][1:]}
        archived_header = tabs["Archived"][0]
        archived_uid_index = archived_header.index("topic_uid")
        archived_note_index = archived_header.index("Human Notes")
        archived = {
            row[archived_uid_index]: row for row in tabs["Archived"][1:]
        }

        self.assertEqual(summary["archived"], 1)
        self.assertNotIn("uid-stale", opportunity_uids)
        self.assertIn("uid-stale", archived)
        self.assertEqual(archived["uid-stale"][archived_note_index], "Archive me safely")

import gzip
from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from django.test import TestCase, override_settings

from apps.clients.models import Client, Market
from apps.connectors.sitemap import SitemapConnector, SitemapError
from apps.connectors.ga4 import GA4Connector, GA4ConnectorError
from apps.connectors.google_auth import (
    GA4_READONLY_SCOPE,
    GSC_READONLY_SCOPE,
    GoogleTransportError,
    ga4_live_executor,
    get_google_session,
    gsc_live_executor,
)
from apps.connectors.gsc import GSCConnector, GSCConnectorError
from apps.connectors.sheets.provisioner import (
    DRIVE_FILES_URL,
    DRIVE_SCOPE,
    DriveProvisioningError,
    DriveSpreadsheetProvisioner,
    SPREADSHEET_MIME_TYPE,
)
from apps.ingestion.models import RawFetch
from apps.pages.models import ExistingPage
from apps.runs.models import Run, RunStage
from apps.runs.stages.stage_1b_sitemap import run_stage_sitemap
from apps.runs.stages.stage_1c_google import run_stage_google_ingest


URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/shoes</loc><lastmod>2026-08-01</lastmod></url>
  <url><loc>https://example.com/clothing</loc></url>
</urlset>"""

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/products.xml</loc></sitemap>
  <sitemap><loc>https://example.com/products.xml.gz</loc></sitemap>
</sitemapindex>"""


class DriveSpreadsheetProvisionerTests(TestCase):
    @override_settings(
        GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE="C:/secure/sheets-key.json"
    )
    def test_drive_session_uses_sheets_key_and_drive_scope(self):
        authorized_session = Mock()
        with patch(
            "apps.connectors.sheets.provisioner.get_google_session",
            return_value=authorized_session,
        ) as session_factory:
            result = DriveSpreadsheetProvisioner()._session()

        self.assertIs(result, authorized_session)
        session_factory.assert_called_once_with(
            [DRIVE_SCOPE],
            credential_setting_name="GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE",
        )

    @override_settings(GOOGLE_API_TIMEOUT_SECONDS=45)
    def test_creates_shared_drive_sheet_then_shares_admin_as_editor(self):
        create_response = Mock(status_code=200)
        create_response.json.return_value = {"id": "new-sheet-id"}
        permission_response = Mock(status_code=200)
        permission_response.json.return_value = {"id": "permission-id"}
        session = Mock()
        session.request.side_effect = [create_response, permission_response]
        provisioner = DriveSpreadsheetProvisioner(
            session=session,
            parent_folder_id="shared-drive-folder",
            admin_email="admin@example.com",
        )

        result = provisioner.provision("Acme")

        self.assertEqual(result, "new-sheet-id")
        create_call, permission_call = session.request.call_args_list
        self.assertEqual(
            create_call.args[:2],
            ("POST", f"{DRIVE_FILES_URL}?supportsAllDrives=true&fields=id"),
        )
        self.assertEqual(
            create_call.kwargs,
            {
                "json": {
                    "name": "Acme - SEO Opportunities",
                    "mimeType": SPREADSHEET_MIME_TYPE,
                    "parents": ["shared-drive-folder"],
                },
                "timeout": 45,
            },
        )
        self.assertEqual(permission_call.args[0], "POST")
        self.assertIn("new-sheet-id/permissions", permission_call.args[1])
        self.assertEqual(
            permission_call.kwargs["json"],
            {
                "type": "user",
                "role": "writer",
                "emailAddress": "admin@example.com",
            },
        )

    def test_share_failure_attempts_cleanup_and_preserves_original_error(self):
        create_response = Mock(status_code=200)
        create_response.json.return_value = {"id": "orphan-sheet-id"}
        permission_response = Mock(status_code=403)
        permission_error = requests.HTTPError("private response")
        permission_error.response = permission_response
        permission_response.raise_for_status.side_effect = permission_error
        cleanup_response = Mock(status_code=204)
        session = Mock()
        session.request.side_effect = [
            create_response,
            permission_response,
            cleanup_response,
        ]
        provisioner = DriveSpreadsheetProvisioner(
            session=session,
            parent_folder_id="shared-drive-folder",
            admin_email="admin@example.com",
        )

        with self.assertRaisesMessage(
            DriveProvisioningError,
            "Google Drive spreadsheet sharing returned HTTP 403",
        ):
            provisioner.provision("Acme")

        cleanup_call = session.request.call_args_list[2]
        self.assertEqual(cleanup_call.args[0], "DELETE")
        self.assertIn("orphan-sheet-id", cleanup_call.args[1])


class FakeResponse:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class SitemapTests(TestCase):
    def setUp(self):
        client = Client.objects.create(
            name="Sitemap Client", slug="sitemap-client", primary_domain="example.com"
        )
        self.market = Market.objects.create(
            client=client, code="UK", country_iso="GB", language_code="en",
            dataforseo_location_code=2826, gsc_property="sc-domain:example.com",
            sitemap_url="https://example.com/sitemap.xml",
        )
        self.run = Run.objects.create(
            client=client,
            settings_snapshot={"markets": [{"market_id": self.market.pk, "market_code": "UK"}]},
        )

    def test_connector_reads_nested_and_gzip_sitemaps_and_logs_raw_fetches(self):
        session = Mock()
        responses = {
            "https://example.com/sitemap.xml": INDEX,
            "https://example.com/products.xml": URLSET,
            "https://example.com/products.xml.gz": gzip.compress(URLSET),
        }
        session.get.side_effect = lambda url, **kwargs: FakeResponse(responses[url])

        pages = SitemapConnector(self.run, self.market, session=session).fetch()

        self.assertEqual({page["url"] for page in pages}, {
            "https://example.com/shoes", "https://example.com/clothing"
        })
        self.assertEqual(len(pages), 2)  # duplicate URLs across child maps are removed
        self.assertEqual(RawFetch.objects.filter(run=self.run, source="sitemap").count(), 3)
        self.assertIsNotNone(next(page for page in pages if page["url"].endswith("shoes"))["last_modified"])

    def test_connector_rejects_malformed_xml_and_records_error(self):
        session = Mock()
        session.get.return_value = FakeResponse(b"<not-valid")
        with self.assertRaises(SitemapError):
            SitemapConnector(self.run, self.market, session=session).fetch()
        raw = RawFetch.objects.get(run=self.run, source="sitemap")
        self.assertIn("error", raw.payload)

    def test_stage_is_idempotent_and_marks_removed_pages_out_of_sitemap(self):
        class Connector:
            records = [
                {"url": "https://example.com/shoes", "last_modified": None},
                {"url": "https://example.com/clothing", "last_modified": None},
            ]

            def __init__(self, run, market):
                pass

            def fetch(self):
                return list(self.records)

        first = run_stage_sitemap(self.run, connector_class=Connector)
        second = run_stage_sitemap(self.run, connector_class=Connector)
        Connector.records = [{"url": "https://example.com/shoes", "last_modified": None}]
        third = run_stage_sitemap(self.run, connector_class=Connector)

        self.assertEqual(first["pages_created"], 2)
        self.assertEqual(second["pages_created"], 0)
        self.assertEqual(second["pages_updated"], 2)
        self.assertEqual(ExistingPage.objects.count(), 2)
        self.assertEqual(third["pages_marked_not_in_sitemap"], 1)
        self.assertTrue(ExistingPage.objects.get(url="https://example.com/shoes").in_sitemap)
        self.assertFalse(ExistingPage.objects.get(url="https://example.com/clothing").in_sitemap)

    def test_stage_records_complete_failure_without_removing_existing_pages(self):
        ExistingPage.objects.create(
            market=self.market, url="https://example.com/existing", path="/existing"
        )

        class FailingConnector:
            def __init__(self, run, market):
                pass

            def fetch(self):
                raise SitemapError("offline")

        with self.assertRaises(RuntimeError):
            run_stage_sitemap(self.run, connector_class=FailingConnector)

        stage = RunStage.objects.get(run=self.run, name="sitemap")
        self.assertEqual(stage.status, "failed")
        self.assertIn("offline", stage.error)
        self.assertTrue(ExistingPage.objects.get().in_sitemap)


class GoogleConnectorTests(TestCase):
    def setUp(self):
        client = Client.objects.create(
            name="Google Fixture Client", slug="google-fixture-client", primary_domain="example.com"
        )
        self.market = Market.objects.create(
            client=client, code="UK", country_iso="GB", language_code="en",
            dataforseo_location_code=2826, gsc_property="sc-domain:example.com",
            ga4_property_id="123456789", sitemap_url="https://example.com/sitemap.xml",
        )
        self.run = Run.objects.create(client=client)

    def test_gsc_fixture_is_validated_and_audited(self):
        rows = GSCConnector(self.run, self.market).fetch(
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 28)
        )

        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0].query, "running shoes")
        self.assertEqual(rows[0].observed_on, date(2026, 7, 15))
        raw = RawFetch.objects.get(run=self.run, source="gsc")
        self.assertEqual(raw.request_params["dimensions"], ["query", "page", "country", "date"])
        self.assertEqual(raw.request_params["dataState"], "final")
        self.assertEqual(
            raw.request_params["dimensionFilterGroups"][0]["filters"][0]["expression"],
            "gbr",
        )

    def test_gsc_paginates_until_a_short_page(self):
        calls = []

        def execute(params):
            calls.append(params["startRow"])
            if params["startRow"] == 0:
                return {"rows": [{
                    "keys": ["shoes", "https://example.com/shoes", "gbr", "2026-07-15"],
                    "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 5,
                }]}
            return {"rows": []}

        rows = GSCConnector(self.run, self.market, executor=execute, use_dummy=False).fetch(
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 28), row_limit=1
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(RawFetch.objects.filter(run=self.run, source="gsc").count(), 2)

    def test_gsc_rejects_wrong_dimension_shape_and_logs_error(self):
        connector = GSCConnector(
            self.run, self.market,
            executor=lambda params: {"rows": [{
                "keys": ["missing", "dimensions"],
                "clicks": 1, "impressions": 2, "ctr": 0.5, "position": 3,
            }]},
            use_dummy=False,
        )
        with self.assertRaises(GSCConnectorError):
            connector.fetch(start_date=date(2026, 7, 1), end_date=date(2026, 7, 28))
        self.assertIn("error", RawFetch.objects.get(source="gsc").payload)

    def test_ga4_fixture_maps_values_by_response_headers_and_audits(self):
        records = GA4Connector(self.run, self.market).fetch()

        self.assertEqual(len(records), 10)
        self.assertEqual(records[0], {
            "landingPagePlusQueryString": "/collections/running-shoes",
            "sessionDefaultChannelGroup": "Organic Search",
            "sessions": Decimal("1500"),
            "keyEvents": Decimal("45"),
            "purchaseRevenue": Decimal("6750.00"),
        })
        raw = RawFetch.objects.get(run=self.run, source="ga4")
        self.assertEqual(raw.request_params["property"], "properties/123456789")
        self.assertEqual(raw.request_params["metrics"][1]["name"], "keyEvents")

    def test_ga4_rejects_mismatched_row_width_and_logs_error(self):
        connector = GA4Connector(
            self.run, self.market,
            executor=lambda params: {
                "dimensionHeaders": [{"name": "landingPagePlusQueryString"}],
                "metricHeaders": [{"name": "sessions"}],
                "rows": [{"dimensionValues": [], "metricValues": [{"value": "1"}]}],
            },
            use_dummy=False,
        )
        with self.assertRaises(GA4ConnectorError):
            connector.fetch()
        self.assertIn("error", RawFetch.objects.get(source="ga4").payload)

    @override_settings(GOOGLE_API_SERVICE_ACCOUNT_FILE="C:/secure/google-key.json")
    def test_service_account_session_uses_configured_file_and_scopes(self):
        credentials = Mock()
        authorized_session = Mock()
        with (
            patch("apps.connectors.google_auth.Path.is_file", return_value=True),
            patch(
                "apps.connectors.google_auth.Credentials.from_service_account_file",
                return_value=credentials,
            ) as credential_loader,
            patch(
                "apps.connectors.google_auth.AuthorizedSession",
                return_value=authorized_session,
            ),
        ):
            result = get_google_session([GSC_READONLY_SCOPE])

        self.assertIs(result, authorized_session)
        credential_loader.assert_called_once_with(
            "C:\\secure\\google-key.json", scopes=[GSC_READONLY_SCOPE]
        )

    @override_settings(GOOGLE_API_TIMEOUT_SECONDS=45)
    def test_live_executors_build_exact_urls_without_mutating_params(self):
        response = Mock()
        response.json.side_effect = [{"rows": []}, {"rows": []}]
        session = Mock()
        session.post.return_value = response
        gsc_params = {
            "siteUrl": "sc-domain:example.com",
            "startDate": "2026-07-01",
            "endDate": "2026-07-28",
        }
        ga4_params = {
            "property": "properties/123456789",
            "dateRanges": [{"startDate": "28daysAgo", "endDate": "today"}],
        }
        original_gsc = dict(gsc_params)
        original_ga4 = dict(ga4_params)

        gsc_live_executor(gsc_params, session=session)
        ga4_live_executor(ga4_params, session=session)

        self.assertEqual(gsc_params, original_gsc)
        self.assertEqual(ga4_params, original_ga4)
        gsc_call, ga4_call = session.post.call_args_list
        self.assertEqual(
            gsc_call.args[0],
            "https://www.googleapis.com/webmasters/v3/sites/"
            "sc-domain%3Aexample.com/searchAnalytics/query",
        )
        self.assertEqual(
            gsc_call.kwargs,
            {
                "json": {"startDate": "2026-07-01", "endDate": "2026-07-28"},
                "timeout": 45,
            },
        )
        self.assertEqual(
            ga4_call.args[0],
            "https://analyticsdata.googleapis.com/v1beta/"
            "properties/123456789:runReport",
        )
        self.assertEqual(
            ga4_call.kwargs,
            {
                "json": {"dateRanges": ga4_params["dateRanges"]},
                "timeout": 45,
            },
        )
        self.assertNotIn("property", ga4_call.kwargs["json"])
        self.assertEqual(response.raise_for_status.call_count, 2)

    def test_live_http_errors_are_sanitized(self):
        response = Mock()
        http_error = requests.HTTPError("secret response body")
        http_error.response = Mock(status_code=403)
        response.raise_for_status.side_effect = http_error
        session = Mock()
        session.post.return_value = response

        with self.assertRaisesMessage(
            GoogleTransportError, "GSC API returned HTTP 403"
        ) as raised:
            gsc_live_executor({"siteUrl": "sc-domain:example.com"}, session=session)

        self.assertNotIn("secret response body", str(raised.exception))

    def test_live_cache_never_reuses_dummy_fixture_rows(self):
        date_range = {"start_date": date(2026, 7, 1), "end_date": date(2026, 7, 28)}
        GSCConnector(self.run, self.market, use_dummy=True).fetch(**date_range)
        live_executor = Mock(return_value={"rows": []})

        rows = GSCConnector(
            self.run, self.market, executor=live_executor, use_dummy=False
        ).fetch(**date_range)

        self.assertEqual(rows, [])
        live_executor.assert_called_once()
        self.assertEqual(RawFetch.objects.filter(source="gsc").count(), 2)

    @override_settings(GOOGLE_USE_DUMMY_DATA=False)
    def test_stage_injects_live_executors_when_dummy_mode_is_disabled(self):
        calls = []

        class RecordingConnector:
            def __init__(self, run, market, **kwargs):
                calls.append(kwargs)

            def fetch(self):
                return []

        self.run.settings_snapshot = {
            "markets": [{"market_id": self.market.pk, "market_code": "UK"}]
        }
        self.run.save(update_fields=["settings_snapshot"])

        summary = run_stage_google_ingest(
            self.run,
            gsc_connector_class=RecordingConnector,
            ga4_connector_class=RecordingConnector,
        )

        self.assertEqual(summary["stage_status"], "complete")
        self.assertEqual(calls[0], {"executor": gsc_live_executor, "use_dummy": False})
        self.assertEqual(calls[1], {"executor": ga4_live_executor, "use_dummy": False})

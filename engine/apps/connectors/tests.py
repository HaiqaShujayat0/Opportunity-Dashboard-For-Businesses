import gzip
from unittest.mock import Mock

import requests
from django.test import TestCase

from apps.clients.models import Client, Market
from apps.connectors.sitemap import SitemapConnector, SitemapError
from apps.ingestion.models import RawFetch
from apps.pages.models import ExistingPage
from apps.runs.models import Run, RunStage
from apps.runs.stages.stage_1b_sitemap import run_stage_sitemap


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

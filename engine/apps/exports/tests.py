import csv
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from apps.clients.models import Client, Market
from apps.exports.builder import EXPORT_COLUMNS, build_opportunity_rows
from apps.opportunities.models import Opportunity
from apps.runs.models import Run
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

    def test_builder_returns_requested_flat_columns(self):
        rows = build_opportunity_rows(self.run)
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows[0]), EXPORT_COLUMNS)
        self.assertEqual(rows[0]["Topic"], "Running Shoes")
        self.assertEqual(rows[0]["Action"], "New Content")
        self.assertEqual(rows[0]["Suggested URL"], "/running-shoes")
        self.assertEqual(
            rows[0]["Why Flagged"], "competitor_gap, keyword_research"
        )

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

import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase
from django.core.management import call_command

from apps.clients.models import (
    Client, Competitor, EngineSettings, Market, ScoringWeights,
)
from apps.connectors.ga4 import GA4Connector
from apps.connectors.gsc import GSCConnector
from apps.connectors.dataforseo.connector import (
    DataForSEOConnector, DataForSEOResponseError,
)
from apps.ingestion.models import KeywordObservation, RawFetch
from apps.pages.models import ExistingPage, PositionSnapshot
from apps.opportunities.models import Opportunity
from apps.runs.exporters import OPPORTUNITY_COLUMNS, build_export_tabs
from apps.runs.models import Run, RunStage
from apps.runs.stages.stage_0_plan import run_stage_plan
from apps.runs.stages.stage_1_ingest import run_stage_ingest
from apps.runs.stages.stage_2_normalise import PayloadStructureError, run_stage_normalise
from apps.runs.stages.stage_2b_analytics import run_stage_analytics
from apps.runs.stages.stage_4_cluster import run_stage_cluster
from apps.runs.stages.stage_5_match import run_stage_match
from apps.runs.stages.stage_6_decide import run_stage_decide
from apps.runs.stages.stage_8_score import run_stage_score
from apps.runs.tasks import run_pipeline_async
from apps.topics.models import Topic
from apps.topics.models import TopicKeyword


def response(items):
    return {"tasks": [{"result": [{"items": items}]}]}


class PerMarketSettingsSnapshotTests(TestCase):
    def test_plan_merges_default_and_market_override_by_market_code(self):
        client = Client.objects.create(
            name="Settings Client",
            slug="settings-client",
            primary_domain="example.com",
        )
        uk = Market.objects.create(
            client=client, code="UK", country_iso="GB", language_code="en",
            dataforseo_location_code=2826, sitemap_url="https://example.com/uk.xml",
        )
        de = Market.objects.create(
            client=client, code="DE", country_iso="DE", language_code="de",
            dataforseo_location_code=2276, sitemap_url="https://example.com/de.xml",
        )
        EngineSettings.objects.create(
            client=client,
            min_search_volume=50,
            max_keyword_difficulty=80,
            serp_overlap_threshold=4,
            semantic_similarity_threshold=0.75,
            category_extraction_regex=r"^/default/([^/]+)/",
        )
        EngineSettings.objects.create(
            client=client,
            market=de,
            min_search_volume=10,
            max_keyword_difficulty=60,
            serp_overlap_threshold=2,
            semantic_similarity_threshold=0.55,
            category_extraction_regex="",
        )
        run = Run.objects.create(
            client=client,
            markets=["UK", "DE"],
            seed_keywords="running shoes",
        )

        summary = run_stage_plan(run)

        run.refresh_from_db()
        settings = run.settings_snapshot["engine_settings"]
        self.assertEqual(set(settings), {"UK", "DE"})
        self.assertEqual(settings["UK"]["min_search_volume"], 50)
        self.assertEqual(settings["UK"]["max_keyword_difficulty"], 80)
        self.assertEqual(settings["UK"]["semantic_similarity_threshold"], 0.75)
        self.assertEqual(settings["DE"]["min_search_volume"], 10)
        self.assertEqual(settings["DE"]["max_keyword_difficulty"], 60)
        self.assertEqual(settings["DE"]["serp_overlap_threshold"], 2)
        self.assertEqual(settings["DE"]["semantic_similarity_threshold"], 0.55)
        self.assertEqual(
            settings["DE"]["category_extraction_regex"],
            r"^/default/([^/]+)/",
        )
        self.assertTrue(summary["has_engine_settings"])


class DataForSEOIngestionRegressionTests(TestCase):
    def setUp(self):
        client = Client.objects.create(
            name="DataForSEO Client",
            slug="dataforseo-client",
            primary_domain="example.com",
        )
        self.market = Market.objects.create(
            client=client,
            code="UK",
            country_iso="GB",
            language_code="en",
            dataforseo_location_code=2826,
            gsc_property="sc-domain:example.com",
            sitemap_url="https://example.com/sitemap.xml",
        )
        self.run = Run.objects.create(
            client=client,
            seed_keywords="home workout equipment reviews",
            settings_snapshot={
                "seed_keywords": ["home workout equipment reviews"],
                "markets": [{
                    "market_id": self.market.pk,
                    "market_code": self.market.code,
                    "competitors": [],
                }],
            },
        )

    @patch("apps.connectors.dataforseo.client.DataForSEOClient._post")
    def test_ingest_accepts_null_bulk_keyword_difficulty(self, api_post):
        competitor_discovery_response = response([])
        competitor_discovery_response["cost"] = 0.02
        api_post.side_effect = [
            response([{"keyword": "home workout equipment reviews"}]),
            response([{"keyword": "home gym reviews"}]),
            response([{"keyword_data": {
                "keyword": "workout equipment guide",
                "keyword_info": {"search_volume": 90},
            }}]),
            competitor_discovery_response,
            response([{
                "keyword": "home workout equipment reviews",
                "keyword_difficulty": None,
            }]),
        ]

        summary = run_stage_ingest(self.run)

        self.assertEqual(summary["stage_status"], "complete")
        self.assertEqual(summary["total_cost_usd"], 0.02)
        self.assertEqual(summary["total_raw_fetches"], 5)
        self.assertEqual(RawFetch.objects.filter(run=self.run).count(), 5)
        endpoints = set(
            RawFetch.objects.filter(run=self.run).values_list("endpoint", flat=True)
        )
        self.assertIn("/dataforseo_labs/google/keyword_suggestions/live", endpoints)
        self.assertIn("/dataforseo_labs/google/related_keywords/live", endpoints)
        discovery_calls = api_post.call_args_list[:3]
        self.assertEqual(
            [call.args[0] for call in discovery_calls],
            [
                "/dataforseo_labs/google/keyword_ideas/live",
                "/dataforseo_labs/google/keyword_suggestions/live",
                "/dataforseo_labs/google/related_keywords/live",
            ],
        )
        self.assertEqual(discovery_calls[0].args[1], [{
            "keywords": ["home workout equipment reviews"],
            "location_code": 2826,
            "language_name": "English",
            "limit": 100,
        }])
        for call in discovery_calls[1:]:
            self.assertEqual(call.args[1], [{
                "keyword": "home workout equipment reviews",
                "location_code": 2826,
                "language_name": "English",
                "limit": 100,
            }])
        stage = RunStage.objects.get(run=self.run, name="ingest")
        self.assertEqual(stage.status, "complete")
        self.assertEqual(stage.error, "")

    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_bulk_keyword_difficulty"
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_competitor_domains",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_related_keywords"
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_keyword_suggestions"
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_keyword_ideas"
    )
    def test_discovery_union_is_deduplicated_before_bulk_difficulty(
        self, keyword_ideas, keyword_suggestions, related_keywords,
        competitor_domains, bulk_difficulty
    ):
        keyword_ideas.return_value = [
            SimpleNamespace(keyword="Running Shoes"),
            SimpleNamespace(keyword="gym clothes"),
        ]
        keyword_suggestions.return_value = [
            SimpleNamespace(keyword=" running   shoes "),
            SimpleNamespace(keyword="workout gear"),
        ]
        related_keywords.return_value = [
            SimpleNamespace(keyword="GYM CLOTHES"),
            SimpleNamespace(keyword="training apparel"),
            SimpleNamespace(keyword="   "),
        ]
        bulk_difficulty.return_value = []

        summary = run_stage_ingest(self.run)

        self.assertEqual(summary["stage_status"], "complete")
        keyword_ideas.assert_called_once_with(
            keywords=["home workout equipment reviews"], limit=100
        )
        keyword_suggestions.assert_called_once_with(
            keyword="home workout equipment reviews", limit=100
        )
        related_keywords.assert_called_once_with(
            keyword="home workout equipment reviews", limit=100
        )
        bulk_difficulty.assert_called_once_with(keywords=[
            "Running Shoes",
            "gym clothes",
            "workout gear",
            "training apparel",
        ])

    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_bulk_keyword_difficulty",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_relevant_pages",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_domain_intersection",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_competitor_domains"
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_related_keywords",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_keyword_suggestions",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_keyword_ideas",
        return_value=[],
    )
    def test_empty_competitor_list_auto_discovers_and_updates_snapshot(
        self, keyword_ideas, keyword_suggestions, related_keywords,
        competitor_domains, domain_intersection, relevant_pages, bulk_difficulty
    ):
        competitor_domains.return_value = [
            SimpleNamespace(domain="www.example.com", intersections=999),
            SimpleNamespace(domain="youtube.com", intersections=900),
            SimpleNamespace(domain="amazon.co.uk", intersections=850),
            SimpleNamespace(
                domain="generic-directory.example", intersections=100,
                full_domain_metrics=SimpleNamespace(organic={"count": 200000}),
            ),
            SimpleNamespace(domain="low.example", intersections=5),
            SimpleNamespace(domain="top.example", intersections=80),
            SimpleNamespace(domain="mid.example", intersections=30),
            SimpleNamespace(domain="TOP.EXAMPLE", intersections=20),
            SimpleNamespace(domain="fourth.example", intersections=1),
        ]

        first = run_stage_ingest(self.run)
        second = run_stage_ingest(self.run)

        expected = ["top.example", "mid.example", "low.example"]
        self.run.refresh_from_db()
        self.assertEqual(first["markets"]["UK"]["competitors"], expected)
        self.assertTrue(first["markets"]["UK"]["competitors_auto_discovered"])
        self.assertEqual(
            self.run.settings_snapshot["markets"][0]["competitors"], expected
        )
        self.assertTrue(
            self.run.settings_snapshot["markets"][0]["competitors_auto_discovered"]
        )
        self.assertEqual(
            self.run.settings_snapshot["markets"][0]["competitor_discovery_version"],
            2,
        )
        self.assertFalse(Competitor.objects.filter(market=self.market).exists())
        self.assertFalse(second["markets"]["UK"]["competitors_auto_discovered"])
        self.assertEqual(Competitor.objects.filter(market=self.market).count(), 0)
        competitor_domains.assert_called_once_with(
            target_domain="example.com", limit=50
        )
        self.assertEqual(domain_intersection.call_count, 6)
        self.assertEqual(relevant_pages.call_count, 6)
        self.assertEqual(first["total_raw_fetches"], 10)

    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_bulk_keyword_difficulty",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_relevant_pages",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_domain_intersection",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_competitor_domains"
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_related_keywords",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_keyword_suggestions",
        return_value=[],
    )
    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_keyword_ideas",
        return_value=[],
    )
    def test_configured_competitors_bypass_auto_discovery(
        self, keyword_ideas, keyword_suggestions, related_keywords,
        competitor_domains, domain_intersection, relevant_pages, bulk_difficulty
    ):
        self.run.settings_snapshot["markets"][0]["competitors"] = ["manual.example"]
        self.run.save(update_fields=["settings_snapshot"])

        summary = run_stage_ingest(self.run)

        competitor_domains.assert_not_called()
        domain_intersection.assert_called_once()
        relevant_pages.assert_called_once()
        self.assertEqual(
            summary["markets"]["UK"]["competitors"], ["manual.example"]
        )
        self.assertFalse(
            summary["markets"]["UK"]["competitors_auto_discovered"]
        )
        self.assertEqual(Competitor.objects.count(), 0)

    @patch(
        "apps.runs.stages.stage_1_ingest.DataForSEOConnector.get_keyword_ideas",
        side_effect=ValueError("provider response could not be parsed"),
    )
    def test_all_market_failure_records_failed_ingest_stage(self, _keyword_ideas):
        with self.assertRaisesMessage(
            RuntimeError,
            "DataForSEO ingestion failed for every configured market.",
        ):
            run_stage_ingest(self.run)

        stage = RunStage.objects.get(run=self.run, name="ingest")
        self.assertEqual(stage.status, "failed")
        self.assertIn("provider response could not be parsed", stage.error)
        self.assertIsNotNone(stage.finished_at)

    @patch("apps.connectors.dataforseo.client.DataForSEOClient._post")
    def test_missing_budget_uses_fifty_dollar_fallback_and_stops(self, api_post):
        second_market = Market.objects.create(
            client=self.run.client,
            code="DE",
            country_iso="DE",
            language_code="de",
            dataforseo_location_code=2276,
            gsc_property="sc-domain:example.com",
            sitemap_url="https://example.com/de/sitemap.xml",
        )
        self.run.settings_snapshot["markets"].append({
            "market_id": second_market.pk,
            "market_code": second_market.code,
            "competitors": [],
        })
        self.run.save(update_fields=["settings_snapshot"])
        paid_response = response([{"keyword": "home workout equipment reviews"}])
        paid_response["cost"] = 50.0
        api_post.return_value = paid_response

        with self.assertRaisesMessage(RuntimeError, "$50.00 limit"):
            run_stage_ingest(self.run)

        self.assertEqual(api_post.call_count, 1)
        self.assertEqual(RawFetch.objects.filter(run=self.run).count(), 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.total_cost_usd, Decimal("50.0000"))
        self.assertIn("Budget guardrail hit", self.run.error)
        stage = RunStage.objects.get(run=self.run, name="ingest")
        self.assertEqual(stage.status, "failed")
        self.assertIn("$50.00 limit", stage.error)

    @patch("apps.connectors.dataforseo.client.DataForSEOClient._post")
    def test_custom_budget_stops_immediately_after_threshold(self, api_post):
        self.run.settings_snapshot["engine_settings"] = {
            "UK": {"max_spend_per_run_usd": 0.02},
        }
        self.run.save(update_fields=["settings_snapshot"])
        first = response([{"keyword": "home workout equipment reviews"}])
        first["cost"] = 0.01
        second = response([{"keyword": "home gym reviews"}])
        second["cost"] = 0.01
        api_post.side_effect = [first, second]

        with self.assertRaisesMessage(RuntimeError, "$0.02 limit"):
            run_stage_ingest(self.run)

        # Keyword Ideas and Suggestions ran; Related Keywords and every later
        # endpoint were blocked as soon as cumulative spend reached the limit.
        self.assertEqual(api_post.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in api_post.call_args_list],
            [
                "/dataforseo_labs/google/keyword_ideas/live",
                "/dataforseo_labs/google/keyword_suggestions/live",
            ],
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.total_cost_usd, Decimal("0.0200"))

    @patch("apps.connectors.dataforseo.client.DataForSEOClient._post")
    def test_failed_task_is_not_reused_from_cache(self, api_post):
        failed = {
            "status_code": 20000,
            "tasks": [{
                "status_code": 40501,
                "status_message": "Invalid Field: 'keyword'.",
                "result": None,
            }],
        }
        valid = response([{"keyword": "home gym reviews"}])
        api_post.side_effect = [failed, valid]
        connector = DataForSEOConnector(
            self.run, self.market, login="login", password="password"
        )

        with self.assertRaisesMessage(DataForSEOResponseError, "40501"):
            connector.get_keyword_suggestions(
                keyword="home workout equipment reviews", limit=100
            )
        rows = connector.get_keyword_suggestions(
            keyword="home workout equipment reviews", limit=100
        )

        self.assertEqual([row.keyword for row in rows], ["home gym reviews"])
        self.assertEqual(api_post.call_count, 2)
        self.assertEqual(RawFetch.objects.filter(run=self.run).count(), 2)

    @patch("apps.connectors.dataforseo.client.DataForSEOClient._post")
    def test_task_error_fails_ingest_before_later_api_calls(self, api_post):
        api_post.side_effect = [
            response([{"keyword": "home workout equipment reviews"}]),
            {
                "status_code": 20000,
                "tasks": [{
                    "status_code": 40501,
                    "status_message": "Invalid Field: 'keyword'.",
                    "result": None,
                }],
            },
        ]

        with self.assertRaisesMessage(
            RuntimeError,
            "DataForSEO ingestion failed for every configured market.",
        ):
            run_stage_ingest(self.run)

        self.assertEqual(api_post.call_count, 2)
        self.assertEqual(RawFetch.objects.filter(run=self.run).count(), 2)
        stage = RunStage.objects.get(run=self.run, name="ingest")
        self.assertEqual(stage.status, "failed")
        self.assertIn("40501", stage.error)
        self.assertNotIn("related_keywords", " ".join(
            RawFetch.objects.filter(run=self.run).values_list("endpoint", flat=True)
        ))

    @patch("apps.connectors.dataforseo.client.DataForSEOClient._post")
    def test_each_seed_gets_its_own_suggestion_and_related_request(self, api_post):
        self.run.settings_snapshot["seed_keywords"] = ["first seed", "second seed"]
        self.run.settings_snapshot["markets"][0]["competitors"] = ["competitor.example"]
        self.run.save(update_fields=["settings_snapshot"])
        api_post.side_effect = [
            response([]),
            response([{"keyword": "first suggestion"}]),
            response([{"keyword": "second suggestion"}]),
            response([{"keyword_data": {"keyword": "first related"}}]),
            response([{"keyword_data": {"keyword": "second related"}}]),
            response([]),
            response([]),
            response([]),
        ]

        run_stage_ingest(self.run)

        calls = api_post.call_args_list
        suggestion_payloads = [
            call.args[1][0]
            for call in calls
            if "keyword_suggestions" in call.args[0]
        ]
        related_payloads = [
            call.args[1][0]
            for call in calls
            if "related_keywords" in call.args[0]
        ]
        self.assertEqual(
            [payload["keyword"] for payload in suggestion_payloads],
            ["first seed", "second seed"],
        )
        self.assertEqual(
            [payload["keyword"] for payload in related_payloads],
            ["first seed", "second seed"],
        )
        self.assertTrue(all("keywords" not in payload for payload in [
            *suggestion_payloads, *related_payloads,
        ]))


class CeleryPipelineTaskTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Celery Client", slug="celery-client", primary_domain="example.com"
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

    def _run(self):
        return Run.objects.create(
            client=self.client_record,
            markets=["UK"],
            seed_keywords="running shoes",
        )

    def test_task_apply_matches_management_command(self):
        command_run = self._run()
        task_run = self._run()

        call_command(
            "run_pipeline", run_id=command_run.pk, stage="plan", verbosity=0
        )
        result = run_pipeline_async.apply(
            args=[task_run.pk], kwargs={"stages": ["plan"]}, throw=True
        )

        command_run.refresh_from_db()
        task_run.refresh_from_db()
        command_stage = RunStage.objects.get(run=command_run, name="plan")
        task_stage = RunStage.objects.get(run=task_run, name="plan")
        command_snapshot = dict(command_run.settings_snapshot)
        task_snapshot = dict(task_run.settings_snapshot)
        command_snapshot.pop("snapshot_taken_at")
        task_snapshot.pop("snapshot_taken_at")

        self.assertTrue(result.successful())
        self.assertEqual(result.result["status"], "complete")
        self.assertEqual(result.result["stage"], "plan")
        self.assertEqual(task_run.status, command_run.status)
        self.assertEqual(task_snapshot, command_snapshot)
        self.assertEqual(task_stage.status, command_stage.status)
        self.assertEqual(task_stage.records_in, command_stage.records_in)
        self.assertEqual(task_stage.records_out, command_stage.records_out)


class PipelineStabilityTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Test Client", slug="test-client", primary_domain="example.com"
        )
        self.uk = self._market("UK", "GB", "en", 2826)
        self.de = self._market("DE", "DE", "de", 2276)

    def _market(self, code, country, language, location):
        return Market.objects.create(
            client=self.client_record,
            code=code,
            country_iso=country,
            language_code=language,
            dataforseo_location_code=location,
            gsc_property="sc-domain:example.com",
            sitemap_url="https://example.com/sitemap.xml",
        )

    def _run(self):
        return Run.objects.create(client=self.client_record, seed_keywords="running shoes")

    def _raw(self, run, market, endpoint, items, params=None):
        return RawFetch.objects.create(
            run=run,
            market=market,
            source="dataforseo",
            endpoint=f"/dataforseo_labs/google/{endpoint}/live",
            request_params=params or {},
            request_hash=f"{run.pk}-{market.pk}-{endpoint}",
            payload=response(items),
        )

    def _keyword_idea(self, keyword, volume=100, difficulty=None, core=None):
        return {
            "keyword": keyword,
            "keyword_info": {"search_volume": volume, "cpc": 1.25, "competition": 0.4},
            "keyword_properties": {"keyword_difficulty": difficulty, "core_keyword": core},
            "search_intent_info": {"main_intent": "commercial"},
        }

    def test_normalise_is_idempotent_and_applies_bulk_difficulty(self):
        run = self._run()
        self._raw(run, self.uk, "keyword_ideas", [self._keyword_idea("Running   Shoes")])
        self._raw(run, self.uk, "bulk_keyword_difficulty", [
            {"keyword": "running shoes", "keyword_difficulty": 37}
        ])

        first = run_stage_normalise(run)
        second = run_stage_normalise(run)

        self.assertEqual(first["observations_created"], 1)
        self.assertEqual(second["observations_created"], 0)
        self.assertEqual(second["observations_updated"], 1)
        self.assertEqual(KeywordObservation.objects.filter(run=run).count(), 1)
        observation = KeywordObservation.objects.get(run=run)
        self.assertEqual(observation.keyword_normalised, "running shoes")
        self.assertEqual(observation.keyword_difficulty, 37)

    def test_normalise_parses_all_keyword_discovery_endpoints(self):
        run = self._run()
        self._raw(run, self.uk, "keyword_ideas", [
            self._keyword_idea("running shoes", 100)
        ])
        self._raw(run, self.uk, "keyword_suggestions", [
            self._keyword_idea("best running shoes", 80)
        ])
        self._raw(run, self.uk, "related_keywords", [
            {"keyword_data": self._keyword_idea("jogging trainers", 60)}
        ])

        summary = run_stage_normalise(run)

        observations = KeywordObservation.objects.filter(
            run=run, signal="keyword_research"
        )
        self.assertEqual(summary["observations_created"], 3)
        self.assertEqual(observations.count(), 3)
        self.assertSetEqual(
            set(observations.values_list("keyword", flat=True)),
            {"running shoes", "best running shoes", "jogging trainers"},
        )

    def test_competitor_url_flows_from_real_gap_shape_to_export(self):
        run = self._run()
        competitor_url = "https://competitor.example/best-running-shoes"
        self._raw(
            run,
            self.uk,
            "domain_intersection",
            [{
                "keyword_data": {
                    "keyword": "running shoes",
                    "keyword_info": {
                        "search_volume": 2400,
                        "cpc": 1.75,
                        "competition": 0.65,
                    },
                    "keyword_properties": {"keyword_difficulty": 42},
                },
                "first_domain_serp_element": {
                    "domain": "competitor.example",
                    "url": competitor_url,
                    "rank_group": 2,
                },
                "second_domain_serp_element": None,
            }],
            params={"target1": "competitor.example", "target2": "example.com"},
        )

        run_stage_normalise(run)
        observation = KeywordObservation.objects.get(
            run=run, signal="competitor_gap"
        )
        self.assertEqual(observation.competitor_url, competitor_url)
        self.assertEqual(observation.competitor_domain, "competitor.example")
        self.assertEqual(observation.search_volume, 2400)
        self.assertEqual(observation.cpc, Decimal("1.75"))
        self.assertEqual(observation.competition, 0.65)
        self.assertEqual(observation.keyword_difficulty, 42)

        run_stage_cluster(run)
        run_stage_decide(run)
        opportunity = Opportunity.objects.get(run=run)
        self.assertEqual(opportunity.competitor_url, competitor_url)
        self.assertEqual(
            opportunity.decision_trace["competitor_url"]["competitor_domain"],
            "competitor.example",
        )

        tabs, _ = build_export_tabs(run)
        competitor_column = OPPORTUNITY_COLUMNS.index("Competitor URL")
        self.assertEqual(tabs["Opportunities"][1][competitor_column], competitor_url)

    def test_database_rejects_duplicate_observation_identity(self):
        run = self._run()
        values = dict(
            run=run, market=self.uk, keyword="Shoes", keyword_normalised="shoes",
            source="dataforseo", signal="keyword_research", competitor_domain="",
        )
        KeywordObservation.objects.create(**values)
        with self.assertRaises(IntegrityError):
            KeywordObservation.objects.create(**values)

    def test_malformed_payload_fails_loudly_and_records_stage_error(self):
        run = self._run()
        RawFetch.objects.create(
            run=run, market=self.uk, source="dataforseo",
            endpoint="/dataforseo_labs/google/keyword_ideas/live",
            request_params={}, request_hash="malformed", payload={"unexpected": []},
        )
        with self.assertRaises(PayloadStructureError):
            run_stage_normalise(run)
        stage = RunStage.objects.get(run=run, name="normalise")
        self.assertEqual(stage.status, "failed")
        self.assertIn("Malformed DataForSEO payload", stage.error)

    def test_clustering_is_partitioned_by_market(self):
        run = self._run()
        self._raw(run, self.uk, "keyword_ideas", [self._keyword_idea("running shoes", core="running shoes")])
        self._raw(run, self.de, "keyword_ideas", [self._keyword_idea("laufschuhe", core="laufschuhe")])
        run_stage_normalise(run)

        summary = run_stage_cluster(run)

        self.assertEqual(summary["markets"]["UK"]["topics"], 1)
        self.assertEqual(summary["markets"]["DE"]["topics"], 1)
        self.assertEqual(Topic.objects.filter(market=self.uk).count(), 1)
        self.assertEqual(Topic.objects.filter(market=self.de).count(), 1)

    def test_clustering_prefers_three_shared_serp_urls(self):
        run = self._run()
        shared = [
            "https://one.example/page",
            "https://two.example/page",
            "https://three.example/page",
        ]
        for keyword, extra_url in (
            ("running footwear", "https://four.example/page"),
            ("marathon trainers", "https://five.example/page"),
        ):
            KeywordObservation.objects.create(
                run=run, market=self.uk, keyword=keyword,
                keyword_normalised=keyword, source="dataforseo",
                signal="keyword_research", serp_top_urls=[*shared, extra_url],
            )

        summary = run_stage_cluster(run)

        self.assertEqual(summary["markets"]["UK"]["topics"], 1)
        self.assertEqual(summary["markets"]["UK"]["serp_overlap_threshold"], 3)
        self.assertEqual(TopicKeyword.objects.filter(topic__last_seen_run=run).count(), 2)

    def test_clustering_uses_snapshot_serp_overlap_threshold(self):
        run = self._run()
        run.settings_snapshot = {
            "engine_settings": {"serp_overlap_threshold": 4}
        }
        run.save(update_fields=["settings_snapshot"])
        shared = [
            "https://one.example/page",
            "https://two.example/page",
            "https://three.example/page",
        ]
        for keyword in ("running footwear", "marathon trainers"):
            KeywordObservation.objects.create(
                run=run, market=self.uk, keyword=keyword,
                keyword_normalised=keyword, source="dataforseo",
                signal="keyword_research", serp_top_urls=shared,
            )

        summary = run_stage_cluster(run)

        self.assertEqual(summary["markets"]["UK"]["topics"], 2)
        self.assertEqual(summary["markets"]["UK"]["serp_overlap_threshold"], 4)

    def test_clustering_falls_back_to_jaccard_without_serp_overlap(self):
        run = self._run()
        for keyword in ("running shoes", "best running shoes"):
            KeywordObservation.objects.create(
                run=run, market=self.uk, keyword=keyword,
                keyword_normalised=keyword, source="dataforseo",
                signal="keyword_research", serp_top_urls=[],
            )

        summary = run_stage_cluster(run)

        self.assertEqual(summary["markets"]["UK"]["topics"], 1)

    def test_clustering_uses_each_markets_semantic_threshold(self):
        run = self._run()
        run.settings_snapshot = {
            "engine_settings": {
                "UK": {
                    "semantic_similarity_threshold": 0.8,
                    "serp_overlap_threshold": 4,
                },
                "DE": {
                    "semantic_similarity_threshold": 0.5,
                    "serp_overlap_threshold": 2,
                },
            }
        }
        run.save(update_fields=["settings_snapshot"])
        for market in (self.uk, self.de):
            for keyword in ("red shoes", "red running shoes"):
                KeywordObservation.objects.create(
                    run=run, market=market, keyword=keyword,
                    keyword_normalised=keyword, source="dataforseo",
                    signal="keyword_research",
                )

        summary = run_stage_cluster(run)

        self.assertEqual(summary["markets"]["UK"]["topics"], 2)
        self.assertEqual(summary["markets"]["DE"]["topics"], 1)
        self.assertEqual(
            summary["markets"]["UK"]["semantic_similarity_threshold"], 0.8
        )
        self.assertEqual(
            summary["markets"]["DE"]["semantic_similarity_threshold"], 0.5
        )
        self.assertEqual(summary["markets"]["UK"]["serp_overlap_threshold"], 4)
        self.assertEqual(summary["markets"]["DE"]["serp_overlap_threshold"], 2)

    def test_stable_topic_is_reconciled_across_runs(self):
        first_run = self._run()
        self._raw(first_run, self.uk, "keyword_ideas", [self._keyword_idea("running shoes", 100)])
        run_stage_normalise(first_run)
        first_summary = run_stage_cluster(first_run)
        topic = Topic.objects.get()
        topic_id = topic.pk

        second_run = self._run()
        self._raw(second_run, self.uk, "keyword_ideas", [self._keyword_idea("running shoes", 250)])
        run_stage_normalise(second_run)
        second_summary = run_stage_cluster(second_run)

        topic.refresh_from_db()
        self.assertEqual(first_summary["topics_created"], 1)
        self.assertEqual(second_summary["topics_created"], 0)
        self.assertEqual(second_summary["topics_updated"], 1)
        self.assertEqual(Topic.objects.count(), 1)
        self.assertEqual(topic.pk, topic_id)
        self.assertEqual(topic.first_seen_run, first_run)
        self.assertEqual(topic.last_seen_run, second_run)
        self.assertEqual(topic.total_search_volume, 250)


class AnalyticsNormalisationTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Analytics Client", slug="analytics-client", primary_domain="example.com"
        )
        self.market = Market.objects.create(
            client=self.client_record, code="UK", country_iso="GB", language_code="en",
            dataforseo_location_code=2826, gsc_property="sc-domain:example.com",
            ga4_property_id="123456789", sitemap_url="https://example.com/sitemap.xml",
        )
        self.run = Run.objects.create(
            client=self.client_record,
            settings_snapshot={
                "engine_settings": {"quick_win_min_position": 7, "quick_win_max_position": 20}
            },
        )

    def _fetch_fixtures(self):
        GSCConnector(self.run, self.market).fetch(
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 28)
        )
        GA4Connector(self.run, self.market).fetch()

    def test_gsc_and_ga4_are_normalised_into_durable_page_metrics(self):
        self._fetch_fixtures()

        summary = run_stage_analytics(self.run)

        self.assertEqual(summary["gsc"]["snapshots_created"], 20)
        self.assertEqual(PositionSnapshot.objects.count(), 20)
        self.assertEqual(
            KeywordObservation.objects.filter(source="gsc", signal="quick_win").count(), 10
        )
        page = ExistingPage.objects.get(path="/collections/running-shoes")
        self.assertFalse(page.in_sitemap)
        self.assertEqual(page.total_clicks_28d, 530)
        self.assertEqual(page.total_impressions_28d, 16500)
        self.assertEqual(page.ranking_keyword_count, 2)
        self.assertEqual(page.sessions_28d, 1500)
        self.assertEqual(page.conversions_28d, 45)
        self.assertEqual(page.conversion_rate, 0.03)
        self.assertEqual(page.revenue_28d, Decimal("6750.00"))

    def test_analytics_normalisation_is_idempotent(self):
        self._fetch_fixtures()

        first = run_stage_analytics(self.run)
        second = run_stage_analytics(self.run)

        self.assertEqual(first["gsc"]["snapshots_created"], 20)
        self.assertEqual(second["gsc"]["snapshots_created"], 0)
        self.assertEqual(second["gsc"]["snapshots_updated"], 20)
        self.assertEqual(PositionSnapshot.objects.count(), 20)
        self.assertEqual(KeywordObservation.objects.filter(source="gsc").count(), 20)
        self.assertEqual(ExistingPage.objects.count(), 10)

    def test_same_keyword_can_preserve_rankings_for_two_pages(self):
        rows = [
            {
                "keys": ["running shoes", url, "gbr", "2026-07-15"],
                "clicks": clicks,
                "impressions": 100,
                "ctr": clicks / 100,
                "position": position,
            }
            for url, clicks, position in [
                ("https://example.com/shoes", 10, 8),
                ("https://example.com/running", 5, 12),
            ]
        ]
        RawFetch.objects.create(
            run=self.run, market=self.market, source="gsc", endpoint="searchAnalytics/query",
            request_params={}, request_hash="two-pages", payload={"rows": rows},
        )

        run_stage_analytics(self.run)

        observations = KeywordObservation.objects.filter(
            source="gsc", keyword_normalised="running shoes"
        )
        self.assertEqual(observations.count(), 2)
        self.assertEqual(set(observations.values_list("our_url", flat=True)), {
            "https://example.com/shoes", "https://example.com/running"
        })

    def test_gsc_signals_use_each_markets_quick_win_range(self):
        de = Market.objects.create(
            client=self.client_record, code="DE", country_iso="DE",
            language_code="de", dataforseo_location_code=2276,
            gsc_property="sc-domain:example.com",
            sitemap_url="https://example.com/de.xml",
        )
        self.run.settings_snapshot = {
            "engine_settings": {
                "UK": {
                    "quick_win_min_position": 7,
                    "quick_win_max_position": 20,
                },
                "DE": {
                    "quick_win_min_position": 10,
                    "quick_win_max_position": 20,
                },
            }
        }
        self.run.save(update_fields=["settings_snapshot"])
        for market, country, page in (
            (self.market, "gbr", "https://example.com/uk-shoes"),
            (de, "deu", "https://example.com/de-shoes"),
        ):
            RawFetch.objects.create(
                run=self.run, market=market, source="gsc",
                endpoint="searchAnalytics/query", request_params={},
                request_hash=f"quick-win-{market.code}",
                payload={"rows": [{
                    "keys": ["running shoes", page, country, "2026-07-15"],
                    "clicks": 10, "impressions": 100,
                    "ctr": 0.1, "position": 8,
                }]},
            )

        run_stage_analytics(self.run)

        self.assertEqual(
            KeywordObservation.objects.get(market=self.market).signal,
            "quick_win",
        )
        self.assertEqual(
            KeywordObservation.objects.get(market=de).signal,
            "existing_ranking",
        )

    def test_older_snapshot_triggers_ranking_decay_and_preserves_previous_position(self):
        PositionSnapshot.objects.create(
            market=self.market, observed_on=date(2026, 6, 15),
            keyword="running shoes", keyword_normalised="running shoes",
            page_url="https://example.com/collections/running-shoes", country="gbr",
            clicks=100, impressions=1000, ctr=0.1, position=3.0,
        )
        GSCConnector(self.run, self.market).fetch(
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 28)
        )

        run_stage_analytics(self.run)

        observation = KeywordObservation.objects.get(
            source="gsc", keyword_normalised="running shoes"
        )
        self.assertEqual(observation.signal, "ranking_decay")
        self.assertEqual(observation.previous_position, 3.0)
        self.assertEqual(observation.our_position, 8.2)

    def test_management_command_runs_google_and_analytics_stages_separately(self):
        self.run.settings_snapshot = {
            "markets": [{"market_id": self.market.pk, "market_code": "UK"}],
            "engine_settings": {"quick_win_min_position": 7, "quick_win_max_position": 20},
        }
        self.run.save(update_fields=["settings_snapshot"])

        call_command("run_pipeline", run_id=self.run.pk, stage="google", verbosity=0)
        call_command("run_pipeline", run_id=self.run.pk, stage="analytics", verbosity=0)

        self.assertEqual(RawFetch.objects.filter(run=self.run, source="gsc").count(), 1)
        self.assertEqual(RawFetch.objects.filter(run=self.run, source="ga4").count(), 1)
        self.assertEqual(PositionSnapshot.objects.count(), 20)
        self.assertEqual(
            ExistingPage.objects.get(path="/collections/running-shoes").sessions_28d,
            1500,
        )


class DecisionEngineTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Decision Client", slug="decision-client", primary_domain="example.com"
        )
        self.market = Market.objects.create(
            client=self.client_record, code="UK", country_iso="GB", language_code="en",
            dataforseo_location_code=2826, gsc_property="sc-domain:example.com",
            ga4_property_id="123", sitemap_url="https://example.com/sitemap.xml",
        )
        self.run = Run.objects.create(
            client=self.client_record,
            settings_snapshot={
                "engine_settings": {"min_search_volume": 50, "max_keyword_difficulty": 80}
            },
        )

    def _topic(self, keyword, volume=1000, difficulty=30, intent="commercial"):
        topic = Topic.objects.create(
            client=self.client_record, market=self.market,
            topic_uid=f"uid-{keyword.replace(' ', '-')}", label=keyword.title(),
            primary_keyword=keyword, primary_keyword_volume=volume,
            total_search_volume=volume, intent=intent,
            first_seen_run=self.run, last_seen_run=self.run,
        )
        TopicKeyword.objects.create(
            topic=topic, keyword=keyword, search_volume=volume,
            keyword_difficulty=difficulty, is_primary=True,
        )
        return topic

    def _ranking(self, keyword, url, position, clicks=10, impressions=100):
        return KeywordObservation.objects.create(
            run=self.run, market=self.market, keyword=keyword,
            keyword_normalised=keyword.lower(), source="gsc", signal="quick_win",
            our_url=url, our_position=position, clicks=clicks,
            impressions=impressions, ctr=clicks / impressions,
        )

    def _serp_observation(
        self, topic, features, intent=None, position=None
    ):
        return KeywordObservation.objects.create(
            run=self.run,
            market=self.market,
            keyword=topic.primary_keyword,
            keyword_normalised=topic.primary_keyword.lower(),
            source="dataforseo",
            signal="keyword_research",
            serp_features=features,
            intent=intent or topic.intent,
            our_position=position,
        )

    def test_ranking_match_detects_cannibalisation_and_decides_merge(self):
        topic = self._topic("running shoes")
        first = ExistingPage.objects.create(
            market=self.market, url="https://example.com/shoes", path="/shoes",
            sessions_28d=1000, conversions_28d=40, conversion_rate=0.04,
        )
        second = ExistingPage.objects.create(
            market=self.market, url="https://example.com/running", path="/running"
        )
        self._ranking("running shoes", first.url, 8, clicks=20)
        self._ranking("running shoes", second.url, 12, clicks=5)

        match = run_stage_match(self.run)
        decide = run_stage_decide(self.run, match["match_results"])

        result = match["match_results"][topic.pk]
        opportunity = Opportunity.objects.get(topic=topic)
        self.assertTrue(result["cannibalisation"])
        self.assertEqual(result["match_source"], "gsc_ranking")
        self.assertEqual(decide["merge"], 1)
        self.assertEqual(opportunity.action, "merge")
        self.assertEqual(set(opportunity.target_urls), {first.url, second.url})
        self.assertEqual(opportunity.conversion_basis, "data")

    def test_top_three_is_ignored_and_unmatched_valid_topic_is_new_content(self):
        winning = self._topic("winning shoes")
        new_topic = self._topic("trail socks")
        page = ExistingPage.objects.create(
            market=self.market, url="https://example.com/winning", path="/winning"
        )
        self._ranking("winning shoes", page.url, 2)

        match = run_stage_match(self.run)
        summary = run_stage_decide(self.run, match["match_results"])

        self.assertEqual(Opportunity.objects.get(topic=winning).action, "ignore")
        self.assertEqual(Opportunity.objects.get(topic=new_topic).action, "new_content")
        self.assertEqual(summary["ignore"], 1)
        self.assertEqual(summary["new_content"], 1)

    def test_estimated_impact_calculates_clicks_and_data_based_conversions(self):
        new_topic = self._topic("trail running guide", volume=1000)
        optimise_topic = self._topic("best trail shoes", volume=1000)
        page = ExistingPage.objects.create(
            market=self.market,
            url="https://example.com/best-trail-shoes",
            path="/best-trail-shoes",
            sessions_28d=100,
            conversions_28d=10,
            conversion_rate=0.10,
        )

        run_stage_decide(self.run, {
            optimise_topic.pk: {
                "matched": True,
                "matched_url": page.url,
                "matched_urls": [page.url],
                "match_source": "controlled_match",
                "current_position": 8,
            }
        })

        new_opportunity = Opportunity.objects.get(topic=new_topic)
        optimise_opportunity = Opportunity.objects.get(topic=optimise_topic)
        self.assertEqual(new_opportunity.estimated_impact, {
            "monthly_clicks_gain": 20,
            "monthly_conversions_gain": None,
        })
        self.assertEqual(optimise_opportunity.estimated_impact, {
            "monthly_clicks_gain": 30,
            "monthly_conversions_gain": 3,
        })
        impact_trace = optimise_opportunity.decision_trace["estimated_impact"]
        self.assertEqual(impact_trace["target_position"], 5)
        self.assertEqual(impact_trace["current_ctr"], 0.02)
        self.assertEqual(impact_trace["target_ctr"], 0.05)

    def test_best_competitor_url_prefers_primary_keyword_then_volume(self):
        primary_topic = self._topic("running shoes", volume=1000)
        secondary = TopicKeyword.objects.create(
            topic=primary_topic, keyword="best trainers", search_volume=5000,
            keyword_difficulty=30, is_primary=False,
        )
        KeywordObservation.objects.create(
            run=self.run, market=self.market, keyword=secondary.keyword,
            keyword_normalised=secondary.keyword, source="dataforseo",
            signal="competitor_gap", competitor_domain="high-volume.example",
            competitor_url="https://high-volume.example/best-trainers",
            search_volume=5000,
        )
        primary_evidence = KeywordObservation.objects.create(
            run=self.run, market=self.market, keyword=primary_topic.primary_keyword,
            keyword_normalised=primary_topic.primary_keyword, source="dataforseo",
            signal="competitor_gap", competitor_domain="primary.example",
            competitor_url="https://primary.example/running-shoes",
            search_volume=1000,
        )

        run_stage_decide(self.run)

        opportunity = Opportunity.objects.get(topic=primary_topic)
        self.assertEqual(
            opportunity.competitor_url,
            "https://primary.example/running-shoes",
        )
        trace = opportunity.decision_trace["competitor_url"]
        self.assertEqual(trace["observation_id"], primary_evidence.pk)
        self.assertEqual(
            trace["selection_rule"],
            "primary_keyword_then_highest_search_volume",
        )

    def test_best_competitor_url_uses_highest_volume_without_primary_evidence(self):
        topic = self._topic("trail footwear", volume=1000)
        for keyword, volume, url, domain in (
            ("trail shoes", 500, "https://low.example/trail", "low.example"),
            ("off road trainers", 2500, "https://best.example/off-road", "best.example"),
        ):
            TopicKeyword.objects.create(
                topic=topic, keyword=keyword, search_volume=volume,
                keyword_difficulty=30, is_primary=False,
            )
            KeywordObservation.objects.create(
                run=self.run, market=self.market, keyword=keyword,
                keyword_normalised=keyword, source="dataforseo",
                signal="competitor_gap", competitor_domain=domain,
                competitor_url=url, search_volume=volume,
            )

        run_stage_decide(self.run)

        self.assertEqual(
            Opportunity.objects.get(topic=topic).competitor_url,
            "https://best.example/off-road",
        )

    def test_decisions_use_market_specific_volume_limits(self):
        uk_topic = self._topic("small uk topic", volume=30)
        de = Market.objects.create(
            client=self.client_record, code="DE", country_iso="DE",
            language_code="de", dataforseo_location_code=2276,
            sitemap_url="https://example.com/de.xml",
        )
        de_topic = Topic.objects.create(
            client=self.client_record, market=de,
            topic_uid="uid-small-de-topic", label="Small DE Topic",
            primary_keyword="small de topic", primary_keyword_volume=30,
            total_search_volume=30, intent="commercial",
            first_seen_run=self.run, last_seen_run=self.run,
        )
        TopicKeyword.objects.create(
            topic=de_topic, keyword="small de topic", search_volume=30,
            keyword_difficulty=30, is_primary=True,
        )
        self.run.settings_snapshot = {
            "engine_settings": {
                "UK": {"min_search_volume": 50, "max_keyword_difficulty": 80},
                "DE": {"min_search_volume": 10, "max_keyword_difficulty": 80},
            }
        }
        self.run.save(update_fields=["settings_snapshot"])

        run_stage_decide(self.run)

        uk_opportunity = Opportunity.objects.get(topic=uk_topic)
        de_opportunity = Opportunity.objects.get(topic=de_topic)
        self.assertEqual(uk_opportunity.action, "ignore")
        self.assertEqual(de_opportunity.action, "new_content")
        self.assertEqual(uk_opportunity.decision_trace["minimum_volume"], 50)
        self.assertEqual(de_opportunity.decision_trace["minimum_volume"], 10)

    def test_match_is_market_isolated(self):
        topic = self._topic("running shoes")
        other_market = Market.objects.create(
            client=self.client_record, code="DE", country_iso="DE", language_code="de",
            dataforseo_location_code=2276, gsc_property="sc-domain:example.com",
            sitemap_url="https://example.com/de-sitemap.xml",
        )
        ExistingPage.objects.create(
            market=other_market, url="https://example.com/running-shoes", path="/running-shoes"
        )

        result = run_stage_match(self.run)["match_results"][topic.pk]

        self.assertFalse(result["matched"])

    def test_score_uses_measured_conversion_and_all_six_configured_factors(self):
        topic = self._topic("buy running shoes", intent="transactional")
        page = ExistingPage.objects.create(
            market=self.market, url="https://example.com/buy-running-shoes",
            path="/buy-running-shoes", sessions_28d=1000,
            conversions_28d=50, conversion_rate=0.05,
        )
        self._ranking("buy running shoes", page.url, 8)
        match = run_stage_match(self.run)
        run_stage_decide(self.run, match["match_results"])

        run_stage_score(self.run)

        opportunity = Opportunity.objects.get(topic=topic)
        scoring = opportunity.decision_trace["scoring"]
        self.assertEqual(opportunity.conversion_basis, "data")
        self.assertEqual(scoring["components"]["conversion"], 1.0)
        self.assertEqual(set(scoring["components"]), {
            "volume", "position", "conversion", "difficulty", "signal", "market"
        })
        self.assertFalse(scoring["missing_conversion_weight_redistributed"])
        self.assertGreater(opportunity.priority_score, 80)

    def test_ai_search_opportunity_uses_serp_intent_and_observation_authority(self):
        topic = self._topic(
            "how running shoes work", intent="informational"
        )
        observation = self._serp_observation(
            topic, ["ai_overview", "featured_snippet"], position=18
        )

        run_stage_decide(self.run)

        opportunity = Opportunity.objects.get(topic=topic)
        evidence = opportunity.decision_trace["ai_search_opportunity"]
        self.assertTrue(opportunity.ai_search_opportunity)
        self.assertEqual(evidence["primary_observation_id"], observation.pk)
        self.assertEqual(evidence["qualifying_features"], ["ai_overview"])
        self.assertTrue(evidence["has_eligible_intent"])
        self.assertTrue(evidence["structured_answerable"])
        self.assertTrue(evidence["has_topical_authority"])
        self.assertEqual(evidence["best_authority_position"], 18)

    def test_ai_search_opportunity_can_use_match_current_position_for_authority(self):
        topic = self._topic(
            "best running shoes", intent="commercial"
        )
        self._serp_observation(topic, ["people_also_ask"])
        page = ExistingPage.objects.create(
            market=self.market,
            url="https://example.com/best-running-shoes",
            path="/best-running-shoes",
        )
        run_stage_decide(self.run, {
            topic.pk: {
                "matched": True,
                "matched_url": page.url,
                "matched_urls": [page.url],
                "match_source": "controlled_match",
                "current_position": 12,
            }
        })

        opportunity = Opportunity.objects.get(topic=topic)
        evidence = opportunity.decision_trace["ai_search_opportunity"]
        self.assertTrue(opportunity.ai_search_opportunity)
        self.assertEqual(opportunity.current_position, 12)
        self.assertIsNone(evidence["observation_position"])
        self.assertEqual(evidence["current_position"], 12)

    def test_ai_search_opportunity_is_false_when_any_required_condition_is_missing(self):
        no_feature = self._topic("shoe care guide", intent="informational")
        wrong_intent = self._topic("buy running shoes now", intent="transactional")
        no_authority = self._topic("running shoe materials", intent="informational")
        self._serp_observation(no_feature, ["featured_snippet"], position=10)
        self._serp_observation(wrong_intent, ["ai_overview"], position=10)
        self._serp_observation(no_authority, ["people_also_ask"], position=21)

        run_stage_decide(self.run)

        results = {
            opportunity.topic_id: opportunity
            for opportunity in Opportunity.objects.filter(run=self.run)
        }
        self.assertFalse(results[no_feature.pk].ai_search_opportunity)
        self.assertFalse(results[wrong_intent.pk].ai_search_opportunity)
        self.assertFalse(results[no_authority.pk].ai_search_opportunity)
        self.assertFalse(
            results[no_feature.pk].decision_trace["ai_search_opportunity"]
            ["has_ai_serp_feature"]
        )
        self.assertFalse(
            results[wrong_intent.pk].decision_trace["ai_search_opportunity"]
            ["has_eligible_intent"]
        )
        self.assertFalse(
            results[no_authority.pk].decision_trace["ai_search_opportunity"]
            ["has_topical_authority"]
        )


class DatabaseBackupCommandTests(TestCase):
    def test_backup_database_writes_unicode_as_utf8_json(self):
        Client.objects.create(
            name="Māori Test Client",
            slug="maori-test-client",
            primary_domain="example.com",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backup.json"

            call_command("backup_database", output=str(output), verbosity=0)

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(any(
                item["model"] == "clients.client"
                and item["fields"]["name"] == "Māori Test Client"
                for item in payload
            ))

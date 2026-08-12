import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase
from django.core.management import call_command

from apps.clients.models import Client, Market, ScoringWeights
from apps.connectors.ga4 import GA4Connector
from apps.connectors.gsc import GSCConnector
from apps.ingestion.models import KeywordObservation, RawFetch
from apps.pages.models import ExistingPage, PositionSnapshot
from apps.opportunities.models import Opportunity
from apps.runs.models import Run, RunStage
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
        api_post.side_effect = [
            response([{"keyword": "home workout equipment reviews"}]),
            response([{
                "keyword": "home workout equipment reviews",
                "keyword_difficulty": None,
            }]),
        ]

        summary = run_stage_ingest(self.run)

        self.assertEqual(summary["stage_status"], "complete")
        self.assertEqual(summary["total_raw_fetches"], 2)
        self.assertEqual(RawFetch.objects.filter(run=self.run).count(), 2)
        stage = RunStage.objects.get(run=self.run, name="ingest")
        self.assertEqual(stage.status, "complete")
        self.assertEqual(stage.error, "")

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

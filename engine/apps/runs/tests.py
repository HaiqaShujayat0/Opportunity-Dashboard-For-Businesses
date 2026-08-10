from django.db import IntegrityError
from django.test import TestCase

from apps.clients.models import Client, Market
from apps.ingestion.models import KeywordObservation, RawFetch
from apps.runs.models import Run, RunStage
from apps.runs.stages.stage_2_normalise import PayloadStructureError, run_stage_normalise
from apps.runs.stages.stage_4_cluster import run_stage_cluster
from apps.topics.models import Topic


def response(items):
    return {"tasks": [{"result": [{"items": items}]}]}


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

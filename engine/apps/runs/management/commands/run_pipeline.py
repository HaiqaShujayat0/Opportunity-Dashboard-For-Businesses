"""
Management command: run_pipeline

Usage:
    python manage.py run_pipeline --run-id <ID>

What it does:
    Kicks off the Engine 1 pipeline for an existing Run record.
    Executes stages in order: PLAN -> INGEST -> NORMALISE -> ENRICH -> CLUSTER -> MATCH -> DECIDE -> SCORE
    Updates Run.status as it goes.
    Logs every step to the console so you can see exactly what's happening.

Example:
    1. Create a Run in Django Admin (set seed keywords, pick client + market)
    2. Note the Run ID
    3. Run: python manage.py run_pipeline --run-id 1
"""
import logging
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum
from django.utils import timezone

from apps.ingestion.models import RawFetch
from apps.runs.models import Run
from apps.runs.stages.stage_0_plan import run_stage_plan
from apps.runs.stages.stage_1_ingest import run_stage_ingest
from apps.runs.stages.stage_1b_sitemap import run_stage_sitemap
from apps.runs.stages.stage_1c_google import run_stage_google_ingest
from apps.runs.stages.stage_2_normalise import run_stage_normalise
from apps.runs.stages.stage_2b_analytics import run_stage_analytics
from apps.runs.stages.stage_3_enrich import run_stage_enrich
from apps.runs.stages.stage_4_cluster import run_stage_cluster
from apps.runs.stages.stage_5_match import run_stage_match
from apps.runs.stages.stage_6_decide import run_stage_decide
from apps.runs.stages.stage_8_score import run_stage_score
from apps.runs.stages.stage_9_export import run_stage_export

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the Engine 1 pipeline (Stages 0-9) for a given Run ID"

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-id",
            type=int,
            required=True,
            help="The ID of the Run record to execute (created in Django Admin)",
        )
        parser.add_argument(
            "--stage",
            type=str,
            default="all",
            choices=["all", "plan", "ingest", "sitemap", "google", "normalise", "analytics", "enrich", "cluster", "match", "decide", "score", "export"],
            help="Which stage to run. Default is 'all' (runs the complete pipeline through export).",
        )
        parser.add_argument(
            "--serp-calls",
            type=int,
            default=0,
            help="How many Advanced SERP calls to make in Stage 3 (costs ~$0.01 each). Default 0 (skip).",
        )

    def handle(self, *args, **options):
        run_id = options["run_id"]
        stage = options["stage"]
        serp_calls = options["serp_calls"]

        # ── Load the Run ────────────────────────────────────────────────
        try:
            run = Run.objects.select_related("client").get(pk=run_id)
        except Run.DoesNotExist:
            raise CommandError(f"Run #{run_id} does not exist. Create one in Django Admin first.")

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{'='*60}"
            f"\nEngine 1 Pipeline - Run #{run.pk}"
            f"\nClient : {run.client.name}"
            f"\nType   : {run.run_type}"
            f"\nStatus : {run.status}"
            f"\n{'='*60}\n"
        ))

        if run.status == "running":
            raise CommandError(
                f"Run #{run_id} is already running. "
                "If it's stuck, manually set status to 'pending' in admin."
            )

        # ── Mark as running ─────────────────────────────────────────────
        run.status = "running"
        run.started_at = timezone.now()
        run.finished_at = None
        run.error = ""
        run.save(update_fields=["status", "started_at", "finished_at", "error"])

        try:
            had_partial_stage = False
            # ── Stage 0: PLAN ────────────────────────────────────────────
            if stage in ("all", "plan"):
                self.stdout.write(self.style.HTTP_INFO("\n> Stage 0 - PLAN"))
                plan_summary = run_stage_plan(run)
                self.stdout.write(self.style.SUCCESS(
                    f"  [OK] PLAN complete\n"
                    f"     Seed keywords : {plan_summary['seed_keywords']}\n"
                    f"     Markets       : {plan_summary['markets']}\n"
                    f"     Has settings  : {plan_summary['has_engine_settings']}\n"
                ))

            # ── Stage 1: INGEST ──────────────────────────────────────────
            if stage in ("all", "ingest"):
                self.stdout.write(self.style.HTTP_INFO("\n> Stage 1 - INGEST (calling DataForSEO...)"))
                ingest_summary = run_stage_ingest(run)
                self.stdout.write(self.style.SUCCESS(
                    f"  [OK] INGEST complete\n"
                    f"     RawFetch rows : {ingest_summary['total_raw_fetches']}\n"
                    f"     Cost so far   : ${ingest_summary['total_cost_usd']:.4f} USD\n"
                    f"     Markets       : {ingest_summary['markets']}\n"
                ))

                if ingest_summary["stage_status"] == "partial":
                    had_partial_stage = True
                    self.stdout.write(self.style.WARNING(
                        "  [WARNING] Some markets failed - check logs above for details."
                    ))

            # -- Stage 1b: SITEMAP ----------------------------------------
            if stage in ("all", "sitemap"):
                self.stdout.write(self.style.HTTP_INFO(
                    "\n> Stage 1b - SITEMAP (building existing-page inventory...)"
                ))
                sitemap_summary = run_stage_sitemap(run)
                self.stdout.write(self.style.SUCCESS(
                    f"  [OK] SITEMAP {sitemap_summary['stage_status']}\n"
                    f"     Pages found    : {sitemap_summary['pages_total']}\n"
                    f"     Pages created  : {sitemap_summary['pages_created']}\n"
                    f"     Pages updated  : {sitemap_summary['pages_updated']}\n"
                    f"     Marked stale   : {sitemap_summary['pages_marked_not_in_sitemap']}\n"
                ))
                if sitemap_summary["stage_status"] == "partial":
                    had_partial_stage = True

            # -- Stage 1c: GOOGLE -----------------------------------------
            if stage in ("all", "google"):
                self.stdout.write(self.style.HTTP_INFO(
                    "\n> Stage 1c - GOOGLE (fetching configured GSC/GA4 data...)"
                ))
                google_summary = run_stage_google_ingest(run)
                self.stdout.write(self.style.SUCCESS(
                    f"  [OK] GOOGLE {google_summary['stage_status']}\n"
                    f"     Sources configured : {google_summary['configured_sources']}\n"
                    f"     Sources successful : {google_summary['successful_sources']}\n"
                    f"     RawFetch rows       : {google_summary['raw_fetches_created']}\n"
                ))
                if google_summary["stage_status"] == "partial":
                    had_partial_stage = True

            # ── Stage 2: NORMALISE ───────────────────────────────────────
            if stage in ("all", "normalise"):
                self.stdout.write(self.style.HTTP_INFO("\n> Stage 2 - NORMALISE (parsing RawFetch into KeywordObservations...)"))
                normalise_summary = run_stage_normalise(run)
                self.stdout.write(self.style.SUCCESS(
                    f"  [OK] NORMALISE complete\n"
                    f"     RawFetch rows processed : {normalise_summary['raw_fetches_processed']}\n"
                    f"     KeywordObservations     : {normalise_summary['observations_created']}\n"
                    f"     Existing rows updated  : {normalise_summary['observations_updated']}\n"
                    f"     Skipped (blank kws)     : {normalise_summary['observations_skipped']}\n"
                ))

            # -- Stage 2b: ANALYTICS --------------------------------------
            if stage in ("all", "analytics"):
                self.stdout.write(self.style.HTTP_INFO(
                    "\n> Stage 2b - ANALYTICS (normalising GSC/GA4 data...)"
                ))
                analytics_summary = run_stage_analytics(run)
                self.stdout.write(self.style.SUCCESS(
                    f"  [OK] ANALYTICS complete\n"
                    f"     RawFetch rows       : {analytics_summary['raw_fetches_processed']}\n"
                    f"     GSC snapshots new   : {analytics_summary['gsc']['snapshots_created']}\n"
                    f"     GSC observations    : {analytics_summary['gsc']['observations_created']}\n"
                    f"     GA4 pages updated   : {analytics_summary['ga4']['pages_updated']}\n"
                ))

            # ── Stage 3: ENRICH ──────────────────────────────────────────
            if stage in ("all", "enrich"):
                self.stdout.write(self.style.HTTP_INFO(
                    "\n> Stage 3 -- ENRICH "
                    "(difficulty labels + intent + SERP features...)"
                ))
                enrich_summary = run_stage_enrich(run, max_serp_calls=serp_calls)
                self.stdout.write(self.style.SUCCESS(
                    f"  [OK] ENRICH complete\n"
                    f"     Keywords processed   : {enrich_summary['total_observations']}\n"
                    f"     With difficulty score : {enrich_summary['difficulty_enriched']}\n"
                    f"     Intent tagged         : {enrich_summary['intent_enriched']}\n"
                    f"     SERP enriched         : {enrich_summary['serp_enriched']}\n"
                ))

            # -- Stage 4: CLUSTER ------------------------------------------
            if stage in ("all", "cluster"):
                self.stdout.write(self.style.HTTP_INFO(
                    "\n> Stage 4 -- CLUSTER "
                    "(grouping keywords into topics...)"
                ))
                cluster_summary = run_stage_cluster(run)
                self.stdout.write(self.style.SUCCESS(
                    f"  [OK] CLUSTER complete\n"
                    f"     Unique keywords     : {cluster_summary['unique_keywords']}\n"
                    f"     Topics created      : {cluster_summary['topics_created']}\n"
                    f"     Topics reconciled   : {cluster_summary['topics_updated']}\n"
                    f"     Keyword assignments : {cluster_summary['topic_keywords_created']}\n"
                ))


            # Variables to pass state between stages
            match_results = {}

            # -- Stage 5: MATCH --------------------------------------------
            if stage in ("all", "match"):
                self.stdout.write(self.style.HTTP_INFO(
                    "\n" + chr(9654) + " Stage 5 -- MATCH "
                    "(checking for existing client pages...)"
                ))
                match_summary = run_stage_match(run)
                match_results = match_summary.get("match_results", {})
                self.stdout.write(self.style.SUCCESS(
                    f"  " + chr(9989) + f" MATCH complete\n"
                    f"     Topics checked : {match_summary['total_topics']}\n"
                    f"     Matched        : {match_summary['matched']}\n"
                    f"     Unmatched      : {match_summary['unmatched']}\n"
                ))

            # -- Stage 6: DECIDE -------------------------------------------
            if stage in ("all", "decide"):
                self.stdout.write(self.style.HTTP_INFO(
                    "\n" + chr(9654) + " Stage 6 -- DECIDE "
                    "(recommending actions and URLs...)"
                ))
                decide_summary = run_stage_decide(run, match_results=match_results)
                self.stdout.write(self.style.SUCCESS(
                    f"  " + chr(9989) + f" DECIDE complete\n"
                    f"     New content    : {decide_summary['new_content']}\n"
                    f"     Optimise       : {decide_summary['optimise']}\n"
                    f"     Merge          : {decide_summary['merge']}\n"
                    f"     Ignored        : {decide_summary['ignore']}\n"
                ))

            # -- Stage 8: SCORE --------------------------------------------
            if stage in ("all", "score"):
                self.stdout.write(self.style.HTTP_INFO(
                    "\n" + chr(9654) + " Stage 8 -- SCORE "
                    "(calculating priority scores...)"
                ))
                score_summary = run_stage_score(run)
                self.stdout.write(self.style.SUCCESS(
                    f"  " + chr(9989) + f" SCORE complete\n"
                    f"     Scored         : {score_summary['scored']}\n"
                    f"     Ignored        : {score_summary['ignored']}\n"
                ))

            # -- Stage 9: EXPORT ------------------------------------------
            if stage in ("all", "export"):
                self.stdout.write(self.style.HTTP_INFO(
                    "\n" + chr(9654) + " Stage 9 -- EXPORT "
                    "(merge-writing the Google Sheets deliverable...)"
                ))
                export_summary = run_stage_export(run)
                self.stdout.write(self.style.SUCCESS(
                    f"  " + chr(9989) + f" EXPORT complete\n"
                    f"     Opportunities : {export_summary['opportunities']}\n"
                    f"     Ignored       : {export_summary['ignored']}\n"
                    f"     Merge URLs    : {export_summary['cannibalisation_rows']}\n"
                    f"     Archived      : {export_summary['archived']}\n"
                ))

            # -- All done --------------------------------------------------
            # A stage that completed with missing markets must never be promoted
            # to a fully successful run.
            run.status = "partial" if had_partial_stage else "complete"
            run.finished_at = timezone.now()
            run.error = "" if not had_partial_stage else "One or more ingestion markets failed; inspect RunStage records."
            cost_agg = RawFetch.objects.filter(run=run).aggregate(
                total=Sum("cost_usd")
            )
            run.total_cost_usd = cost_agg["total"] or 0
            run.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "error",
                    "total_cost_usd",
                ]
            )

            self.stdout.write(self.style.SUCCESS(
                f"\n{'='*60}"
                f"\n[OK] Pipeline finished for Run #{run.pk} with status: {run.status}"
                f"\n   Total cost: ${float(run.total_cost_usd):.4f} USD"
                f"\n   Check Django Admin to inspect Opportunities!"
                f"\n{'='*60}\n"
            ))

        except Exception as e:
            # Mark the run as failed and record the error
            run.status = "failed"
            run.finished_at = timezone.now()
            run.error = str(e)
            cost_agg = RawFetch.objects.filter(run=run).aggregate(
                total=Sum("cost_usd")
            )
            run.total_cost_usd = cost_agg["total"] or 0
            run.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "error",
                    "total_cost_usd",
                ]
            )

            self.stdout.write(self.style.ERROR(
                f"\n[FAILED] Pipeline failed for Run #{run.pk}\n"
                f"   Error: {e}\n"
                f"   The Run has been marked 'failed' in admin.\n"
            ))
            raise CommandError(str(e))

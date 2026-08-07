from django.db import models


class Run(models.Model):
    """
    One execution of the pipeline.
    
    When an admin wants to start the engine, they create a Run:
    - Select the client
    - Choose the run type (full / gsc_delta / rescore)
    - Pick which markets to run for
    - Paste in seed keywords (comma-separated text)
    - Hit save → pipeline starts
    
    `settings_snapshot` captures the exact thresholds used, so 6 weeks later
    we can explain why a topic scored 84 in June but 61 in July.
    """
    client = models.ForeignKey(
        "clients.Client", on_delete=models.CASCADE, related_name="runs"
    )
    run_type = models.CharField(
        max_length=20,
        choices=[
            ("full", "Full Run"),
            ("gsc_delta", "GSC Delta (Weekly)"),
            ("rescore", "Rescore Only"),
        ],
        default="full",
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("running", "Running"),
            ("complete", "Complete"),
            ("failed", "Failed"),
            ("partial", "Partial"),
        ],
        default="pending",
    )

    # --- What this run will process ---
    markets = models.JSONField(
        default=list,
        help_text='List of market codes to include, e.g. ["UK", "DE"]',
    )
    seed_keywords = models.TextField(
        blank=True,
        help_text="Comma-separated seed keywords for DataForSEO keyword research.",
    )
    competitor_domains = models.TextField(
        blank=True,
        help_text=(
            "Comma-separated competitor domains for gap analysis. "
            "Leave blank to use the competitors already configured in the Market."
        ),
    )

    # --- Reproducibility ---
    settings_snapshot = models.JSONField(
        default=dict,
        help_text="Frozen copy of EngineSettings + ScoringWeights at run start.",
    )

    # --- Tracking ---
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    total_cost_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=0,
        help_text="Accumulated DataForSEO spend for this run.",
    )
    sheet_url = models.URLField(blank=True, help_text="Link to the output Google Sheet.")
    error = models.TextField(blank=True, help_text="Error details if the run failed.")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Run #{self.pk} — {self.client.name} ({self.run_type}) [{self.status}]"

    def get_seed_keywords_list(self):
        """Parse the comma-separated seed keywords into a clean Python list."""
        if not self.seed_keywords:
            return []
        return [kw.strip() for kw in self.seed_keywords.split(",") if kw.strip()]

    def get_competitor_domains_list(self):
        """Parse the comma-separated competitor domains into a clean Python list."""
        if not self.competitor_domains:
            return []
        return [d.strip() for d in self.competitor_domains.split(",") if d.strip()]


class RunStage(models.Model):
    """
    Tracks each stage of the pipeline within a Run.
    
    The pipeline has 10 stages (0-9):
      0=PLAN, 1=INGEST, 2=NORMALISE, 3=ENRICH, 4=CLUSTER,
      5=MATCH, 6=DECIDE, 7=CROSS-MARKET, 8=SCORE, 9=EXPORT
    
    records_in / records_out make silent data loss visible — if a stage
    ingests 80k keywords and emits 200 topics, only these counts tell us
    whether that's aggressive-but-correct clustering or a bug.
    """
    STAGE_CHOICES = [
        ("plan", "Stage 0 — Plan"),
        ("ingest", "Stage 1 — Ingest"),
        ("normalise", "Stage 2 — Normalise"),
        ("enrich", "Stage 3 — Enrich"),
        ("cluster", "Stage 4 — Cluster"),
        ("match", "Stage 5 — Match"),
        ("decide", "Stage 6 — Decide"),
        ("cross_market", "Stage 7 — Cross-Market"),
        ("score", "Stage 8 — Score"),
        ("export", "Stage 9 — Export"),
    ]

    run = models.ForeignKey(Run, related_name="stages", on_delete=models.CASCADE)
    name = models.CharField(max_length=40, choices=STAGE_CHOICES)
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("running", "Running"),
            ("complete", "Complete"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
        ],
        default="pending",
    )
    records_in = models.IntegerField(default=0)
    records_out = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["run", "pk"]
        unique_together = [("run", "name")]

    def __str__(self):
        return f"{self.get_name_display()} [{self.status}]"

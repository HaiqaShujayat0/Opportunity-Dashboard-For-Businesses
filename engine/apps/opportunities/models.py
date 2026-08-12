from django.db import models



class Opportunity(models.Model):
    """
    One row in the output Google Sheet — the final artifact of the whole pipeline.
    
    Every Topic gets run through the Decision Engine (Stage 6) and Scoring Engine (Stage 8)
    to produce exactly one Opportunity record per run.
    """
    run = models.ForeignKey(
        "runs.Run", on_delete=models.CASCADE, related_name="opportunities"
    )
    topic = models.ForeignKey(
        "topics.Topic", on_delete=models.CASCADE, related_name="opportunities"
    )
    market = models.ForeignKey(
        "clients.Market", on_delete=models.CASCADE, related_name="opportunities"
    )

    # --- The Core Recommendation ---
    action = models.CharField(
        max_length=20,
        choices=[
            ("new_content", "New Content"),
            ("optimise", "Optimise"),
            ("merge", "Merge (Cannibalisation)"),
            ("ignore", "Ignore"),
        ],
    )
    # If action is 'optimise' this is 1 URL. If 'merge' this is 2+ URLs. If 'new_content' it is empty.
    target_urls = models.JSONField(default=list)
    
    # List of signals (e.g., ["competitor_gap", "quick_win"]), ordered by strength
    why_flagged = models.JSONField(default=list)

    # --- Metrics ---
    current_position = models.FloatField(null=True, blank=True)
    previous_position = models.FloatField(null=True, blank=True)
    difficulty = models.CharField(max_length=20, blank=True)
    difficulty_score = models.IntegerField(null=True, blank=True)
    
    # --- Content Execution Details ---
    page_type = models.CharField(max_length=32, blank=True)
    suggested_slug = models.CharField(max_length=300, blank=True)
    
    # --- Commercials ---
    conversion_potential = models.CharField(
        max_length=10, blank=True,
        choices=[("High", "High"), ("Medium", "Medium"), ("Low", "Low")]
    )
    conversion_basis = models.CharField(
        max_length=20, blank=True, 
        choices=[("data", "GA4 Data"), ("inferred", "Inferred"), ("unknown", "Unknown")]
    )
    
    competitor_url = models.URLField(max_length=1000, blank=True)
    ai_search_opportunity = models.BooleanField(null=True, blank=True)
    estimated_impact = models.JSONField(
        null=True, blank=True,
        help_text="Expected traffic/revenue gain if target position is reached."
    )
    
    # --- The Score ---
    priority_score = models.FloatField(null=True, blank=True)
    confidence = models.FloatField(
        null=True, blank=True,
        help_text="Engine's confidence in this recommendation (0.0 to 1.0)."
    )
    
    # --- Audit Trail ---
    decision_trace = models.JSONField(
        default=dict,
        help_text="A complete log of WHY the engine chose this action. Used for debugging."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # A single pipeline run can only have ONE opportunity row per topic
        unique_together = [("run", "topic")]
        # We always want to read these sorted by highest priority score
        indexes = [models.Index(fields=["run", "-priority_score"])]
        verbose_name_plural = "Opportunities"

    def __str__(self):
        return f"{self.action.upper()} - {self.topic.label} ({self.priority_score})"

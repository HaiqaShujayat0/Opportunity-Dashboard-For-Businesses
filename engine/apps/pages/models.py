from django.db import models


class ExistingPage(models.Model):
    """
    Every URL found on the client's site (via sitemap crawl and GSC).
    
    This model serves two massive purposes:
    1. It's the baseline. When the engine finds a new keyword topic, it compares it
       against all ExistingPages to see if we already have content for it. If yes,
       the action is "Optimise". If no, it's "New content".
    2. It holds the GSC & GA4 traffic/conversion data. This gives us our 
       "Conversion Potential" rating.
    """
    market = models.ForeignKey(
        "clients.Market", on_delete=models.CASCADE, related_name="existing_pages"
    )

    # --- Page Identity & Content ---
    url = models.URLField(max_length=1000)
    path = models.CharField(max_length=1000, db_index=True)
    title = models.CharField(max_length=500, blank=True)
    h1 = models.CharField(max_length=500, blank=True)
    meta_description = models.TextField(blank=True)
    
    # category is blank by default until we build the taxonomy matcher (Task 6.4)
    category = models.CharField(max_length=200, blank=True)     
    page_type = models.CharField(max_length=32, blank=True)
    last_modified = models.DateTimeField(null=True, blank=True)
    in_sitemap = models.BooleanField(default=True)

    # --- GSC Performance (Last 28 Days) ---
    total_clicks_28d = models.IntegerField(default=0)
    total_impressions_28d = models.IntegerField(default=0)
    ranking_keyword_count = models.IntegerField(default=0)

    # --- GA4 Conversions (Last 28 Days) ---
    sessions_28d = models.IntegerField(default=0)
    conversions_28d = models.IntegerField(default=0)
    conversion_rate = models.FloatField(null=True, blank=True)
    revenue_28d = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    # --- Embeddings for semantic matching ---
    # In production: embedding = VectorField(dimensions=768, null=True)
    # Using BinaryField temporarily for SQLite support
    embedding_blob = models.BinaryField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("market", "url")]

    def __str__(self):
        return self.path


class PositionSnapshot(models.Model):
    """Finalized daily GSC position data retained across pipeline runs."""

    market = models.ForeignKey(
        "clients.Market", on_delete=models.CASCADE, related_name="position_snapshots"
    )
    last_seen_run = models.ForeignKey(
        "runs.Run", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="position_snapshots",
    )
    observed_on = models.DateField()
    keyword = models.CharField(max_length=500)
    keyword_normalised = models.CharField(max_length=500, db_index=True)
    page_url = models.URLField(max_length=1000)
    country = models.CharField(max_length=3)
    clicks = models.FloatField(default=0)
    impressions = models.FloatField(default=0)
    ctr = models.FloatField(default=0)
    position = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["market", "observed_on", "keyword_normalised", "page_url", "country"],
                name="unique_daily_gsc_position",
            )
        ]
        indexes = [
            models.Index(fields=["market", "keyword_normalised", "observed_on"]),
            models.Index(fields=["market", "page_url", "observed_on"]),
        ]

    def __str__(self):
        return f"{self.keyword} @ {self.position} ({self.observed_on})"

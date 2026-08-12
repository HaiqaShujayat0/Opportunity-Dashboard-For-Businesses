from django.db import models


class RawFetch(models.Model):
    """
    Immutable snapshot of one external API call. Never mutated, never deleted early.
    
    Think of this as a "receipt" — every time we call DataForSEO, GSC, GA4, 
    or crawl a sitemap, we save the EXACT raw JSON response here.
    
    Why this matters:
    - If the pipeline crashes at Stage 4 (clustering), we can restart from 
      the saved data without paying DataForSEO again.
    - `request_hash` enables caching: if we made the exact same API call 
      within a configurable time window, we reuse this row instead of 
      paying for a duplicate call.
    """
    SOURCE_CHOICES = [
        ("dataforseo", "DataForSEO"),
        ("gsc", "Google Search Console"),
        ("ga4", "Google Analytics 4"),
        ("sitemap", "Sitemap Crawler"),
    ]

    run = models.ForeignKey(
        "runs.Run", on_delete=models.CASCADE, related_name="raw_fetches"
    )
    market = models.ForeignKey(
        "clients.Market", on_delete=models.CASCADE, related_name="raw_fetches"
    )
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES)
    endpoint = models.CharField(
        max_length=128,
        help_text="API endpoint called, e.g. /v3/dataforseo_labs/google/keyword_ideas/live",
    )
    request_params = models.JSONField(
        help_text="The exact parameters sent to the API.",
    )
    request_hash = models.CharField(
        max_length=64, db_index=True,
        help_text="SHA-256 hash of (source + endpoint + sorted params). Used for cache lookups.",
    )
    payload = models.JSONField(
        help_text="The raw API response JSON. Immutable — never modified after creation.",
    )
    cost_usd = models.DecimalField(
        max_digits=10, decimal_places=5, default=0,
        help_text="Cost of this specific API call in USD.",
    )
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["market", "source", "fetched_at"]),
            models.Index(fields=["request_hash", "fetched_at"]),
        ]
        verbose_name_plural = "Raw fetches"

    def __str__(self):
        return f"RawFetch [{self.source}] {self.endpoint} @ {self.fetched_at:%Y-%m-%d %H:%M}"


class KeywordObservation(models.Model):
    """
    One keyword as seen by one source in one market at one point in time.
    
    A single keyword like "running shoes" can produce MULTIPLE observation 
    rows — and that's intentional:
      - Found via DataForSEO keyword research → signal = "keyword_research"
      - We currently rank for it in GSC → signal = "quick_win"
      - A competitor ranks for it but we don't → signal = "competitor_gap"
    
    The union of all signals for a keyword is exactly what populates the 
    "Why Flagged" column (column 11) in the final Google Sheet. Collapsing 
    observations early would lose that traceability permanently.
    """
    SIGNAL_CHOICES = [
        ("keyword_research", "Keyword Research"),
        ("competitor_gap", "Competitor Gap"),
        ("competitor_top_page", "Competitor Top Page"),
        ("quick_win", "Quick Win (Position 7-20)"),
        ("ranking_decay", "Ranking Decay"),
        ("existing_ranking", "Existing Ranking"),
        ("conversion_proven", "Conversion Proven"),
        ("cross_market", "Proven in Other Market"),
    ]

    run = models.ForeignKey(
        "runs.Run", on_delete=models.CASCADE, related_name="keyword_observations"
    )
    market = models.ForeignKey(
        "clients.Market", on_delete=models.CASCADE, related_name="keyword_observations"
    )

    # --- The keyword itself ---
    keyword = models.CharField(max_length=500, db_index=True)
    keyword_normalised = models.CharField(
        max_length=500, db_index=True,
        help_text="Lowercased, stopwords removed, lemmatised, tokens sorted.",
    )
    source = models.CharField(max_length=32)   # dataforseo | gsc | ga4 | sitemap
    signal = models.CharField(
        max_length=40, choices=SIGNAL_CHOICES,
        help_text="Why this keyword was flagged — maps to column 11 (Why Flagged).",
    )

    # --- Volume & difficulty metrics (from DataForSEO) ---
    search_volume = models.IntegerField(null=True, blank=True)
    cpc = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    keyword_difficulty = models.IntegerField(null=True, blank=True)
    competition = models.FloatField(null=True, blank=True)

    # --- Our own ranking data (from GSC) ---
    our_position = models.FloatField(null=True, blank=True)
    previous_position = models.FloatField(null=True, blank=True)
    our_url = models.URLField(max_length=1000, blank=True)
    impressions = models.IntegerField(null=True, blank=True)
    clicks = models.IntegerField(null=True, blank=True)
    ctr = models.FloatField(null=True, blank=True)

    # --- Competitor data (from DataForSEO gap analysis) ---
    competitor_domain = models.CharField(max_length=255, blank=True)
    competitor_url = models.URLField(max_length=1000, blank=True)
    competitor_position = models.FloatField(null=True, blank=True)

    # --- SERP data (from DataForSEO SERP calls — shortlisted keywords only) ---
    serp_features = models.JSONField(
        default=list,
        help_text='SERP features present, e.g. ["ai_overview", "featured_snippet", "paa"]',
    )
    serp_top_urls = models.JSONField(
        default=list,
        help_text="Top 10 URLs from the SERP — used for clustering (SERP overlap).",
    )
    intent = models.CharField(
        max_length=20, blank=True,
        help_text="informational | commercial | transactional | navigational",
    )

    # --- Embedding (placeholder — will use pgvector when we switch to PostgreSQL) ---
    # In production: embedding = VectorField(dimensions=768, null=True)
    # For now with SQLite, we store as binary. Will swap when we move to PostgreSQL.
    embedding_blob = models.BinaryField(
        null=True, blank=True,
        help_text="768-dim embedding stored as bytes. Will become a pgvector VectorField.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["run", "market", "keyword_normalised"]),
            models.Index(fields=["market", "signal"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "run",
                    "market",
                    "keyword_normalised",
                    "source",
                    "signal",
                    "competitor_domain",
                    "our_url",
                ],
                name="unique_keyword_observation_identity",
            ),
        ]

    def __str__(self):
        return f"{self.keyword} [{self.signal}] — {self.market.code}"

from django.db import models

class Client(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    primary_domain = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Market(models.Model):
    """One row per (client, country, language). UK, DE, FR, NL to start (per requirements)."""
    client = models.ForeignKey(Client, related_name="markets", on_delete=models.CASCADE)
    code = models.CharField(max_length=10)              # e.g., "UK", "DE"
    country_iso = models.CharField(max_length=2)         # e.g., "GB", "DE"
    language_code = models.CharField(max_length=5)       # e.g., "en", "de"
    
    # API specific configurations for this market
    dataforseo_location_code = models.IntegerField()     # e.g., 2826 = United Kingdom
    gsc_property = models.CharField(max_length=255)      # sc-domain:... or URL prefix
    ga4_property_id = models.CharField(max_length=50, blank=True)
    sitemap_url = models.URLField()
    url_pattern = models.CharField(max_length=255, blank=True)  # "/de/" or subdomain
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("client", "code")]

    def __str__(self):
        return f"{self.client.name} - {self.code}"


class Competitor(models.Model):
    market = models.ForeignKey(Market, related_name="competitors", on_delete=models.CASCADE)
    domain = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)   # primary set drives gap analysis

    def __str__(self):
        return f"{self.domain} ({self.market.code})"

class EngineSettings(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    market = models.ForeignKey(Market, null=True, blank=True, on_delete=models.CASCADE)
    # null market = client-wide default

    # --- Dynamic Category Extraction ---
    category_extraction_regex = models.CharField(
        max_length=255, 
        default=r"^/([^/]+)/",
        help_text="Regex to extract category from URL. Default grabs the first folder path."
    )

    # --- explicitly required by the client ---
    quick_win_min_position = models.FloatField(default=7.0)
    quick_win_max_position = models.FloatField(default=20.0)
    decay_baseline_max_position = models.FloatField(default=5.0)   
    decay_current_min_position = models.FloatField(default=5.0)    
    decay_min_drop = models.FloatField(default=3.0)                
    decay_baseline_days = models.IntegerField(default=90)
    decay_comparison_days = models.IntegerField(default=28)

    # --- discovery ---
    min_search_volume = models.IntegerField(default=50)
    min_keywords_per_topic = models.IntegerField(default=1)
    max_keyword_difficulty = models.IntegerField(default=100)

    # --- clustering ---
    serp_overlap_threshold = models.IntegerField(default=3)   
    semantic_similarity_threshold = models.FloatField(default=0.82)
    clustering_linkage = models.CharField(max_length=20, default="complete")

    # --- matching / cannibalisation ---
    existing_page_match_threshold = models.FloatField(default=0.75)
    cannibalisation_min_pages = models.IntegerField(default=2)

    # --- output ---
    max_rows_per_run = models.IntegerField(default=500)
    include_ignored_rows = models.BooleanField(default=True)

    # --- cost guardrails ---
    max_serp_calls_per_run = models.IntegerField(default=5000)
    max_spend_per_run_usd = models.DecimalField(max_digits=8, decimal_places=2, default=100)


class ScoringWeights(models.Model):
    """Priority score weights, separated so they can be tuned without touching thresholds."""
    client = models.OneToOneField(Client, on_delete=models.CASCADE)
    w_volume = models.FloatField(default=0.25)
    w_position_opportunity = models.FloatField(default=0.20)
    w_conversion = models.FloatField(default=0.20)
    w_difficulty = models.FloatField(default=0.15)
    w_signal = models.FloatField(default=0.10)
    w_market = models.FloatField(default=0.10)
    signal_weights = models.JSONField(default=dict)   
    market_weights = models.JSONField(default=dict)   

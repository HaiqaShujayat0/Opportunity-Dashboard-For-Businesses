from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from .models import Client, Market, Competitor, EngineSettings, ScoringWeights


class MarketInline(TabularInline):
    model = Market
    extra = 1
    show_change_link = True


class CompetitorInline(TabularInline):
    model = Competitor
    extra = 1


@admin.register(Client)
class ClientAdmin(ModelAdmin):
    list_display = (
        "name",
        "slug",
        "primary_domain",
        "google_sheets_spreadsheet_id",
        "is_active",
    )
    search_fields = ("name", "primary_domain")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MarketInline]


@admin.register(Market)
class MarketAdmin(ModelAdmin):
    list_display = ("client", "code", "country_iso", "language_code", "dataforseo_location_code", "is_active")
    list_filter = ("client", "is_active")
    inlines = [CompetitorInline]


@admin.register(Competitor)
class CompetitorAdmin(ModelAdmin):
    list_display = ("domain", "market", "is_primary")
    list_filter = ("market", "is_primary")


@admin.register(EngineSettings)
class EngineSettingsAdmin(ModelAdmin):
    list_display = ("client", "market", "min_search_volume", "max_spend_per_run_usd", "serp_overlap_threshold")
    list_filter = ("client",)
    fieldsets = (
        ("Client & market", {"fields": ("client", "market")}),
        (
            "Category extraction",
            {"fields": ("category_extraction_regex",)},
        ),
        (
            "Quick win thresholds",
            {"fields": ("quick_win_min_position", "quick_win_max_position")},
        ),
        (
            "Decay detection",
            {
                "fields": (
                    "decay_baseline_max_position",
                    "decay_current_min_position",
                    "decay_min_drop",
                    "decay_baseline_days",
                    "decay_comparison_days",
                )
            },
        ),
        (
            "Keyword filters",
            {
                "fields": (
                    "min_search_volume",
                    "min_keywords_per_topic",
                    "max_keyword_difficulty",
                )
            },
        ),
        (
            "Clustering",
            {
                "fields": (
                    "serp_overlap_threshold",
                    "semantic_similarity_threshold",
                    "clustering_linkage",
                ),
                "description": (
                    "Note: semantic_similarity_threshold and clustering_linkage "
                    "are stored but not yet active in the pipeline."
                ),
            },
        ),
        (
            "Page matching",
            {
                "fields": (
                    "existing_page_match_threshold",
                    "cannibalisation_min_pages",
                )
            },
        ),
        (
            "Spend & row limits",
            {
                "fields": (
                    "max_rows_per_run",
                    "include_ignored_rows",
                    "max_serp_calls_per_run",
                    "max_spend_per_run_usd",
                )
            },
        ),
    )


@admin.register(ScoringWeights)
class ScoringWeightsAdmin(ModelAdmin):
    list_display = ("client", "w_volume", "w_position_opportunity", "w_conversion", "w_difficulty")

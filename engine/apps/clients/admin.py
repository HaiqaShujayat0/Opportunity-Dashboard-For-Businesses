from django.contrib import admin
from .models import Client, Market, Competitor, EngineSettings, ScoringWeights


class MarketInline(admin.TabularInline):
    model = Market
    extra = 1
    show_change_link = True


class CompetitorInline(admin.TabularInline):
    model = Competitor
    extra = 1


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "primary_domain", "is_active")
    search_fields = ("name", "primary_domain")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MarketInline]


@admin.register(Market)
class MarketAdmin(admin.ModelAdmin):
    list_display = ("client", "code", "country_iso", "language_code", "dataforseo_location_code", "is_active")
    list_filter = ("client", "is_active")
    inlines = [CompetitorInline]


@admin.register(Competitor)
class CompetitorAdmin(admin.ModelAdmin):
    list_display = ("domain", "market", "is_primary")
    list_filter = ("market", "is_primary")


@admin.register(EngineSettings)
class EngineSettingsAdmin(admin.ModelAdmin):
    list_display = ("client", "market", "min_search_volume", "max_spend_per_run_usd", "serp_overlap_threshold")
    list_filter = ("client",)


@admin.register(ScoringWeights)
class ScoringWeightsAdmin(admin.ModelAdmin):
    list_display = ("client", "w_volume", "w_position_opportunity", "w_conversion", "w_difficulty")

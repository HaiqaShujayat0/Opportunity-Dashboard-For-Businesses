from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from .models import RawFetch, KeywordObservation

@admin.register(RawFetch)
class RawFetchAdmin(ModelAdmin):
    list_display = ("source", "endpoint", "market", "cost_usd", "fetched_at")
    list_filter = ("source", "market")
    readonly_fields = (
        "run",
        "market",
        "source",
        "endpoint",
        "request_params",
        "request_hash",
        "payload",
        "cost_usd",
        "fetched_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(KeywordObservation)
class KeywordObservationAdmin(ModelAdmin):
    list_display = ("keyword", "market", "source", "signal", "search_volume")
    list_filter = ("source", "signal", "market")
    search_fields = ("keyword",)
    readonly_fields = (
        "run",
        "market",
        "keyword",
        "keyword_normalised",
        "source",
        "signal",
        "search_volume",
        "cpc",
        "keyword_difficulty",
        "competition",
        "our_position",
        "previous_position",
        "our_url",
        "impressions",
        "clicks",
        "ctr",
        "competitor_domain",
        "competitor_url",
        "competitor_position",
        "serp_features",
        "serp_top_urls",
        "intent",
        "embedding_blob",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from .models import ExistingPage, PositionSnapshot

@admin.register(ExistingPage)
class ExistingPageAdmin(ModelAdmin):
    list_display = ("path", "market", "category", "in_sitemap", "total_clicks_28d")
    list_filter = ("market", "in_sitemap", "category")
    search_fields = ("path", "title")
    readonly_fields = (
        "market",
        "url",
        "path",
        "title",
        "h1",
        "meta_description",
        "category",
        "page_type",
        "last_modified",
        "in_sitemap",
        "total_clicks_28d",
        "total_impressions_28d",
        "ranking_keyword_count",
        "sessions_28d",
        "conversions_28d",
        "conversion_rate",
        "revenue_28d",
        "embedding_blob",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PositionSnapshot)
class PositionSnapshotAdmin(ModelAdmin):
    list_display = ("keyword", "market", "observed_on", "position", "page_url")
    list_filter = ("market", "observed_on", "country")
    search_fields = ("keyword", "keyword_normalised", "page_url")
    readonly_fields = (
        "market",
        "last_seen_run",
        "observed_on",
        "keyword",
        "keyword_normalised",
        "page_url",
        "country",
        "clicks",
        "impressions",
        "ctr",
        "position",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

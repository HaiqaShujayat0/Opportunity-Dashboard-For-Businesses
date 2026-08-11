from django.contrib import admin
from .models import ExistingPage, PositionSnapshot

@admin.register(ExistingPage)
class ExistingPageAdmin(admin.ModelAdmin):
    list_display = ("path", "market", "category", "in_sitemap", "total_clicks_28d")
    list_filter = ("market", "in_sitemap", "category")
    search_fields = ("path", "title")


@admin.register(PositionSnapshot)
class PositionSnapshotAdmin(admin.ModelAdmin):
    list_display = ("keyword", "market", "observed_on", "position", "page_url")
    list_filter = ("market", "observed_on", "country")
    search_fields = ("keyword", "keyword_normalised", "page_url")

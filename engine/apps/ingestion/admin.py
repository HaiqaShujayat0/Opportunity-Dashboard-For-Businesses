from django.contrib import admin
from .models import RawFetch, KeywordObservation

@admin.register(RawFetch)
class RawFetchAdmin(admin.ModelAdmin):
    list_display = ("source", "endpoint", "market", "cost_usd", "fetched_at")
    list_filter = ("source", "market")
    readonly_fields = ("payload", "request_params", "request_hash")

@admin.register(KeywordObservation)
class KeywordObservationAdmin(admin.ModelAdmin):
    list_display = ("keyword", "market", "source", "signal", "search_volume")
    list_filter = ("source", "signal", "market")
    search_fields = ("keyword",)

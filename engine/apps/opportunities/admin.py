from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from .models import Opportunity

@admin.register(Opportunity)
class OpportunityAdmin(ModelAdmin):
    list_display = ("topic", "market", "action", "priority_score", "current_position")
    list_filter = ("action", "market", "conversion_potential")
    search_fields = ("topic__label", "suggested_slug")
    readonly_fields = (
        "run",
        "topic",
        "market",
        "action",
        "target_urls",
        "why_flagged",
        "current_position",
        "previous_position",
        "difficulty",
        "difficulty_score",
        "page_type",
        "suggested_slug",
        "conversion_potential",
        "conversion_basis",
        "competitor_url",
        "ai_search_opportunity",
        "estimated_impact",
        "priority_score",
        "confidence",
        "decision_trace",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

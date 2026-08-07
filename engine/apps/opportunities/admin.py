from django.contrib import admin
from .models import Opportunity

@admin.register(Opportunity)
class OpportunityAdmin(admin.ModelAdmin):
    list_display = ("topic", "market", "action", "priority_score", "current_position")
    list_filter = ("action", "market", "conversion_potential")
    search_fields = ("topic__label", "suggested_slug")
    readonly_fields = ("created_at",)

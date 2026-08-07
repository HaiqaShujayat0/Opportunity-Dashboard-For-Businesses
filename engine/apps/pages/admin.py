from django.contrib import admin
from .models import ExistingPage

@admin.register(ExistingPage)
class ExistingPageAdmin(admin.ModelAdmin):
    list_display = ("path", "market", "category", "in_sitemap", "total_clicks_28d")
    list_filter = ("market", "in_sitemap", "category")
    search_fields = ("path", "title")

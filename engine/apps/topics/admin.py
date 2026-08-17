from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from .models import Topic, TopicKeyword

class TopicKeywordInline(TabularInline):
    model = TopicKeyword
    extra = 0
    readonly_fields = (
        "keyword",
        "search_volume",
        "is_primary",
        "our_position",
        "keyword_difficulty",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Topic)
class TopicAdmin(ModelAdmin):
    list_display = ("label", "market", "primary_keyword", "total_search_volume", "intent", "category")
    list_filter = ("market", "category")
    search_fields = ("label", "primary_keyword")
    inlines = [TopicKeywordInline]
    readonly_fields = (
        "client",
        "market",
        "topic_uid",
        "label",
        "primary_keyword",
        "primary_keyword_volume",
        "total_search_volume",
        "category",
        "intent",
        "cross_market_group",
        "first_seen_run",
        "last_seen_run",
        "centroid_blob",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

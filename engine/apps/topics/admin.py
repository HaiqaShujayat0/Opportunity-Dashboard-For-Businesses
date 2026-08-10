from django.contrib import admin
from .models import Topic, TopicKeyword

class TopicKeywordInline(admin.TabularInline):
    model = TopicKeyword
    extra = 0
    readonly_fields = ("keyword", "search_volume", "is_primary", "our_position")

@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("label", "market", "primary_keyword", "total_search_volume", "intent", "category")
    list_filter = ("market", "category")
    search_fields = ("label", "primary_keyword")
    inlines = [TopicKeywordInline]

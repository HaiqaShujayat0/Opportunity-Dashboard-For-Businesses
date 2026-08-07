from django.db import models


class Topic(models.Model):
    """
    The core unit of our entire engine. 
    
    A Topic is a cluster of keywords that have the exact same search intent. 
    (e.g., "running shoes for men", "mens running trainers", "best jogging shoes for him")
    
    Our final output is ONE row per Topic, not one row per keyword.
    """
    client = models.ForeignKey(
        "clients.Client", on_delete=models.CASCADE, related_name="topics"
    )
    market = models.ForeignKey(
        "clients.Market", on_delete=models.CASCADE, related_name="topics"
    )
    
    # topic_uid must remain perfectly stable across multiple pipeline runs.
    # If the topic "mens running shoes" gets ID 5 today, it must be the exact same
    # ID next week so we don't accidentally delete the client's notes in the Google Sheet.
    topic_uid = models.CharField(max_length=64, unique=True, db_index=True)
    
    # AI-generated, plain-english label (e.g., "Men's Running Shoes")
    label = models.CharField(max_length=300)
    
    # The highest volume keyword in the cluster
    primary_keyword = models.CharField(max_length=500)
    primary_keyword_volume = models.IntegerField(default=0)
    
    # Sum of all keyword volumes in this cluster (Deduplicated!)
    total_search_volume = models.IntegerField(default=0)
    
    category = models.CharField(max_length=200, blank=True)
    intent = models.CharField(max_length=20, blank=True)
    
    # For cross-market propagation (Task 9.4)
    cross_market_group = models.CharField(max_length=64, blank=True, db_index=True)
    
    first_seen_run = models.ForeignKey(
        "runs.Run", null=True, blank=True, on_delete=models.SET_NULL, related_name="topics_found"
    )
    
    # In production: centroid = VectorField(dimensions=768, null=True)
    centroid_blob = models.BinaryField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.label} ({self.market.code})"


class TopicKeyword(models.Model):
    """
    Every individual keyword that belongs to a Topic cluster.
    """
    topic = models.ForeignKey(
        Topic, related_name="keywords", on_delete=models.CASCADE
    )
    keyword = models.CharField(max_length=500, db_index=True)
    search_volume = models.IntegerField(default=0)
    is_primary = models.BooleanField(default=False)
    
    our_position = models.FloatField(null=True, blank=True)
    keyword_difficulty = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-is_primary", "-search_volume"]

    def __str__(self):
        return self.keyword

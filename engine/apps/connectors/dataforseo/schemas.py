"""
Pydantic v2 schemas for DataForSEO API responses.

Each schema maps the real JSON fields DataForSEO returns into typed Python objects.
We use Optional for everything — DataForSEO sometimes omits fields for low-volume keywords.

Reference: https://docs.dataforseo.com/v3/dataforseo_labs/google/
"""
from pydantic import BaseModel, field_validator
from typing import List, Optional, Any, Dict


# ---------------------------------------------------------------------------
# 1. Keyword Ideas
#    Endpoint: /v3/dataforseo_labs/google/keyword_ideas/live
# ---------------------------------------------------------------------------
class KeywordProperties(BaseModel):
    """Nested object inside KeywordIdeaItem — carries free clustering hints."""
    core_keyword: Optional[str] = None          # DataForSEO's own cluster hint
    keyword_difficulty: Optional[int] = None     # 0-100 score


class KeywordIdeaItem(BaseModel):
    keyword: str
    search_volume: Optional[int] = 0
    cpc: Optional[float] = 0.0
    competition: Optional[float] = None          # 0.0 – 1.0
    competition_level: Optional[str] = None      # LOW / MEDIUM / HIGH
    keyword_difficulty: Optional[int] = None     # top-level shortcut
    keyword_properties: Optional[KeywordProperties] = None
    # monthly_searches is a list of {year, month, search_volume} dicts
    monthly_searches: Optional[List[Dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# 2. Competitor Discovery
#    Endpoint: /v3/dataforseo_labs/google/competitors_domain/live
# ---------------------------------------------------------------------------
class DomainMetrics(BaseModel):
    organic: Optional[Dict[str, Any]] = None     # etv, count, estimated_paid_traffic_cost


class CompetitorDomainItem(BaseModel):
    domain: str
    avg_position: Optional[float] = None
    sum_position: Optional[float] = None
    intersections: Optional[int] = None          # keywords shared with our domain
    full_domain_metrics: Optional[DomainMetrics] = None


# ---------------------------------------------------------------------------
# 3. Competitor Gaps (Domain Intersection)
#    Endpoint: /v3/dataforseo_labs/google/domain_intersection/live
#    intersections=False means: keywords competitor ranks for, but WE DON'T
# ---------------------------------------------------------------------------
class KeywordData(BaseModel):
    keyword: Optional[str] = None

class DomainIntersectionItem(BaseModel):
    keyword_data: Optional[KeywordData] = None
    search_volume: Optional[int] = None
    keyword_difficulty: Optional[int] = None
    cpc: Optional[float] = None
    competition: Optional[float] = None
    # position data for the two domains compared
    first_domain_serp_element: Optional[Dict[str, Any]] = None
    second_domain_serp_element: Optional[Dict[str, Any]] = None

    @property
    def keyword(self) -> str:
        """Helper to get the nested keyword string."""
        if self.keyword_data and self.keyword_data.keyword:
            return self.keyword_data.keyword
        return ""


# ---------------------------------------------------------------------------
# 4. Competitor Top Pages (Relevant Pages)
#    Endpoint: /v3/dataforseo_labs/google/relevant_pages/live
# ---------------------------------------------------------------------------
class OrganicMetrics(BaseModel):
    etv: Optional[float] = None          # estimated traffic value
    count: Optional[int] = None          # number of keywords ranking
    avg_position: Optional[float] = None


class PageMetrics(BaseModel):
    organic: Optional[OrganicMetrics] = None


class RelevantPageItem(BaseModel):
    page_address: str
    metrics: Optional[PageMetrics] = None

    def get_etv(self) -> float:
        """Safe helper — gets estimated traffic value, returns 0 if missing."""
        if self.metrics and self.metrics.organic:
            return self.metrics.organic.etv or 0.0
        return 0.0

    def get_keyword_count(self) -> int:
        """How many keywords this competitor page ranks for."""
        if self.metrics and self.metrics.organic:
            return self.metrics.organic.count or 0
        return 0


# ---------------------------------------------------------------------------
# 5. Bulk Keyword Difficulty
#    Endpoint: /v3/dataforseo_labs/google/bulk_keyword_difficulty/live
# ---------------------------------------------------------------------------
class KeywordDifficultyItem(BaseModel):
    keyword: str
    keyword_difficulty: int              # 0-100


# ---------------------------------------------------------------------------
# 6. Advanced SERP (for Clustering — shortlisted keywords only)
#    Endpoint: /v3/serp/google/organic/live/advanced
# ---------------------------------------------------------------------------
class SerpItem(BaseModel):
    type: str                            # "organic", "paid", "featured_snippet", etc.
    rank_group: Optional[int] = None     # position in its group
    rank_absolute: Optional[int] = None  # absolute position on the SERP
    domain: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    breadcrumb: Optional[str] = None

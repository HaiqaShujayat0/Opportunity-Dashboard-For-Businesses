from pydantic import BaseModel
from typing import List, Optional, Any, Dict

# 1. Keyword Ideas
class KeywordIdeaItem(BaseModel):
    keyword: str
    search_volume: Optional[int] = 0
    cpc: Optional[float] = 0.0
    competition: Optional[str] = None

# 2. Competitor Discovery (competitors_domain)
class CompetitorDomainItem(BaseModel):
    domain: str
    
# 3. Competitor Gaps (domain_intersection)
class DomainIntersectionItem(BaseModel):
    keyword: str
    
# 4. Competitor Top Pages (relevant_pages)
class RelevantPageItem(BaseModel):
    page_address: str
    # We will extract ETV dynamically in the client since it's nested deep in metrics.organic.etv

# 5. Bulk Keyword Difficulty
class KeywordDifficultyItem(BaseModel):
    keyword: str
    keyword_difficulty: int

# 6. Advanced SERP (for Clustering)
class SerpItem(BaseModel):
    type: str
    domain: Optional[str] = None
    url: Optional[str] = None

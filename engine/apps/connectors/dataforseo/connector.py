import logging
from typing import List, Dict, Any

from apps.connectors.base import BaseConnector
from apps.connectors.dataforseo.client import DataForSEOClient
from apps.connectors.dataforseo.schemas import (
    KeywordIdeaItem, 
    CompetitorDomainItem, 
    DomainIntersectionItem,
    KeywordDifficultyItem, 
    SerpItem
)

logger = logging.getLogger(__name__)

class DataForSEOConnector(BaseConnector):
    """
    The DataForSEO Database Hook.
    Intercepts the raw JSON from the API client and saves it to the RawFetch table,
    then returns the clean Pydantic models to the engine.
    """
    source_name = "dataforseo"

    def __init__(self, run, market, login: str, password: str):
        super().__init__(run, market)
        self.client = DataForSEOClient(login, password)

    def _execute_and_log(self, endpoint: str, payload_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Core execution loop: Calls API -> Saves Raw JSON to SQLite -> Returns Raw JSON."""
        # The payload is always a list with 1 dictionary in our use cases
        params = payload_list[0] 
        
        try:
            # 1. Fetch raw json from the internet
            raw_response = self.client._post(endpoint, payload_list)
            
            # 2. Extract cost (DataForSEO returns this in the root JSON)
            cost = raw_response.get("cost", 0.0)
            
            # 3. Permanently save to SQLite (RawFetch table)
            self._log_fetch(endpoint, params, raw_response, cost_usd=cost)
            
            return raw_response
            
        except Exception as e:
            logger.exception(f"DataForSEO API failed for endpoint: {endpoint}")
            # If the API crashes (e.g. 500 error, or no money), we log the error string as the payload
            # so we have an audit trail of why the pipeline failed.
            self._log_fetch(endpoint, params, {"error": str(e)}, cost_usd=0)
            raise

    def get_keyword_ideas(self, keywords: List[str], limit: int = 100) -> List[KeywordIdeaItem]:
        endpoint = "/dataforseo_labs/google/keyword_ideas/live"
        payload = [{
            "keywords": keywords,
            "location_code": self.market.dataforseo_location_code,
            "language_name": "English",
            "limit": limit
        }]
        
        raw_response = self._execute_and_log(endpoint, payload)
        items = self.client._extract_items(raw_response)
        return [KeywordIdeaItem(**item) for item in items]
        
    def get_competitor_domains(self, target_domain: str, limit: int = 10) -> List[CompetitorDomainItem]:
        endpoint = "/dataforseo_labs/google/competitors_domain/live"
        payload = [{
            "target": target_domain,
            "location_code": self.market.dataforseo_location_code,
            "language_name": "English",
            "limit": limit
        }]
        
        raw_response = self._execute_and_log(endpoint, payload)
        items = self.client._extract_items(raw_response)
        return [CompetitorDomainItem(**item) for item in items]

    def get_domain_intersection(self, target1: str, target2: str, limit: int = 50) -> List[DomainIntersectionItem]:
        endpoint = "/dataforseo_labs/google/domain_intersection/live"
        payload = [{
            "target1": target1,
            "target2": target2,
            "location_code": self.market.dataforseo_location_code,
            "language_name": "English",
            "intersections": False,
            "limit": limit
        }]
        
        raw_response = self._execute_and_log(endpoint, payload)
        items = self.client._extract_items(raw_response)
        return [DomainIntersectionItem(**item) for item in items]
        
    def get_relevant_pages(self, target_domain: str, limit: int = 10) -> List[Dict[str, Any]]:
        endpoint = "/dataforseo_labs/google/relevant_pages/live"
        payload = [{
            "target": target_domain,
            "location_code": self.market.dataforseo_location_code,
            "language_name": "English",
            "limit": limit
        }]
        
        raw_response = self._execute_and_log(endpoint, payload)
        items = self.client._extract_items(raw_response)
        
        results = []
        for item in items:
            etv = item.get('metrics', {}).get('organic', {}).get('etv', 0)
            results.append({
                "page_address": item.get('page_address'),
                "etv": etv
            })
        return results

    def get_bulk_keyword_difficulty(self, keywords: List[str]) -> List[KeywordDifficultyItem]:
        endpoint = "/dataforseo_labs/google/bulk_keyword_difficulty/live"
        payload = [{
            "keywords": keywords,
            "location_code": self.market.dataforseo_location_code,
            "language_name": "English"
        }]
        
        raw_response = self._execute_and_log(endpoint, payload)
        items = self.client._extract_items(raw_response)
        return [KeywordDifficultyItem(**item) for item in items]

    def get_advanced_serp(self, keyword: str, depth: int = 10) -> List[SerpItem]:
        endpoint = "/serp/google/organic/live/advanced"
        payload = [{
            "keyword": keyword,
            "location_code": self.market.dataforseo_location_code,
            "language_name": "English",
            "depth": depth
        }]
        
        raw_response = self._execute_and_log(endpoint, payload)
        items = self.client._extract_items(raw_response)
        return [SerpItem(**item) for item in items]

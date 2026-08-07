import base64
import requests
from typing import List, Dict, Any, Optional
from .schemas import (
    KeywordIdeaItem, 
    CompetitorDomainItem, 
    DomainIntersectionItem, 
    RelevantPageItem, 
    KeywordDifficultyItem, 
    SerpItem
)

class DataForSEOClient:
    BASE_URL = "https://api.dataforseo.com/v3"

    def __init__(self, login: str, password: str):
        """Initializes the client and generates the Base64 auth header automatically, exactly like Postman does."""
        self.login = login
        self.password = password
        
        credentials = f"{login}:{password}".encode('utf-8')
        base64_auth = base64.b64encode(credentials).decode('utf-8')
        
        self.headers = {
            'Authorization': f'Basic {base64_auth}',
            'Content-Type': 'application/json'
        }

    def _post(self, endpoint: str, payload: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Wrapper for POST requests"""
        response = requests.post(
            f"{self.BASE_URL}{endpoint}",
            headers=self.headers,
            json=payload
        )
        response.raise_for_status()
        return response.json()

    def _extract_items(self, response_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Helper to safely extract the 'items' array from the nested DataForSEO response."""
        try:
            return response_data['tasks'][0]['result'][0]['items']
        except (KeyError, IndexError, TypeError):
            return []

    def get_keyword_ideas(self, keywords: List[str], location_code: int = 2826, language_name: str = "English", limit: int = 100) -> List[KeywordIdeaItem]:
        payload = [{
            "keywords": keywords,
            "location_code": location_code,
            "language_name": language_name,
            "limit": limit
        }]
        data = self._post("/dataforseo_labs/google/keyword_ideas/live", payload)
        items = self._extract_items(data)
        return [KeywordIdeaItem(**item) for item in items]
        
    def get_competitor_domains(self, target_domain: str, location_code: int = 2826, language_name: str = "English", limit: int = 10) -> List[CompetitorDomainItem]:
        payload = [{
            "target": target_domain,
            "location_code": location_code,
            "language_name": language_name,
            "limit": limit
        }]
        data = self._post("/dataforseo_labs/google/competitors_domain/live", payload)
        items = self._extract_items(data)
        return [CompetitorDomainItem(**item) for item in items]

    def get_domain_intersection(self, target1: str, target2: str, location_code: int = 2826, language_name: str = "English", limit: int = 50) -> List[DomainIntersectionItem]:
        payload = [{
            "target1": target1,
            "target2": target2,
            "location_code": location_code,
            "language_name": language_name,
            "intersections": False,
            "limit": limit
        }]
        data = self._post("/dataforseo_labs/google/domain_intersection/live", payload)
        items = self._extract_items(data)
        return [DomainIntersectionItem(**item) for item in items]
        
    def get_relevant_pages(self, target_domain: str, location_code: int = 2826, language_name: str = "English", limit: int = 10) -> List[Dict[str, Any]]:
        payload = [{
            "target": target_domain,
            "location_code": location_code,
            "language_name": language_name,
            "limit": limit
        }]
        data = self._post("/dataforseo_labs/google/relevant_pages/live", payload)
        items = self._extract_items(data)
        # Relevant pages is nested weirdly so we just return the raw dicts for now
        results = []
        for item in items:
            etv = item.get('metrics', {}).get('organic', {}).get('etv', 0)
            results.append({
                "page_address": item.get('page_address'),
                "etv": etv
            })
        return results

    def get_bulk_keyword_difficulty(self, keywords: List[str], location_code: int = 2826, language_name: str = "English") -> List[KeywordDifficultyItem]:
        payload = [{
            "keywords": keywords,
            "location_code": location_code,
            "language_name": language_name
        }]
        data = self._post("/dataforseo_labs/google/bulk_keyword_difficulty/live", payload)
        items = self._extract_items(data)
        return [KeywordDifficultyItem(**item) for item in items]

    def get_advanced_serp(self, keyword: str, location_code: int = 2826, language_name: str = "English", depth: int = 10) -> List[SerpItem]:
        payload = [{
            "keyword": keyword,
            "location_code": location_code,
            "language_name": language_name,
            "depth": depth
        }]
        data = self._post("/serp/google/organic/live/advanced", payload)
        items = self._extract_items(data)
        return [SerpItem(**item) for item in items]

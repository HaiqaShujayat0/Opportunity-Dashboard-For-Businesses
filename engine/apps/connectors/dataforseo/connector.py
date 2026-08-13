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


class DataForSEOResponseError(RuntimeError):
    """DataForSEO accepted the HTTP request but rejected an API task."""


def validate_dataforseo_response(response: Dict[str, Any], endpoint: str) -> None:
    """Reject root/task-level API failures before downstream parsing."""
    if not isinstance(response, dict):
        raise DataForSEOResponseError(
            f"DataForSEO returned a non-object response for {endpoint}."
        )
    root_code = response.get("status_code")
    if root_code is not None and root_code != 20000:
        raise DataForSEOResponseError(
            f"DataForSEO request failed for {endpoint}: "
            f"{root_code} {response.get('status_message', 'Unknown error')}"
        )
    tasks = response.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise DataForSEOResponseError(
            f"DataForSEO response for {endpoint} contains no tasks."
        )
    for task in tasks:
        if not isinstance(task, dict):
            raise DataForSEOResponseError(
                f"DataForSEO response for {endpoint} contains a malformed task."
            )
        task_code = task.get("status_code")
        if task_code is not None and task_code != 20000:
            raise DataForSEOResponseError(
                f"DataForSEO task failed for {endpoint}: "
                f"{task_code} {task.get('status_message', 'Unknown error')}"
            )
        if task.get("result") is None:
            raise DataForSEOResponseError(
                f"DataForSEO task returned no result for {endpoint}."
            )


def dataforseo_response_is_successful(response: Dict[str, Any], endpoint: str) -> bool:
    try:
        validate_dataforseo_response(response, endpoint)
    except DataForSEOResponseError:
        return False
    return True


def dataforseo_response_is_declared_failure(response: Dict[str, Any]) -> bool:
    """Return True only for an explicit provider root/task error code."""
    if not isinstance(response, dict):
        return False
    root_code = response.get("status_code")
    if root_code is not None and root_code != 20000:
        return True
    tasks = response.get("tasks")
    if not isinstance(tasks, list):
        return False
    return any(
        isinstance(task, dict)
        and task.get("status_code") is not None
        and task.get("status_code") != 20000
        for task in tasks
    )


def _keyword_item(item: Dict[str, Any]) -> KeywordIdeaItem:
    """Flatten suggestion and related-keyword response variants."""
    data = item.get("keyword_data") or item
    keyword_info = data.get("keyword_info") or {}
    keyword_properties = data.get("keyword_properties") or {}
    return KeywordIdeaItem(
        keyword=data.get("keyword") or "",
        search_volume=keyword_info.get("search_volume"),
        cpc=keyword_info.get("cpc"),
        competition=keyword_info.get("competition"),
        competition_level=keyword_info.get("competition_level"),
        keyword_difficulty=keyword_properties.get("keyword_difficulty"),
        keyword_properties=keyword_properties or None,
        monthly_searches=keyword_info.get("monthly_searches"),
    )

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
        """Core execution loop: Check cache → Call API → Save to DB → Return JSON."""
        params = payload_list[0]

        # 1. Check cache first — if we paid for this already, don't pay again
        cached = self._check_cache(endpoint, params, ttl_hours=24)
        if cached and not dataforseo_response_is_successful(
            cached.payload, endpoint
        ):
            logger.warning(
                "Ignoring failed DataForSEO cache receipt %s for %s",
                cached.pk,
                endpoint,
            )
            cached = None
        if cached:
            # Bugfix: If the cache hit is from an older Run, we MUST duplicate
            # the RawFetch row for the *current* Run. Otherwise, downstream
            # stages (which filter by run_id) will find no data and crash!
            if cached.run_id != self.run.id:
                self._log_fetch(
                    endpoint=endpoint,
                    params=params,
                    payload=cached.payload,
                    cost_usd=0.0  # Cache hit is free!
                )
            return cached.payload

        try:
            # 2. Cache miss — make the live API call
            raw_response = self.client._post(endpoint, payload_list)

            # 3. Extract cost (DataForSEO returns this in the root JSON)
            cost = raw_response.get("cost", 0.0)

            # 4. Permanently save to database
            self._log_fetch(endpoint, params, raw_response, cost_usd=cost)

            validate_dataforseo_response(raw_response, endpoint)
            return raw_response

        except Exception as e:
            # Save the error too — we need an audit trail of failures
            if "raw_response" not in locals():
                logger.exception(
                    f"DataForSEO transport failed for endpoint: {endpoint}"
                )
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

    def get_keyword_suggestions(
        self, keyword: str, limit: int = 100
    ) -> List[KeywordIdeaItem]:
        """Discover close expansions for one seed (live API requirement)."""
        endpoint = "/dataforseo_labs/google/keyword_suggestions/live"
        payload = [{
            "keyword": keyword,
            "location_code": self.market.dataforseo_location_code,
            "language_name": "English",
            "limit": limit,
        }]

        raw_response = self._execute_and_log(endpoint, payload)
        items = self.client._extract_items(raw_response)
        return [_keyword_item(item) for item in items]

    def get_related_keywords(
        self, keyword: str, limit: int = 100
    ) -> List[KeywordIdeaItem]:
        """Discover related searches for one seed (live API requirement)."""
        endpoint = "/dataforseo_labs/google/related_keywords/live"
        payload = [{
            "keyword": keyword,
            "location_code": self.market.dataforseo_location_code,
            "language_name": "English",
            "limit": limit,
        }]

        raw_response = self._execute_and_log(endpoint, payload)
        items = self.client._extract_items(raw_response)
        return [_keyword_item(item) for item in items]
        
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

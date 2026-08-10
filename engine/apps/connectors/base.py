"""
BaseConnector — the abstract blueprint every data source connector must follow.

Two core responsibilities:
1. _check_cache()  — before calling ANY paid API, check if we already have
                     an identical response stored in RawFetch within the TTL.
                     If yes: return it for free. If no: proceed to live call.

2. _log_fetch()    — after every API call (success OR failure), permanently
                     save the response to RawFetch. This is our audit log
                     and the mechanism that lets us restart pipelines mid-run
                     without re-paying for already-fetched data.
"""
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any, Dict, Optional

from django.utils import timezone

from apps.ingestion.models import RawFetch

logger = logging.getLogger(__name__)


class BaseConnector(ABC):
    """
    The blueprint for all external API connectors.
    Enforces that every API call is permanently logged in the RawFetch table.
    """
    source_name = "unknown"

    def __init__(self, run, market):
        self.run = run
        self.market = market

    # ------------------------------------------------------------------
    # Hash helper
    # ------------------------------------------------------------------
    def _generate_hash(self, endpoint: str, params: Dict[str, Any]) -> str:
        """
        Generates a SHA-256 hash that uniquely fingerprints an API call.
        Identical params → identical hash → cache hit.
        Keys are sorted so dict ordering never affects the hash.
        """
        params_str = json.dumps(params, sort_keys=True)
        raw_string = f"{self.source_name}|{endpoint}|{params_str}"
        return hashlib.sha256(raw_string.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Cache check  ← NEW
    # ------------------------------------------------------------------
    def _check_cache(
        self,
        endpoint: str,
        params: Dict[str, Any],
        ttl_hours: int = 24,
    ) -> Optional[RawFetch]:
        """
        Before making a live API call, look for a recent identical call in RawFetch.

        If an identical request was made within `ttl_hours`, return the cached
        RawFetch row so the caller can reuse its payload — zero API cost.

        Returns None if no cache hit, meaning the caller should proceed with a
        live API call.
        """
        request_hash = self._generate_hash(endpoint, params)
        cutoff = timezone.now() - timedelta(hours=ttl_hours)

        cached = (
            RawFetch.objects.filter(
                request_hash=request_hash,
                fetched_at__gte=cutoff,
            )
            .exclude(payload__has_key="error")   # never return a cached error
            .order_by("-fetched_at")
            .first()
        )

        if cached:
            logger.info(
                "Cache HIT — skipping live API call",
                extra={
                    "source": self.source_name,
                    "endpoint": endpoint,
                    "raw_fetch_id": cached.pk,
                    "age_hours": round((timezone.now() - cached.fetched_at).total_seconds() / 3600, 1),
                },
            )
            return cached

        return None

    # ------------------------------------------------------------------
    # Fetch logger
    # ------------------------------------------------------------------
    def _log_fetch(
        self,
        endpoint: str,
        params: Dict[str, Any],
        payload: Any,
        cost_usd: float = 0,
    ) -> RawFetch:
        """
        Saves the exact JSON response to the database.
        Called after EVERY live API call — success and failure alike.
        This provides an indestructible audit log.
        """
        request_hash = self._generate_hash(endpoint, params)

        return RawFetch.objects.create(
            run=self.run,
            market=self.market,
            source=self.source_name,
            endpoint=endpoint,
            request_params=params,
            request_hash=request_hash,
            payload=payload,
            cost_usd=cost_usd,
        )

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Any, Dict

from apps.ingestion.models import RawFetch

class BaseConnector(ABC):
    """
    The blueprint for all external API connectors.
    Enforces that every API call is permanently logged in the RawFetch table.
    """
    source_name = "unknown"

    def __init__(self, run, market):
        self.run = run
        self.market = market

    def _generate_hash(self, endpoint: str, params: Dict[str, Any]) -> str:
        """Generates a SHA-256 hash to uniquely identify an API call and its parameters."""
        # Sort keys to ensure consistent hashing even if dictionary order changes
        params_str = json.dumps(params, sort_keys=True)
        raw_string = f"{self.source_name}|{endpoint}|{params_str}"
        return hashlib.sha256(raw_string.encode('utf-8')).hexdigest()

    def _log_fetch(self, endpoint: str, params: Dict[str, Any], payload: Any, cost_usd: float = 0) -> RawFetch:
        """
        Saves the exact JSON response to the SQLite database.
        This provides an indestructible audit log and prevents paying for duplicate calls.
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
            cost_usd=cost_usd
        )

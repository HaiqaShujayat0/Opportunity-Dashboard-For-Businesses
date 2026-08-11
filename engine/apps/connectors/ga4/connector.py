"""Fixture-first GA4 Data API connector with strict response validation."""

import json
from pathlib import Path
from typing import Callable

from django.conf import settings
from pydantic import ValidationError

from apps.connectors.base import BaseConnector
from apps.connectors.ga4.schemas import GA4RunReportResponse


class GA4ConnectorError(RuntimeError):
    pass


class GA4Connector(BaseConnector):
    source_name = "ga4"
    endpoint = "runReport"

    def __init__(self, run, market, executor: Callable[[dict], dict] | None = None, use_dummy=None):
        super().__init__(run, market)
        self.executor = executor
        self.use_dummy = (
            getattr(settings, "GOOGLE_USE_DUMMY_DATA", True)
            if use_dummy is None
            else use_dummy
        )
        self.cache_namespace = "dummy" if self.use_dummy else "live"

    def _fixture_response(self, params: dict) -> dict:
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ga4_dummy.json"
        return json.loads(fixture.read_text(encoding="utf-8"))

    def _execute(self, params: dict) -> dict:
        if self.executor:
            return self.executor(params)
        if self.use_dummy:
            return self._fixture_response(params)
        raise GA4ConnectorError(
            "Live GA4 transport is not configured. Enable GOOGLE_USE_DUMMY_DATA "
            "or provide an authenticated executor."
        )

    def fetch(self, start_date="28daysAgo", end_date="today") -> list[dict]:
        params = {
            "property": f"properties/{self.market.ga4_property_id}",
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [
                {"name": "landingPagePlusQueryString"},
                {"name": "sessionDefaultChannelGroup"},
            ],
            "metrics": [
                {"name": "sessions"},
                {"name": "keyEvents"},
                {"name": "purchaseRevenue"},
            ],
            "dimensionFilter": {
                "filter": {
                    "fieldName": "sessionDefaultChannelGroup",
                    "stringFilter": {"matchType": "EXACT", "value": "Organic Search"},
                }
            },
        }
        cached = self._check_cache(self.endpoint, params, ttl_hours=24)
        if cached:
            raw = cached.payload
            if cached.run_id != self.run.id:
                self._log_fetch(self.endpoint, params, raw, cost_usd=0)
        else:
            try:
                raw = self._execute(params)
                GA4RunReportResponse.model_validate(raw)
            except Exception as exc:
                self._log_fetch(self.endpoint, params, {"error": str(exc)}, cost_usd=0)
                raise GA4ConnectorError(f"GA4 fetch failed: {exc}") from exc
            self._log_fetch(self.endpoint, params, raw, cost_usd=0)

        try:
            return GA4RunReportResponse.model_validate(raw).records()
        except ValidationError as exc:
            raise GA4ConnectorError(f"Cached GA4 response is invalid: {exc}") from exc

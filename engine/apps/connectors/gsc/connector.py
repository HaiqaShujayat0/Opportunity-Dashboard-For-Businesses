"""Fixture-first Google Search Console connector with pagination and audit storage."""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from django.conf import settings
from django.utils import timezone
from pydantic import ValidationError

from apps.connectors.base import BaseConnector
from apps.connectors.gsc.schemas import GSCSearchAnalyticsResponse, GSCSearchAnalyticsRow


class GSCConnectorError(RuntimeError):
    pass


class GSCConnector(BaseConnector):
    source_name = "gsc"
    endpoint = "searchAnalytics/query"
    dimensions = ["query", "page", "country", "date"]
    iso3_countries = {"GB": "gbr", "DE": "deu", "FR": "fra", "NL": "nld"}

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
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "gsc_dummy.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        domain = self.market.client.primary_domain.strip().rstrip("/")
        if not domain.startswith(("http://", "https://")):
            domain = f"https://{domain}"
        for row in payload.get("rows", []):
            if len(row.get("keys", [])) >= 2:
                row["keys"][1] = row["keys"][1].replace("https://example.com", domain, 1)
        return payload

    def _execute(self, params: dict) -> dict:
        if self.executor:
            return self.executor(params)
        if self.use_dummy:
            return self._fixture_response(params)
        raise GSCConnectorError(
            "Live GSC transport is not configured. Enable GOOGLE_USE_DUMMY_DATA "
            "or provide an authenticated executor."
        )

    def fetch(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        row_limit: int = 25_000,
    ) -> list[GSCSearchAnalyticsRow]:
        if not 1 <= row_limit <= 25_000:
            raise ValueError("GSC row_limit must be between 1 and 25,000")

        lag_days = getattr(settings, "GSC_REPORTING_LAG_DAYS", 3)
        end_date = end_date or (timezone.localdate() - timedelta(days=lag_days))
        start_date = start_date or (end_date - timedelta(days=27))
        if start_date > end_date:
            raise ValueError("GSC start_date must not be after end_date")

        all_rows = []
        start_row = 0
        while True:
            params = {
                "siteUrl": self.market.gsc_property,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "dimensions": self.dimensions,
                "rowLimit": row_limit,
                "startRow": start_row,
                "dataState": "final",
            }
            country = self.iso3_countries.get(self.market.country_iso.upper())
            if country:
                params["dimensionFilterGroups"] = [{
                    "filters": [{
                        "dimension": "country",
                        "operator": "equals",
                        "expression": country,
                    }]
                }]
            cached = self._check_cache(self.endpoint, params, ttl_hours=24)
            if cached:
                raw = cached.payload
                if cached.run_id != self.run.id:
                    self._log_fetch(self.endpoint, params, raw, cost_usd=0)
            else:
                try:
                    raw = self._execute(params)
                    parsed = GSCSearchAnalyticsResponse.model_validate(raw)
                except Exception as exc:
                    self._log_fetch(self.endpoint, params, {"error": str(exc)}, cost_usd=0)
                    raise GSCConnectorError(f"GSC fetch failed: {exc}") from exc
                self._log_fetch(self.endpoint, params, raw, cost_usd=0)
                all_rows.extend(parsed.rows)
                if len(parsed.rows) < row_limit:
                    break
                start_row += row_limit
                continue

            try:
                parsed = GSCSearchAnalyticsResponse.model_validate(raw)
            except ValidationError as exc:
                raise GSCConnectorError(f"Cached GSC response is invalid: {exc}") from exc
            all_rows.extend(parsed.rows)
            if len(parsed.rows) < row_limit:
                break
            start_row += row_limit

        return all_rows

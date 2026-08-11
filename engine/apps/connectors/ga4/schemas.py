"""Strict schemas for Google Analytics Data API runReport responses."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Header(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class GA4ReportRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimensionValues: list[Value]
    metricValues: list[Value]


class GA4RunReportResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    dimensionHeaders: list[Header]
    metricHeaders: list[Header]
    rows: list[GA4ReportRow] = Field(default_factory=list)
    rowCount: int | None = None

    @model_validator(mode="after")
    def validate_row_widths(self):
        for row in self.rows:
            if len(row.dimensionValues) != len(self.dimensionHeaders):
                raise ValueError("GA4 row dimension count does not match its headers")
            if len(row.metricValues) != len(self.metricHeaders):
                raise ValueError("GA4 row metric count does not match its headers")
        return self

    def records(self) -> list[dict]:
        dimension_names = [header.name for header in self.dimensionHeaders]
        metric_names = [header.name for header in self.metricHeaders]
        records = []
        for row in self.rows:
            dimensions = dict(zip(dimension_names, (item.value for item in row.dimensionValues)))
            metrics = dict(zip(metric_names, (Decimal(item.value) for item in row.metricValues)))
            records.append({**dimensions, **metrics})
        return records

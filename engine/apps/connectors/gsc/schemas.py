"""Strict schemas for Search Console Search Analytics responses."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GSCSearchAnalyticsRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: tuple[str, str, str, date]
    clicks: float = Field(ge=0)
    impressions: float = Field(ge=0)
    ctr: float = Field(ge=0, le=1)
    position: float = Field(ge=0)

    @field_validator("keys", mode="before")
    @classmethod
    def require_expected_dimensions(cls, value):
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("GSC row keys must contain query, page, country, and date")
        return value

    @property
    def query(self) -> str:
        return self.keys[0]

    @property
    def page(self) -> str:
        return self.keys[1]

    @property
    def country(self) -> str:
        return self.keys[2]

    @property
    def observed_on(self) -> date:
        return self.keys[3]


class GSCSearchAnalyticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[GSCSearchAnalyticsRow] = Field(default_factory=list)
    responseAggregationType: str | None = None
    metadata: dict | None = None

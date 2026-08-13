"""Compatibility helpers for frozen EngineSettings snapshots."""


def engine_settings_for_market(run_or_snapshot, market_code):
    """Return one market's settings from new or legacy snapshot shapes.

    New snapshots store ``engine_settings`` by market code. Runs created before
    per-market overrides used a flat dictionary; retaining that fallback keeps
    historical runs reproducible and individually rerunnable.
    """
    snapshot = (
        run_or_snapshot.settings_snapshot
        if hasattr(run_or_snapshot, "settings_snapshot")
        else run_or_snapshot
    ) or {}
    configured = snapshot.get("engine_settings") or {}
    if not isinstance(configured, dict):
        return {}

    market_values = configured.get(market_code)
    if isinstance(market_values, dict):
        return market_values

    # A dictionary whose values are dictionaries is the new market-keyed
    # shape. A dictionary of scalar setting values is the legacy flat shape.
    if configured and all(isinstance(value, dict) for value in configured.values()):
        return {}
    return configured

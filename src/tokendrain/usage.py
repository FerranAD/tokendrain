"""Normalize Codex limits by their supplied durations, never by primary/secondary names."""

from datetime import UTC, datetime
from math import isfinite
from typing import Any

from tokendrain.domain import UsageWindow


def normalize_rate_limits(payload: dict[str, Any]) -> list[UsageWindow]:
    result: list[UsageWindow] = []
    limits = payload.get("rateLimitsByLimitId")
    if not isinstance(limits, dict) or not limits:
        value = payload.get("rateLimits", payload)
        limits = {value.get("limitId") or "codex": value} if isinstance(value, dict) else {}
    for key, limit in limits.items():
        if not isinstance(limit, dict):
            continue
        for slot in ("primary", "secondary"):
            window = limit.get(slot)
            if not isinstance(window, dict) or window.get("usedPercent") is None:
                continue
            try:
                percent = float(window["usedPercent"])
                duration = window.get("windowDurationMins")
                if duration is not None:
                    duration = int(duration)
                if not isfinite(percent) or percent < 0 or (duration is not None and duration <= 0):
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            reset = window.get("resetsAt")
            try:
                if isinstance(reset, (int, float)):
                    reset_at = datetime.fromtimestamp(reset, UTC)
                elif isinstance(reset, str):
                    reset_at = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                    if reset_at.tzinfo is None:
                        reset_at = reset_at.replace(tzinfo=UTC)
                else:
                    reset_at = None
            except (ValueError, OverflowError, OSError):
                reset_at = None
            result.append(
                UsageWindow(
                    limit_id=str(limit.get("limitId") or key),
                    name=limit.get("limitName"),
                    used_percent=percent,
                    window_minutes=duration,
                    resets_at=reset_at,
                    metadata={
                        **{
                            name: value
                            for name, value in limit.items()
                            if name not in {"primary", "secondary"}
                        },
                        "slot": slot,
                        "plan_type": limit.get("planType"),
                        "credits": limit.get("credits"),
                    },
                )
            )
    return result

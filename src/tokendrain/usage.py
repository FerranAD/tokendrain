"""Normalize Codex limits by their supplied durations, never by primary/secondary names."""

from datetime import UTC, datetime
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
            reset = window.get("resetsAt")
            if isinstance(reset, (int, float)):
                reset_at = datetime.fromtimestamp(reset, UTC)
            elif isinstance(reset, str):
                reset_at = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            else:
                reset_at = None
            result.append(
                UsageWindow(
                    limit_id=str(limit.get("limitId") or key),
                    name=limit.get("limitName"),
                    used_percent=float(window["usedPercent"]),
                    window_minutes=window.get("windowDurationMins"),
                    resets_at=reset_at,
                    metadata={
                        "slot": slot,
                        "plan_type": limit.get("planType"),
                        "credits": limit.get("credits"),
                    },
                )
            )
    return result

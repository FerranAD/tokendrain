from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tokendrain.credentials import SecretRedactor
from tokendrain.domain import (
    ElapsedStop,
    ExecutionState,
    ProjectConfig,
    RunTemplate,
    UsageStop,
    UsageWindow,
    stop_reason,
    validate_transition,
)
from tokendrain.orchestration.driver import parse_report, redact_value, report_output_schema
from tokendrain.scheduler.service import next_occurrence
from tokendrain.usage import normalize_rate_limits


def test_any_stop_rule_matches_duration_not_provider_slot() -> None:
    rules = [
        UsageStop(window_minutes=300, used_percent=95),
        UsageStop(window_minutes=10080, used_percent=70),
        ElapsedStop(seconds=100),
    ]
    windows = [UsageWindow(limit_id="meter", used_percent=69.9, window_minutes=10080)]
    assert stop_reason(rules, windows, 99) is None
    windows[0].used_percent = 70
    assert "70%" in str(stop_reason(rules, windows, 99))
    assert "100 seconds" in str(stop_reason(rules, [], 100))
    assert stop_reason([], [], 0, provider_limited=True)
    assert stop_reason([], [], 0, project_completed=True)
    assert stop_reason([UsageStop(limit_id="other", used_percent=50)], windows, 0) is None


def test_rate_limit_normalization_metadata_and_no_primary_assumption() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "custom": {
                "limitId": "custom",
                "limitName": "Quota",
                "rateLimitReachedType": "weekly",
                "primary": {"usedPercent": 71, "windowDurationMins": 10080, "resetsAt": 1800000000},
                "secondary": {
                    "usedPercent": 2,
                    "windowDurationMins": 15,
                    "resetsAt": "2026-10-03T00:00:00Z",
                },
            }
        }
    }
    windows = normalize_rate_limits(payload)
    assert [(window.window_minutes, window.used_percent) for window in windows] == [
        (10080, 71),
        (15, 2),
    ]
    assert windows[0].metadata["rateLimitReachedType"] == "weekly"
    assert windows[1].resets_at == datetime(2026, 10, 3, tzinfo=UTC)
    assert normalize_rate_limits({"rateLimits": {"primary": {"usedPercent": float("nan")}}}) == []
    assert normalize_rate_limits({"rateLimits": {"primary": {"usedPercent": "invalid"}}}) == []
    assert (
        normalize_rate_limits(
            {"rateLimits": {"primary": {"usedPercent": 1, "resetsAt": "broken"}}}
        )[0].resets_at
        is None
    )


def test_state_machine_and_duplicate_project_validation() -> None:
    validate_transition(ExecutionState.QUEUED, ExecutionState.PREPARING)
    validate_transition(ExecutionState.STOPPING, ExecutionState.FAILED)
    with pytest.raises(ValueError):
        validate_transition(ExecutionState.RUNNING, ExecutionState.COMPLETED)
    with pytest.raises(ValueError):
        validate_transition(ExecutionState.COMPLETED, ExecutionState.RUNNING)
    with pytest.raises(ValidationError):
        RunTemplate(projects=[ProjectConfig(project_id="one"), ProjectConfig(project_id="one")])
    with pytest.raises(ValidationError):
        UsageStop(used_percent=70)


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("2026-03-28T02:00:00+00:00", "2026-03-30T00:30:00+00:00"),
        ("2026-10-25T00:00:00+00:00", "2026-10-25T00:30:00+00:00"),
        ("2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+00:00"),
        ("2026-10-25T01:30:00+00:00", "2026-10-26T01:30:00+00:00"),
    ],
)
def test_cron_skips_nonexistent_and_runs_both_repeated_instants(after: str, expected: str) -> None:
    assert next_occurrence(
        "30 2 * * *", "Europe/Madrid", datetime.fromisoformat(after)
    ) == datetime.fromisoformat(expected)


def test_cron_every_minute_during_fallback_keeps_utc_order() -> None:
    assert next_occurrence(
        "* * * * *", "Europe/Madrid", datetime(2026, 10, 25, 0, 45, tzinfo=UTC)
    ) == datetime(2026, 10, 25, 0, 46, tzinfo=UTC)
    assert next_occurrence(
        "* * * * *", "Europe/Madrid", datetime(2026, 10, 25, 0, 59, tzinfo=UTC)
    ) == datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


def test_report_schema_strict_and_malformed_output_never_completes() -> None:
    schema = report_output_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert not schema["additionalProperties"]
    assert "usage" not in schema["properties"]
    with pytest.raises(ValueError, match="checkpoint"):
        parse_report("I might be done")
    assert parse_report('{"status":"completed","summary":"finished"}').status == "completed"
    safe = redact_value(
        {"summary": 'secret"quoted', "nested": ["token"]},
        SecretRedactor(['secret"quoted', "token"]),
    )
    assert safe == {"summary": "[REDACTED]", "nested": ["[REDACTED]"]}


def test_schedule_boundary_rejects_unknown_zone_and_invalid_cron() -> None:
    from tokendrain.domain import ScheduleInput

    template = RunTemplate(projects=[ProjectConfig(project_id="project")])
    with pytest.raises(ValidationError, match="timezone"):
        ScheduleInput(
            name="Invalid", cron="0 3 * * *", timezone="No/Such_Zone", run_template=template
        )
    with pytest.raises(ValidationError, match="cron"):
        ScheduleInput(name="Invalid", cron="", run_template=template)

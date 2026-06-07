import asyncio
import json
import logging
from typing import Any

from app.config import Settings
from app.github_events import parse_github_event
from app.hermes_dispatch import InMemoryHermesDispatchRegistry, dispatch_hermes_runtime_validation


class FakeHermesClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.jobs: list[tuple[str, str, dict[str, Any]]] = []

    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.jobs.append((base_url, token, payload))
        if self.fail:
            raise RuntimeError(
                f"Hermes POST rejected with X-Hermes-Token: {token} and Authorization: Bearer {token}"
            )
        return {"status": "PASSED", "jobId": "job-logged"}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    base = {
        "enable_github_writeback": False,
        "hermes_m2_base_url": "http://100.70.83.13:8787",
        "hermes_m2_token": "secret-token",
        "hermes_m2_enable_dispatch": True,
        "hermes_default_target": "https://preview.vercel.app",
    }
    base.update(overrides)
    return Settings(**base)


def pr_payload(*, action: str, labels: list[str] | None = None) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "sender": {"login": "marcus"},
        "label": {"name": "playwright"},
        "pull_request": {
            "number": 59,
            "head": {"ref": "agent-integration", "sha": "abcdef1234567890"},
            "base": {"ref": "main"},
            "labels": [{"name": item} for item in (labels or ["runtime-agent", "playwright", "bb-review-needed"])],
        },
    }


def log_events(records: list[logging.LogRecord]) -> list[dict[str, Any]]:
    return [json.loads(record.message) for record in records]


def assert_secret_absent(value: Any) -> None:
    serialized = json.dumps(value, default=str)
    assert "secret-token" not in serialized
    assert "url-secret" not in serialized
    assert "Bearer secret-token" not in serialized
    assert "X-Hermes-Token: secret-token" not in serialized


def test_pull_request_labeled_opened_and_synchronize_attempt_hermes_with_operational_logs(caplog: Any) -> None:
    for action in ["labeled", "opened", "synchronize"]:
        caplog.clear()
        parsed = parse_github_event("pull_request", pr_payload(action=action))
        hermes = FakeHermesClient()

        with caplog.at_level(logging.INFO, logger="riseos_agent_orchestrator"):
            result = run(
                dispatch_hermes_runtime_validation(
                    parsed,
                    settings(),
                    hermes_client=hermes,
                    registry=InMemoryHermesDispatchRegistry(),
                )
            )

        events = log_events(caplog.records)
        event_names = [event["event"] for event in events]

        assert result.attempted is True
        assert result.success is True
        assert len(hermes.jobs) == 1
        assert "hermes_route_evaluated" in event_names
        assert "hermes_dispatch_eligibility_evaluated" in event_names
        assert "hermes_post_attempted" in event_names
        assert "hermes_post_completed" in event_names

        route_log = next(event for event in events if event["event"] == "hermes_route_evaluated")
        assert route_log["route"] == f"pull_request_{action}"
        assert route_log["labels_request_hermes"] is True
        assert route_log["runtime_label_match"] is True
        assert route_log["lifecycle_label_match"] is True
        assert route_log["correlation_id"] == "hermes-m2-marcus937-riseos-agent-orchestrator-pr-59-abcdef1"
        assert route_log["orchestrator_correlation_id"].startswith("orch-")

        eligibility_log = next(event for event in events if event["event"] == "hermes_dispatch_eligibility_evaluated")
        assert eligibility_log["dispatch_enabled"] is True
        assert eligibility_log["dispatch_key_available"] is True
        assert eligibility_log["hermes_target"] == "https://preview.vercel.app"

        post_log = next(event for event in events if event["event"] == "hermes_post_attempted")
        assert post_log["payload_correlation_id"] == route_log["correlation_id"]
        assert post_log["hermes_base_url"] == "http://100.70.83.13:8787"
        assert hermes.jobs[0][2]["payload"]["subjectType"] == "pr"
        assert hermes.jobs[0][2]["payload"]["prNumber"] == 59
        assert "issueNumber" not in hermes.jobs[0][2]["payload"]
        assert hermes.jobs[0][2]["payload"]["screenshotName"] == "pr-59-validation.png"
        assert_secret_absent(events)
        assert_secret_absent(result.model_dump())


def test_failed_hermes_post_redacts_exception_and_target_secrets_from_operational_outputs(caplog: Any) -> None:
    parsed = parse_github_event("pull_request", pr_payload(action="labeled"))
    hermes = FakeHermesClient(fail=True)

    with caplog.at_level(logging.INFO, logger="riseos_agent_orchestrator"):
        result = run(
            dispatch_hermes_runtime_validation(
                parsed,
                settings(hermes_default_target="https://preview.vercel.app/?access_token=url-secret"),
                hermes_client=hermes,
                registry=InMemoryHermesDispatchRegistry(),
            )
        )

    events = log_events(caplog.records)
    failed_log = next(event for event in events if event["event"] == "hermes_post_failed")
    attempted_log = next(event for event in events if event["event"] == "hermes_post_attempted")

    assert result.status == "BLOCKED"
    assert result.label == "agent-blocked"
    assert "[REDACTED]" in failed_log["error"]
    assert "[REDACTED]" in attempted_log["hermes_target"]
    assert "[REDACTED]" in (result.error or "")
    assert_secret_absent(events)
    assert_secret_absent(result.model_dump())
    assert_secret_absent(result.message)


def test_non_matching_pr_labels_log_route_evaluation_without_post(caplog: Any) -> None:
    parsed = parse_github_event("pull_request", pr_payload(action="labeled", labels=["playwright"]))
    hermes = FakeHermesClient()

    with caplog.at_level(logging.INFO, logger="riseos_agent_orchestrator"):
        result = run(
            dispatch_hermes_runtime_validation(
                parsed,
                settings(),
                hermes_client=hermes,
                registry=InMemoryHermesDispatchRegistry(),
            )
        )

    events = log_events(caplog.records)
    event_names = [event["event"] for event in events]
    route_log = next(event for event in events if event["event"] == "hermes_route_evaluated")

    assert result.attempted is False
    assert result.skipped_reason == "Event does not require Hermes runtime validation."
    assert hermes.jobs == []
    assert route_log["route"] is None
    assert route_log["labels_request_hermes"] is False
    assert route_log["runtime_label_match"] is True
    assert route_log["lifecycle_label_match"] is False
    assert "hermes_post_attempted" not in event_names
    assert_secret_absent(events)
    assert_secret_absent(result.model_dump())

import asyncio
import logging
from typing import Any

from app.config import Settings
from app.event_store import event_record_from_parsed
from app.github_events import parse_github_event
from app.operational_logging import log_queue_item_created, log_slack_issue_dispatch_result, log_webhook_duplicate_suppressed
from app.review_queue import review_work_item_from_parsed
from app.slack_issue_dispatch import InMemoryDispatchedIssueRegistry, dispatch_ready_issue_to_slack


class FakeSlackClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def post_message(self, *, channel: str, text: str) -> None:
        self.messages.append((channel, text))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def issue_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "sender": {"login": "marcus"},
        "issue": {
            "number": 47,
            "title": "ORCH-003 Dispatch Idempotency and Correlation Tracking",
            "state": "open",
            "html_url": "https://github.com/marcus937/riseos-agent-orchestrator/issues/47",
            "labels": [{"name": "agent-ready"}],
        },
    }


def test_same_correlation_id_spans_webhook_slack_and_lifecycle_logs(caplog: Any) -> None:
    parsed = parse_github_event("issues", issue_payload())
    event_record = event_record_from_parsed(parsed, event_id="github-delivery:delivery-1")
    client = FakeSlackClient()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(slack_webhook_url="https://hooks.slack.test/services/test", slack_channel="#project_riseos"),
            client=client,
            registry=InMemoryDispatchedIssueRegistry(),
        )
    )

    assert event_record.correlation_id is not None
    assert result.correlation_id == event_record.correlation_id
    assert f"Correlation ID: {event_record.correlation_id}" in client.messages[0][1]

    caplog.set_level(logging.INFO, logger="riseos_agent_orchestrator")
    log_slack_issue_dispatch_result(parsed, result)
    assert f'"correlation_id": "{event_record.correlation_id}"' in caplog.text


def test_duplicate_suppression_logs_duplicate_source_and_correlation_id(caplog: Any) -> None:
    parsed = parse_github_event("issues", issue_payload())
    event_record = event_record_from_parsed(parsed, event_id="github-delivery:delivery-1")
    caplog.set_level(logging.INFO, logger="riseos_agent_orchestrator")

    log_webhook_duplicate_suppressed(parsed, event_id="github-delivery:delivery-1")

    assert f'"correlation_id": "{event_record.correlation_id}"' in caplog.text
    assert '"duplicate_source": "github_delivery_header"' in caplog.text


def test_queue_lifecycle_logs_match_issue_correlation_id(caplog: Any) -> None:
    parsed = parse_github_event("issues", issue_payload())
    event_record = event_record_from_parsed(parsed)
    item = review_work_item_from_parsed(parsed)
    caplog.set_level(logging.INFO, logger="riseos_agent_orchestrator")

    log_queue_item_created(item)

    assert f'"correlation_id": "{event_record.correlation_id}"' in caplog.text

import asyncio
import json
from typing import Any

import httpx

from app.clients.slack import SlackClient, build_requeue_task_text
from app.review_workflow import RequeueContext


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_build_requeue_task_text_includes_required_packet_fields() -> None:
    context = RequeueContext(
        repo="riseos/example",
        issue_number=7,
        pull_request_number=None,
        labels=["agent-ready", "bb-review-needed"],
        url="https://github.com/riseos/example/issues/7",
        comment_text="NEEDS_CHANGES\n@circuit-forge retry issue 7",
        matched_keyword="@circuit-forge",
        trigger="issue_comment_requeue",
    )

    text = build_requeue_task_text(context)

    assert "<!subteam^S0B8X9HTF7A>" in text
    assert "Repo: riseos/example" in text
    assert "Issue/PR: #7" in text
    assert "Labels: agent-ready, bb-review-needed" in text
    assert "URL: https://github.com/riseos/example/issues/7" in text
    assert "NEEDS_CHANGES" in text


def test_post_requeue_task_posts_to_slack_channel() -> None:
    seen: dict[str, Any] = {}
    context = RequeueContext(
        repo="riseos/example",
        issue_number=7,
        pull_request_number=None,
        labels=["agent-ready"],
        url="https://github.com/riseos/example/issues/7",
        comment_text="@circuit-forge retry issue 7",
        matched_keyword="@circuit-forge",
        trigger="issue_comment_requeue",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["json"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"ok": True, "ts": "123.456"})

    client = SlackClient(
        token="xoxb-token",
        channel="C123",
        api_base_url="https://slack.test/api",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = run(client.post_requeue_task(context))

    assert result == {"ok": True, "ts": "123.456"}
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/chat.postMessage"
    assert seen["auth"] == "Bearer xoxb-token"
    assert json.loads(seen["json"])["channel"] == "C123"

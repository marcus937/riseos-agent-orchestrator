import asyncio
from typing import Any

from app.config import Settings
from app.github_events import parse_github_event
from app.github_writeback import writeback_review_decision
from app.reviewer.decision import ReviewDecision
from app.reviewer.openai import OpenAIReviewer
from app.reviewer.openai_review import request_openai_review_decision
from app.review_queue import process_review_work_item, review_work_item_from_parsed


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeReviewer:
    model = "mock-review-model"

    def __init__(self, decision: ReviewDecision | None = None, error: Exception | None = None) -> None:
        self.decision = decision
        self.error = error
        self.prompts: list[str] = []

    def build_review_prompt(
        self,
        *,
        task_context: dict[str, object] | str,
        changed_files: list[str],
        diff: str,
        architecture_context: dict[str, object] | str | None = None,
    ) -> str:
        prompt = f"{task_context}\n{changed_files}\n{diff}\n{architecture_context}"
        self.prompts.append(prompt)
        return prompt

    async def request_review_decision(self, prompt: str) -> ReviewDecision:
        if self.error:
            raise self.error
        assert self.decision is not None
        return self.decision


class FakeOpenAIResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHTTPClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> FakeOpenAIResponse:
        self.posts.append({"url": url, **kwargs})
        return FakeOpenAIResponse(self.payload)


class FakeWritebackClient:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo_full_name, issue_number, body))
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        return {"labels": [label]}


def _item():
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "agent-integration", "sha": "abc123"}},
        },
    )
    return review_work_item_from_parsed(parsed)


def _settings(*, enabled: bool, api_key: str | None = "test-key") -> Settings:
    return Settings(
        openai_api_key=api_key,
        openai_review_model="mock-review-model",
        enable_openai_review=enabled,
        work_branch="agent-integration",
        base_branch="main",
    )


def _needs_changes_decision() -> ReviewDecision:
    return ReviewDecision.model_validate(
        {
            "decision": "NEEDS_CHANGES",
            "confidence": 0.86,
            "risk_level": "MEDIUM",
            "summary": "One test is missing.",
            "required_changes": ["Add coverage for the processor."],
            "next_task_prompt": "Add the missing processor test.",
            "human_review_required": True,
        }
    )


def test_disabled_mode_calls_no_openai() -> None:
    reviewer = FakeReviewer(decision=_needs_changes_decision())

    result = run(
        request_openai_review_decision(
            _item(),
            _settings(enabled=False),
            changed_files=["app/main.py"],
            diff_summary="commit abc123: 1 changed file(s), +1/-0.",
            github_context_available=True,
            github_context_error=None,
            reviewer=reviewer,
        )
    )

    assert result.attempted is False
    assert result.decision is None
    assert reviewer.prompts == []


def test_enabled_mode_missing_api_key_blocks_cleanly() -> None:
    result = run(
        request_openai_review_decision(
            _item(),
            _settings(enabled=True, api_key=None),
            changed_files=[],
            diff_summary=None,
            github_context_available=False,
            github_context_error="GITHUB_TOKEN missing",
        )
    )

    assert result.attempted is True
    assert result.success is False
    assert result.decision is not None
    assert result.decision.decision == "BLOCKED"
    assert "OPENAI_API_KEY" in result.error


def test_valid_mocked_openai_json_produces_review_decision() -> None:
    payload = {
        "output_text": (
            '{"decision":"APPROVED_FOR_HUMAN_REVIEW","confidence":0.92,"risk_level":"LOW",'
            '"summary":"Looks ready for Marcus.","required_changes":[],"next_task_prompt":null,'
            '"human_review_required":true}'
        )
    }
    reviewer = OpenAIReviewer(
        api_key="test-key",
        enabled=True,
        model="mock-review-model",
        http_client=FakeHTTPClient(payload),
    )

    result = run(
        request_openai_review_decision(
            _item(),
            _settings(enabled=True),
            changed_files=["app/main.py"],
            diff_summary="commit abc123: 1 changed file(s), +1/-0.",
            github_context_available=True,
            github_context_error=None,
            reviewer=reviewer,
        )
    )

    assert result.success is True
    assert result.decision is not None
    assert result.decision.decision == "APPROVED_FOR_HUMAN_REVIEW"
    assert result.reviewer_model == "mock-review-model"


def test_mocked_openai_json_missing_required_changes_becomes_blocked() -> None:
    payload = {
        "output_text": (
            '{"decision":"APPROVED_FOR_HUMAN_REVIEW","confidence":0.92,"risk_level":"LOW",'
            '"summary":"Looks ready for Marcus.","next_task_prompt":null,'
            '"human_review_required":true}'
        )
    }
    reviewer = OpenAIReviewer(
        api_key="test-key",
        enabled=True,
        model="mock-review-model",
        http_client=FakeHTTPClient(payload),
    )

    result = run(
        request_openai_review_decision(
            _item(),
            _settings(enabled=True),
            changed_files=["app/main.py"],
            diff_summary="commit abc123: 1 changed file(s), +1/-0.",
            github_context_available=True,
            github_context_error=None,
            reviewer=reviewer,
        )
    )

    assert result.attempted is True
    assert result.success is False
    assert result.decision is not None
    assert result.decision.decision == "BLOCKED"
    assert "required_changes" in result.error


def test_invalid_mocked_openai_json_becomes_blocked() -> None:
    reviewer = OpenAIReviewer(
        api_key="test-key",
        enabled=True,
        model="mock-review-model",
        http_client=FakeHTTPClient({"output_text": '{"decision":"AUTO_MERGE"}'}),
    )

    result = run(
        request_openai_review_decision(
            _item(),
            _settings(enabled=True),
            changed_files=[],
            diff_summary=None,
            github_context_available=False,
            github_context_error=None,
            reviewer=reviewer,
        )
    )

    assert result.attempted is True
    assert result.success is False
    assert result.decision is not None
    assert result.decision.decision == "BLOCKED"
    assert "failed validation" in result.error


def test_writeback_uses_validated_openai_decision() -> None:
    item = _item()
    decision = _needs_changes_decision()
    response = process_review_work_item(
        item,
        decision=decision,
        openai_review_attempted=True,
        openai_review_success=True,
        reviewer_model="mock-review-model",
    )
    client = FakeWritebackClient()

    result = run(writeback_review_decision(response, client))

    assert result.success is True
    assert client.labels == [("riseos/example", 7, "agent-needs-changes")]
    assert "NEEDS_CHANGES" in client.comments[0][2]


def test_no_forbidden_github_mutation_behavior_exists() -> None:
    forbidden = {"merge", "merge_pull_request", "delete_branch", "create_file", "update_file", "create_release"}
    assert set(dir(FakeWritebackClient)).isdisjoint(forbidden)

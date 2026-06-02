import asyncio
from typing import Any

from pydantic import ValidationError

from app.reviewer.decision import ReviewDecision, ReviewDecisionType, RiskLevel, build_review_prompt
from app.reviewer.openai import OpenAIReviewDisabledError, OpenAIReviewer, review_decision_json_schema


class FakeOpenAIResponse:
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {
            "output_text": (
                '{"decision":"APPROVED_FOR_HUMAN_REVIEW","confidence":0.91,"risk_level":"LOW",'
                '"summary":"Ready for human review.","required_changes":[],"next_task_prompt":null,'
                '"human_review_required":true}'
            )
        }


class FakeOpenAIHTTPClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> FakeOpenAIResponse:
        self.posts.append({"url": url, **kwargs})
        return FakeOpenAIResponse()


def test_valid_decision_parses() -> None:
    decision = ReviewDecision.model_validate(
        {
            "decision": "APPROVED_FOR_HUMAN_REVIEW",
            "confidence": 0.82,
            "risk_level": "LOW",
            "summary": "Ready for Marcus to review.",
            "required_changes": [],
            "next_task_prompt": None,
            "human_review_required": True,
        }
    )

    assert decision.decision == ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW
    assert decision.risk_level == RiskLevel.LOW
    assert decision.human_review_required is True


def test_required_changes_is_required() -> None:
    try:
        ReviewDecision.model_validate(
            {
                "decision": "APPROVED_FOR_HUMAN_REVIEW",
                "confidence": 0.82,
                "risk_level": "LOW",
                "summary": "Ready for Marcus to review.",
                "next_task_prompt": None,
                "human_review_required": True,
            }
        )
    except ValidationError as exc:
        assert "required_changes" in str(exc)
    else:
        raise AssertionError("ValidationError was not raised")


def test_response_format_schema_requires_every_property() -> None:
    schema = review_decision_json_schema()
    properties = schema["properties"]
    required = set(schema["required"])

    assert required == set(properties)
    assert "required_changes" in required
    assert properties["required_changes"]["type"] == "array"
    assert "next_task_prompt" in required
    assert {"type": "null"} in properties["next_task_prompt"]["anyOf"]


def test_invalid_decision_is_rejected() -> None:
    try:
        ReviewDecision.model_validate(
            {
                "decision": "AUTO_MERGE",
                "confidence": 0.95,
                "risk_level": "LOW",
                "summary": "Merge it.",
                "required_changes": [],
                "next_task_prompt": None,
                "human_review_required": True,
            }
        )
    except ValidationError as exc:
        assert "AUTO_MERGE" in str(exc)
    else:
        raise AssertionError("ValidationError was not raised")


def test_human_review_required_cannot_be_false() -> None:
    try:
        ReviewDecision.model_validate(
            {
                "decision": "NEEDS_CHANGES",
                "confidence": 0.7,
                "risk_level": "MEDIUM",
                "summary": "Needs one fix.",
                "required_changes": ["Update tests."],
                "next_task_prompt": "Fix the missing coverage.",
                "human_review_required": False,
            }
        )
    except ValidationError as exc:
        assert "human_review_required" in str(exc)
    else:
        raise AssertionError("ValidationError was not raised")


def test_prompt_includes_task_files_diff_and_guardrails() -> None:
    prompt = build_review_prompt(
        task_context={"repo": "riseos-agent-orchestrator", "task": "Implement reviewer contract"},
        changed_files=["app/reviewer/decision.py", "tests/test_review_decision.py"],
        diff="+class ReviewDecision",
        architecture_context="Human approval required before merge.",
    )

    assert "Implement reviewer contract" in prompt
    assert "app/reviewer/decision.py" in prompt
    assert "tests/test_review_decision.py" in prompt
    assert "+class ReviewDecision" in prompt
    assert "Human approval required before merge" in prompt
    assert "No auto-merge" in prompt
    assert "No production writes" in prompt
    assert "No branch changes" in prompt
    assert "APPROVED_FOR_HUMAN_REVIEW" in prompt
    assert "ESCALATE_TO_MARCUS" in prompt


def test_openai_reviewer_does_not_call_when_disabled() -> None:
    reviewer = OpenAIReviewer(api_key="test-key", enabled=False)

    try:
        asyncio.run(reviewer.request_review_decision("prompt"))
    except OpenAIReviewDisabledError as exc:
        assert "ENABLE_OPENAI_REVIEW" in str(exc)
    else:
        raise AssertionError("OpenAIReviewDisabledError was not raised")


def test_openai_reviewer_accepts_valid_mocked_json_when_enabled() -> None:
    http_client = FakeOpenAIHTTPClient()
    reviewer = OpenAIReviewer(api_key="test-key", enabled=True, model="test-model", http_client=http_client)

    decision = asyncio.run(reviewer.request_review_decision("prompt"))

    assert decision.decision == ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW
    assert http_client.posts[0]["json"]["model"] == "test-model"
    schema = http_client.posts[0]["json"]["text"]["format"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])

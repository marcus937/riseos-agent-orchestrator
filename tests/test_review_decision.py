import asyncio

from pydantic import ValidationError

from app.reviewer.decision import ReviewDecision, ReviewDecisionType, RiskLevel, build_review_prompt
from app.reviewer.openai import OpenAIReviewDisabledError, OpenAIReviewer


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


def test_openai_reviewer_placeholder_requires_future_integration_when_enabled() -> None:
    reviewer = OpenAIReviewer(api_key="test-key", enabled=True)

    try:
        asyncio.run(reviewer.request_review_decision("prompt"))
    except NotImplementedError as exc:
        assert "not implemented" in str(exc)
    else:
        raise AssertionError("NotImplementedError was not raised")

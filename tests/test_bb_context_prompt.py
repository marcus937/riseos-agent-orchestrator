import asyncio

from app.config import Settings
from app.reviewer.decision import ReviewDecision, ReviewDecisionType, RiskLevel, build_review_prompt
from app.reviewer.openai_review import request_openai_review_decision


class DummyWorkItem:
    repo_full_name = "marcus937/Project-Jarvis"

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return {"repo_full_name": self.repo_full_name, "mode": mode}


class CapturingReviewer:
    model = "test-model"

    def __init__(self) -> None:
        self.prompt: str | None = None

    def build_review_prompt(self, **kwargs: object) -> str:
        prompt = build_review_prompt(
            kwargs["task_context"],
            kwargs["changed_files"],
            kwargs["diff"],
            kwargs["architecture_context"],
            diff_patches=kwargs["diff_patches"],
        )
        self.prompt = prompt
        return prompt

    async def request_review_decision(self, prompt: str) -> ReviewDecision:
        return ReviewDecision(
            decision=ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            confidence=0.9,
            risk_level=RiskLevel.LOW,
            summary="Ready for BB review.",
            required_changes=[],
            next_task_prompt=None,
            human_review_required=True,
        )


def test_openai_prompt_includes_bb_context_when_enabled() -> None:
    reviewer = CapturingReviewer()

    result = asyncio.run(
        request_openai_review_decision(
            DummyWorkItem(),
            Settings(enable_openai_review=True, openai_api_key="test-key", enable_bb_context_pack=True),
            changed_files=["app/services_refactor/chat_service.py"],
            diff_summary="1 changed file",
            diff_patches=[{"filename": "app/main.py", "patch": "@@ -1 +1 @@\n-old\n+new"}],
            github_context_available=True,
            github_context_error=None,
            reviewer=reviewer,
        )
    )

    assert result.success is True
    assert reviewer.prompt is not None
    assert "BB architect context:" in reviewer.prompt
    assert "BB is the Project Jarvis architect and reviewer" in reviewer.prompt
    assert "Review Rubric" in reviewer.prompt
    assert "Branch Policy" in reviewer.prompt
    assert "Project Jarvis Repo Profile" in reviewer.prompt
    assert "Verify architecture alignment before code quality" in reviewer.prompt
    assert "Challenge assumptions" in reviewer.prompt
    assert "Require evidence for runtime claims" in reviewer.prompt
    assert "code inspected, code executed, and tests executed" in reviewer.prompt
    assert "VERIFIED" in reviewer.prompt
    assert "ASSUMED" in reviewer.prompt
    assert "UNVERIFIED" in reviewer.prompt
    assert "app/services_refactor/chat_service.py" in reviewer.prompt
    assert "Diff summary:\n1 changed file" in reviewer.prompt
    assert "@@ -1 +1 @@" in reviewer.prompt
    assert "Human approval is required before merge" in reviewer.prompt


def test_openai_prompt_omits_bb_context_when_disabled() -> None:
    reviewer = CapturingReviewer()

    result = asyncio.run(
        request_openai_review_decision(
            DummyWorkItem(),
            Settings(enable_openai_review=True, openai_api_key="test-key", enable_bb_context_pack=False),
            changed_files=["app/main.py"],
            diff_summary="1 changed file",
            github_context_available=True,
            github_context_error=None,
            reviewer=reviewer,
        )
    )

    assert result.success is True
    assert reviewer.prompt is not None
    assert "BB architect context:" not in reviewer.prompt
    assert "Project Jarvis Repo Profile" not in reviewer.prompt


def test_openai_review_disabled_does_not_call_reviewer() -> None:
    reviewer = CapturingReviewer()

    result = asyncio.run(
        request_openai_review_decision(
            DummyWorkItem(),
            Settings(enable_openai_review=False, openai_api_key="test-key", enable_bb_context_pack=True),
            changed_files=["app/main.py"],
            diff_summary="1 changed file",
            github_context_available=True,
            github_context_error=None,
            reviewer=reviewer,
        )
    )

    assert result.attempted is False
    assert reviewer.prompt is None

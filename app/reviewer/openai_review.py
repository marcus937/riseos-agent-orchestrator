from typing import Protocol

from pydantic import BaseModel

from app.config import Settings
from app.reviewer.decision import ReviewDecision, ReviewDecisionType, RiskLevel
from app.reviewer.openai import OpenAIReviewer
from app.review_queue import ReviewWorkItem


class ReviewDecisionRequester(Protocol):
    model: str

    def build_review_prompt(
        self,
        *,
        task_context: dict[str, object] | str,
        changed_files: list[str],
        diff: str,
        architecture_context: dict[str, object] | str | None = None,
    ) -> str:
        ...

    async def request_review_decision(self, prompt: str) -> ReviewDecision:
        ...


class OpenAIReviewResult(BaseModel):
    decision: ReviewDecision | None = None
    attempted: bool = False
    success: bool = False
    error: str | None = None
    reviewer_model: str | None = None
    prompt: str | None = None


async def request_openai_review_decision(
    item: ReviewWorkItem,
    settings: Settings,
    *,
    changed_files: list[str],
    diff_summary: str | None,
    github_context_available: bool,
    github_context_error: str | None,
    reviewer: ReviewDecisionRequester | None = None,
) -> OpenAIReviewResult:
    if not settings.enable_openai_review:
        return OpenAIReviewResult()

    if not settings.openai_api_key:
        error = "OPENAI_API_KEY is required when ENABLE_OPENAI_REVIEW=true."
        return OpenAIReviewResult(
            decision=_blocked_decision(error),
            attempted=True,
            success=False,
            error=error,
            reviewer_model=settings.openai_review_model,
        )

    owns_reviewer = reviewer is None
    reviewer = reviewer or OpenAIReviewer(
        api_key=settings.openai_api_key,
        enabled=True,
        model=settings.openai_review_model,
    )
    prompt = reviewer.build_review_prompt(
        task_context=_task_context(item, github_context_available, github_context_error),
        changed_files=changed_files,
        diff=diff_summary or "No diff summary available.",
        architecture_context=_architecture_context(settings),
    )
    try:
        decision = await reviewer.request_review_decision(prompt)
    except Exception as exc:
        error = str(exc)
        return OpenAIReviewResult(
            decision=_blocked_decision(error),
            attempted=True,
            success=False,
            error=error,
            reviewer_model=getattr(reviewer, "model", settings.openai_review_model),
            prompt=prompt,
        )
    finally:
        if owns_reviewer and hasattr(reviewer, "aclose"):
            await reviewer.aclose()

    return OpenAIReviewResult(
        decision=decision,
        attempted=True,
        success=True,
        reviewer_model=getattr(reviewer, "model", settings.openai_review_model),
        prompt=prompt,
    )


def _task_context(item: ReviewWorkItem, github_context_available: bool, github_context_error: str | None) -> dict[str, object]:
    return {
        "review_work_item": item.model_dump(mode="json"),
        "github_context_available": github_context_available,
        "github_context_error": github_context_error,
    }


def _architecture_context(settings: Settings) -> dict[str, object]:
    return {
        "work_branch": settings.work_branch,
        "base_branch": settings.base_branch,
        "branch_policy": f"Use {settings.work_branch} only for agent work. Do not change branches.",
        "no_auto_merge_policy": "No auto-merge behavior is allowed.",
        "production_write_policy": "Do not write files to repositories or mutate branches.",
        "human_approval_boundary": "Human approval is required before any merge.",
    }


def _blocked_decision(error: str) -> ReviewDecision:
    summary = f"OpenAI review could not produce a validated decision: {error}"
    return ReviewDecision(
        decision=ReviewDecisionType.BLOCKED,
        confidence=1.0,
        risk_level=RiskLevel.HIGH,
        summary=summary,
        required_changes=[summary],
        next_task_prompt="Resolve the OpenAI review error and retry processing.",
        human_review_required=True,
    )

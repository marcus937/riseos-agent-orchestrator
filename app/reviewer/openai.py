import os
from typing import Any

from app.reviewer.decision import ReviewDecision, build_review_prompt


class OpenAIReviewDisabledError(RuntimeError):
    """Raised when OpenAI review calls are not explicitly enabled."""


class OpenAIReviewer:
    """Placeholder for human-review-oriented AI decisions."""

    def __init__(self, *, api_key: str | None = None, enabled: bool | None = None) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.enabled = enabled if enabled is not None else os.getenv("ENABLE_OPENAI_REVIEW", "").lower() == "true"

    def build_review_prompt(
        self,
        *,
        task_context: dict[str, Any] | str,
        changed_files: list[str],
        diff: str,
        architecture_context: dict[str, Any] | str | None = None,
    ) -> str:
        return build_review_prompt(task_context, changed_files, diff, architecture_context)

    async def request_review_decision(self, prompt: str) -> ReviewDecision:
        if not self.enabled or not self.api_key:
            raise OpenAIReviewDisabledError(
                "OpenAI review is disabled. Set ENABLE_OPENAI_REVIEW=true and OPENAI_API_KEY to enable future calls."
            )
        raise NotImplementedError("OpenAI review decision integration is not implemented yet.")

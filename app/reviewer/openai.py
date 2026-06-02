from typing import Any


class OpenAIReviewer:
    """Placeholder for human-review-oriented AI decisions."""

    def build_review_prompt(self, *, event: dict[str, Any], context: dict[str, Any] | None = None) -> str:
        context = context or {}
        return (
            "Review this RiseOS coding-agent event and recommend comments or labels only.\n"
            "Never approve auto-merge behavior.\n"
            f"Event: {event}\n"
            f"Context: {context}"
        )

    async def request_review_decision(self, prompt: str) -> dict[str, Any]:
        raise NotImplementedError("OpenAI review decision integration is not implemented yet.")

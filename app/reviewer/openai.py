import os
import json
from typing import Any

from pydantic import ValidationError

from app.reviewer.decision import ReviewDecision, build_review_prompt


class OpenAIReviewDisabledError(RuntimeError):
    """Raised when OpenAI review calls are not explicitly enabled."""


class OpenAIReviewError(RuntimeError):
    """Raised when OpenAI review generation or validation fails."""


class OpenAIReviewer:
    """Feature-flagged OpenAI reviewer for human-review-oriented decisions."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        enabled: bool | None = None,
        model: str | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.enabled = enabled if enabled is not None else os.getenv("ENABLE_OPENAI_REVIEW", "").lower() == "true"
        self.model = model or os.getenv("OPENAI_REVIEW_MODEL", "gpt-5.5-thinking")
        self._http_client = http_client
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()

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
        if not self.enabled:
            raise OpenAIReviewDisabledError(
                "OpenAI review is disabled. Set ENABLE_OPENAI_REVIEW=true to enable review calls."
            )
        if not self.api_key:
            raise OpenAIReviewDisabledError("OPENAI_API_KEY is required when ENABLE_OPENAI_REVIEW=true.")

        response = await self._client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "input": prompt,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "review_decision",
                        "schema": review_decision_json_schema(),
                        "strict": True,
                    }
                },
            },
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise OpenAIReviewError(f"OpenAI review request failed with {response.status_code}: {response.text}")

        return self._decision_from_response(response.json())

    @property
    def _client(self) -> Any:
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(timeout=60.0)
        return self._http_client

    def _decision_from_response(self, payload: dict[str, Any]) -> ReviewDecision:
        raw_json = _extract_json_text(payload)
        if raw_json is None:
            raise OpenAIReviewError("OpenAI response did not include structured JSON content.")
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise OpenAIReviewError(f"OpenAI response was not valid JSON: {exc}") from exc
        try:
            return ReviewDecision.model_validate(data)
        except ValidationError as exc:
            raise OpenAIReviewError(f"OpenAI review decision failed validation: {exc}") from exc


def _extract_json_text(payload: dict[str, Any]) -> str | None:
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text

    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if not isinstance(content_item, dict):
                    continue
                text = content_item.get("text")
                if isinstance(text, str):
                    return text

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]

    return None


def review_decision_json_schema() -> dict[str, Any]:
    schema = ReviewDecision.model_json_schema()
    properties = schema.get("properties", {})
    schema["required"] = list(properties.keys())
    return schema

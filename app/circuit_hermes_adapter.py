from __future__ import annotations

from typing import Any

from app.config import Settings
from app.hermes_dispatch import HermesEvidenceSnapshot, HermesHTTPClient
from app.hermes_dispatch import _canonical_hermes_job_id as _legacy_canonical_hermes_job_id
from app.hermes_dispatch import _collect_hermes_evidence as _legacy_collect_hermes_evidence
from app.hermes_dispatch import _format_optional_bool as _legacy_format_optional_bool
from app.hermes_dispatch import _redact_sensitive_text as _legacy_redact_sensitive_text


class CircuitHermesClient:
    """Public adapter for Circuit runtime validation Hermes operations."""

    def __init__(self) -> None:
        self._client = HermesHTTPClient()

    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._client.post_job(base_url, token, payload)

    async def collect_evidence(
        self,
        base_url: str,
        token: str,
        job_id: str,
        settings: Settings,
    ) -> HermesEvidenceSnapshot | None:
        return await _legacy_collect_hermes_evidence(self._client, base_url, token, job_id, settings)

    async def aclose(self) -> None:
        await self._client.aclose()


def canonical_job_id(response: dict[str, Any]) -> str | None:
    return _legacy_canonical_hermes_job_id(response)


def redact_runtime_text(value: str | None, settings: Settings) -> str | None:
    return _legacy_redact_sensitive_text(value, settings)


def format_optional_bool(value: bool | None) -> str:
    return _legacy_format_optional_bool(value)

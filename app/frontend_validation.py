from __future__ import annotations

from enum import StrEnum
from typing import Any


class HermesValidationState(StrEnum):
    HERMES_VALIDATION_REQUESTED = "HERMES_VALIDATION_REQUESTED"
    HERMES_VALIDATION_RUNNING = "HERMES_VALIDATION_RUNNING"
    PLAYWRIGHT_EXECUTED = "PLAYWRIGHT_EXECUTED"
    HERMES_VALIDATION_PASSED = "HERMES_VALIDATION_PASSED"
    HERMES_VALIDATION_FAILED = "HERMES_VALIDATION_FAILED"
    HERMES_VALIDATION_BLOCKED = "HERMES_VALIDATION_BLOCKED"


SUPPORTED_VALIDATION_PROFILES = {
    "frontend_playwright",
    "jmc_frontend_preview_v1",
    "marketing_dashboard_preview_v1",
}

FRONTEND_REPOSITORY_PROFILES = {
    "marcus937/jarvis-mission-control": "jmc_frontend_preview_v1",
    "marcus937/rise-marketing-os": "marketing_dashboard_preview_v1",
    "marcus937/rise-signal": "frontend_playwright",
}

BYPASS_MARKERS = {"documentation-only", "documentation_only", "docs-only", "docs_only", "backend-only", "backend_only"}


def validation_profile_for_repository(repository: str | None) -> str | None:
    if not repository:
        return None
    return FRONTEND_REPOSITORY_PROFILES.get(repository.lower())


def is_frontend_repository(repository: str | None) -> bool:
    return validation_profile_for_repository(repository) is not None


def metadata_bypasses_runtime_validation(metadata: dict[str, Any] | None) -> bool:
    metadata = metadata or {}
    if metadata.get("documentation_only") is True or metadata.get("backend_only") is True:
        return True
    task_type = str(metadata.get("task_type") or metadata.get("work_type") or "").lower()
    return task_type in BYPASS_MARKERS


def requires_runtime_validation(repository: str | None, metadata: dict[str, Any] | None = None) -> bool:
    metadata = metadata or {}
    if metadata_bypasses_runtime_validation(metadata):
        return False
    if metadata.get("requires_runtime_validation") is True:
        return True
    if metadata.get("requires_runtime_validation") is False:
        return False
    return is_frontend_repository(repository)


def validation_profile_for_work(repository: str | None, metadata: dict[str, Any] | None = None) -> str | None:
    metadata = metadata or {}
    profile = metadata.get("validation_profile")
    if isinstance(profile, str) and profile in SUPPORTED_VALIDATION_PROFILES:
        return profile
    return validation_profile_for_repository(repository) if requires_runtime_validation(repository, metadata) else None

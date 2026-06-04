from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReviewDecisionType(StrEnum):
    APPROVED_FOR_HUMAN_REVIEW = "APPROVED_FOR_HUMAN_REVIEW"
    NEEDS_CHANGES = "NEEDS_CHANGES"
    BLOCKED = "BLOCKED"
    ESCALATE_TO_MARCUS = "ESCALATE_TO_MARCUS"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecisionType
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    summary: str
    required_changes: list[str]
    next_task_prompt: str | None
    human_review_required: bool

    @field_validator("summary")
    @classmethod
    def summary_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary is required")
        return value

    @model_validator(mode="after")
    def enforce_human_review(self) -> "ReviewDecision":
        if not self.human_review_required:
            raise ValueError("human_review_required must be true before merge")
        return self


def build_review_prompt(
    task_context: dict[str, Any] | str,
    changed_files: list[str],
    diff: str,
    architecture_context: dict[str, Any] | str | None = None,
    *,
    diff_patches: list[dict[str, Any]] | None = None,
) -> str:
    """Build the BB/Jarvis Architect review prompt for completed agent work."""

    files = "\n".join(f"- {path}" for path in changed_files) if changed_files else "- No files reported"
    patches = _format_diff_patches(diff_patches or [])
    bb_context = _extract_bb_context(architecture_context)
    architecture = _format_architecture_context(architecture_context)
    return (
        "You are BB/Jarvis Architect reviewing completed coding-agent work.\n"
        "Return one structured review decision only.\n\n"
        "Allowed decisions:\n"
        "- APPROVED_FOR_HUMAN_REVIEW\n"
        "- NEEDS_CHANGES\n"
        "- BLOCKED\n"
        "- ESCALATE_TO_MARCUS\n\n"
        "Required fields: decision, confidence, risk_level, summary, required_changes, "
        "next_task_prompt, human_review_required.\n\n"
        f"{bb_context}"
        "Safety guardrails:\n"
        "- No auto-merge.\n"
        "- No production writes.\n"
        "- No branch changes.\n"
        "- Do not approve changes that violate secrets, service ownership, branch mutation, "
        "or production-write rules.\n"
        "- If context is insufficient, request changes instead of guessing.\n\n"
        "Human approval boundary:\n"
        "- Human approval is required before merge.\n"
        "- Your decision may approve work for human review only; it must not merge or imply merge authority.\n\n"
        f"Task context:\n{task_context}\n\n"
        f"Changed files:\n{files}\n\n"
        f"Diff summary:\n{diff}\n\n"
        f"Diff patches:\n{patches}\n\n"
        f"Architecture context:\n{architecture}"
    )


def _extract_bb_context(architecture_context: dict[str, Any] | str | None) -> str:
    if not isinstance(architecture_context, dict):
        return ""
    context_pack = architecture_context.get("bb_architect_context")
    if not isinstance(context_pack, str) or not context_pack.strip():
        return ""
    return f"BB architect context:\n{context_pack}\n\n"


def _format_architecture_context(architecture_context: dict[str, Any] | str | None) -> dict[str, Any] | str:
    if not isinstance(architecture_context, dict):
        return architecture_context if architecture_context is not None else "Not provided"
    return {key: value for key, value in architecture_context.items() if key != "bb_architect_context"}


def _format_diff_patches(diff_patches: list[dict[str, Any]]) -> str:
    if not diff_patches:
        return "No patch content available; review from summary context only."

    formatted: list[str] = []
    for patch_info in diff_patches:
        filename = patch_info.get("filename") or "unknown"
        status = patch_info.get("status") or "unknown"
        additions = patch_info.get("additions", 0)
        deletions = patch_info.get("deletions", 0)
        patch = patch_info.get("patch") or ""
        formatted.append(
            f"### {filename} ({status}, +{additions}/-{deletions})\n"
            "```diff\n"
            f"{patch}\n"
            "```"
        )
    return "\n\n".join(formatted)

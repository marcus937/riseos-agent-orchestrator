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
    required_changes: list[str] = Field(default_factory=list)
    next_task_prompt: str | None = None
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
) -> str:
    """Build the BB/Jarvis Architect review prompt for completed agent work."""

    files = "\n".join(f"- {path}" for path in changed_files) if changed_files else "- No files reported"
    architecture = architecture_context if architecture_context is not None else "Not provided"
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
        "Guardrails:\n"
        "- No auto-merge.\n"
        "- No production writes.\n"
        "- No branch changes.\n"
        "- Human approval is required before merge.\n\n"
        f"Task context:\n{task_context}\n\n"
        f"Changed files:\n{files}\n\n"
        f"Diff:\n{diff}\n\n"
        f"Architecture context:\n{architecture}"
    )

from __future__ import annotations

from typing import Any

from app.reviewer.decision import ReviewDecision, ReviewDecisionType, RiskLevel
from app.runtime_validation_review_bridge import RUNTIME_REVIEW_SOURCE

REVIEWER_MODEL = "hermes-bb2-runtime-validation"


def review_decision_from_runtime_validation_context(context: dict[str, object]) -> ReviewDecision | None:
    if context.get("source") != RUNTIME_REVIEW_SOURCE:
        return None

    bb2_packet = context.get("bb2_packet")
    if not isinstance(bb2_packet, dict):
        return None

    review_status = _normalized_status(bb2_packet.get("review_status") or context.get("validation_status"))
    if not review_status:
        return None

    validation_id = _optional_string(context.get("validation_id"))
    hermes_status = _optional_string(context.get("hermes_status"))
    error = _optional_string(context.get("error")) or _optional_string(bb2_packet.get("error"))
    base_summary = _summary(review_status, validation_id=validation_id, hermes_status=hermes_status, error=error)

    if review_status in {"approved", "completed", "passed", "pass"}:
        return ReviewDecision(
            decision=ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            confidence=1.0,
            risk_level=RiskLevel.LOW,
            summary=base_summary,
            required_changes=[],
            next_task_prompt=None,
            human_review_required=True,
        )

    if review_status in {"needs_changes", "changes_requested", "needs-change", "needs change"}:
        required_changes = _string_list(bb2_packet.get("required_changes")) or [base_summary]
        return ReviewDecision(
            decision=ReviewDecisionType.NEEDS_CHANGES,
            confidence=1.0,
            risk_level=RiskLevel.MEDIUM,
            summary=base_summary,
            required_changes=required_changes,
            next_task_prompt=_optional_string(bb2_packet.get("next_task_prompt")) or "Address the Hermes/BB2 runtime validation findings and retry review.",
            human_review_required=True,
        )

    if review_status in {"blocked", "failed", "error"}:
        required_changes = _string_list(bb2_packet.get("required_changes")) or [base_summary]
        return ReviewDecision(
            decision=ReviewDecisionType.BLOCKED,
            confidence=1.0,
            risk_level=RiskLevel.HIGH,
            summary=base_summary,
            required_changes=required_changes,
            next_task_prompt=_optional_string(bb2_packet.get("next_task_prompt")) or "Resolve the Hermes/BB2 runtime validation failure and retry review.",
            human_review_required=True,
        )

    return None


def _summary(review_status: str, *, validation_id: str | None, hermes_status: str | None, error: str | None) -> str:
    details = [f"review_status={review_status}"]
    if validation_id:
        details.append(f"validation_id={validation_id}")
    if hermes_status:
        details.append(f"hermes_status={hermes_status}")
    if error:
        details.append(f"error={error}")
    return f"Hermes/BB2 runtime validation produced the review decision ({', '.join(details)})."


def _normalized_status(value: Any) -> str | None:
    if value is None:
        return None
    status = str(value).strip().lower()
    return status or None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]

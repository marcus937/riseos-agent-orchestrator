from __future__ import annotations

from enum import StrEnum

from app.reviewer.decision import ReviewDecisionType


LABEL_AGENT_READY = "agent-ready"
LABEL_AGENT_WORKING = "agent-working"
LABEL_AGENT_NEXT = "agent-next"
LABEL_RUNTIME_AGENT = "runtime-agent"
LABEL_PLAYWRIGHT = "playwright"
LABEL_AGENT_VERIFIED = "agent-verified"
LABEL_AGENT_BLOCKED = "agent-blocked"
LABEL_AGENT_REVISIONS = "agent-revisions"
LABEL_BB_REVIEW_NEEDED = "bb-review-needed"
LABEL_BB2_APPROVED = "bb2-approved"
LABEL_BB2_NEEDS_CHANGES = "bb2-needs-changes"
LABEL_BB2_BLOCKED = "bb2-blocked"
LABEL_READY_TO_MERGE = "ready-to-merge"

CIRCUIT_TRIGGER_LABELS = {LABEL_AGENT_READY, LABEL_BB2_NEEDS_CHANGES}
HERMES_TRIGGER_LABELS = {LABEL_RUNTIME_AGENT, LABEL_PLAYWRIGHT}
HERMES_SUCCESS_LABELS = {LABEL_AGENT_VERIFIED}
BLOCKING_LABELS = {LABEL_AGENT_BLOCKED, LABEL_AGENT_REVISIONS, LABEL_BB2_NEEDS_CHANGES, LABEL_BB2_BLOCKED}


class PRWorkflowState(StrEnum):
    CIRCUIT_READY = "circuit_ready"
    CIRCUIT_WORKING = "circuit_working"
    HERMES_REQUESTED = "hermes_requested"
    HERMES_VERIFIED = "hermes_verified"
    HERMES_BLOCKED = "hermes_blocked"
    HERMES_REVISIONS = "hermes_revisions"
    BB2_REVIEW_REQUESTED = "bb2_review_requested"
    BB2_NEEDS_CHANGES = "bb2_needs_changes"
    BB2_BLOCKED = "bb2_blocked"
    BB2_APPROVED = "bb2_approved"
    READY_TO_MERGE = "ready_to_merge"


def normalize_labels(labels: list[str] | set[str] | tuple[str, ...] | None) -> set[str]:
    return {label for label in labels or [] if label}


def workflow_state_from_labels(labels: list[str] | set[str] | tuple[str, ...] | None) -> PRWorkflowState | None:
    current = normalize_labels(labels)
    if LABEL_BB2_NEEDS_CHANGES in current:
        return PRWorkflowState.BB2_NEEDS_CHANGES
    if LABEL_BB2_BLOCKED in current:
        return PRWorkflowState.BB2_BLOCKED
    if LABEL_AGENT_REVISIONS in current:
        return PRWorkflowState.HERMES_REVISIONS
    if LABEL_AGENT_BLOCKED in current:
        return PRWorkflowState.HERMES_BLOCKED
    if LABEL_READY_TO_MERGE in current:
        return PRWorkflowState.READY_TO_MERGE
    if LABEL_BB2_APPROVED in current:
        return PRWorkflowState.BB2_APPROVED
    if LABEL_BB_REVIEW_NEEDED in current:
        return PRWorkflowState.BB2_REVIEW_REQUESTED
    if LABEL_AGENT_VERIFIED in current:
        return PRWorkflowState.HERMES_VERIFIED
    if current & HERMES_TRIGGER_LABELS:
        return PRWorkflowState.HERMES_REQUESTED
    if LABEL_AGENT_WORKING in current:
        return PRWorkflowState.CIRCUIT_WORKING
    if current & CIRCUIT_TRIGGER_LABELS:
        return PRWorkflowState.CIRCUIT_READY
    return None


def bb2_decision_transition_labels(
    decision: ReviewDecisionType,
    current_labels: list[str] | set[str] | tuple[str, ...] | None,
) -> list[str]:
    current = normalize_labels(current_labels)
    if decision == ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW:
        labels = [LABEL_BB2_APPROVED]
        if ready_to_merge_allowed(current | {LABEL_BB2_APPROVED}):
            labels.append(LABEL_READY_TO_MERGE)
        return _missing_labels(labels, current)
    if decision == ReviewDecisionType.NEEDS_CHANGES:
        return _missing_labels([LABEL_BB2_NEEDS_CHANGES], current)
    return _missing_labels([LABEL_BB2_BLOCKED], current)


def ready_to_merge_allowed(labels: list[str] | set[str] | tuple[str, ...] | None) -> bool:
    current = normalize_labels(labels)
    if LABEL_BB2_APPROVED not in current:
        return False
    if not current & HERMES_SUCCESS_LABELS:
        return False
    if current & BLOCKING_LABELS:
        return False
    return True


def _missing_labels(labels: list[str], current: set[str]) -> list[str]:
    return [label for label in labels if label not in current]

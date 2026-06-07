from app.pr_workflow_state import (
    PRWorkflowState,
    bb2_decision_transition_labels,
    ready_to_merge_allowed,
    workflow_state_from_labels,
)
from app.reviewer.decision import ReviewDecisionType


def test_ready_to_merge_requires_bb2_approval_and_agent_verification() -> None:
    labels = {"runtime-agent", "playwright", "agent-verified", "bb2-approved"}

    assert ready_to_merge_allowed(labels) is True
    assert workflow_state_from_labels(labels | {"ready-to-merge"}) == PRWorkflowState.READY_TO_MERGE


def test_ready_to_merge_not_added_without_runtime_verification() -> None:
    labels = {"runtime-agent", "playwright", "bb-review-needed"}

    assert ready_to_merge_allowed(labels | {"bb2-approved"}) is False
    assert bb2_decision_transition_labels(ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW, labels) == ["bb2-approved"]


def test_bb2_approval_adds_ready_to_merge_after_hermes_passes() -> None:
    labels = {"runtime-agent", "playwright", "agent-verified", "bb-review-needed"}

    assert bb2_decision_transition_labels(ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW, labels) == [
        "bb2-approved",
        "ready-to-merge",
    ]


def test_bb2_needs_changes_routes_back_to_circuit_without_removing_triggers() -> None:
    labels = {"runtime-agent", "playwright", "agent-revisions", "bb-review-needed"}

    assert workflow_state_from_labels(labels | {"bb2-needs-changes"}) == PRWorkflowState.BB2_NEEDS_CHANGES
    assert bb2_decision_transition_labels(ReviewDecisionType.NEEDS_CHANGES, labels) == [
        "bb2-needs-changes",
        "agent-next",
    ]


def test_blocker_labels_prevent_ready_to_merge() -> None:
    labels = {"runtime-agent", "playwright", "agent-verified", "bb2-approved", "agent-revisions"}

    assert ready_to_merge_allowed(labels) is False

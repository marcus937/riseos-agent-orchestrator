from app.reviewer.decision import ReviewDecisionType, RiskLevel
from app.runtime_validation_review_bridge import RUNTIME_REVIEW_SOURCE
from app.runtime_validation_review_decision import review_decision_from_runtime_validation_context


def test_runtime_validation_context_approved_becomes_review_decision() -> None:
    decision = review_decision_from_runtime_validation_context(
        {
            "source": RUNTIME_REVIEW_SOURCE,
            "validation_id": "validation-123",
            "validation_status": "completed",
            "hermes_status": "completed",
            "bb2_packet": {"review_status": "approved"},
        }
    )

    assert decision is not None
    assert decision.decision == ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW
    assert decision.risk_level == RiskLevel.LOW
    assert decision.required_changes == []
    assert "Hermes/BB2 runtime validation" in decision.summary
    assert "validation-123" in decision.summary


def test_runtime_validation_context_blocked_becomes_review_decision() -> None:
    decision = review_decision_from_runtime_validation_context(
        {
            "source": RUNTIME_REVIEW_SOURCE,
            "validation_id": "validation-456",
            "validation_status": "failed",
            "error": "runtime smoke check failed",
            "bb2_packet": {
                "review_status": "blocked",
                "required_changes": ["Fix runtime smoke check."],
                "next_task_prompt": "Repair the failing runtime check.",
            },
        }
    )

    assert decision is not None
    assert decision.decision == ReviewDecisionType.BLOCKED
    assert decision.risk_level == RiskLevel.HIGH
    assert decision.required_changes == ["Fix runtime smoke check."]
    assert decision.next_task_prompt == "Repair the failing runtime check."
    assert "runtime smoke check failed" in decision.summary


def test_non_runtime_validation_context_does_not_create_decision() -> None:
    decision = review_decision_from_runtime_validation_context(
        {
            "source": "github_issue_comment",
            "bb2_packet": {"review_status": "approved"},
        }
    )

    assert decision is None


def test_unknown_runtime_review_status_falls_back_to_openai_path() -> None:
    decision = review_decision_from_runtime_validation_context(
        {
            "source": RUNTIME_REVIEW_SOURCE,
            "bb2_packet": {"review_status": "waiting_for_operator"},
        }
    )

    assert decision is None

from pathlib import Path

from app.task_claim_registry import DUPLICATE_DECISION, CLAIMED_DECISION, TaskClaimRegistry, TaskClaimRequest, normalize_branch_rule
from app.task_claim_registry_validation import run_validation


def test_branch_rule_normalization_matches_branch_rule_variants() -> None:
    assert normalize_branch_rule("Agent-Integration ONLY") == "agent-integration"
    assert normalize_branch_rule("`agent-integration` only") == "agent-integration"
    assert normalize_branch_rule("Branch: agent-integration only") == "agent-integration"


def test_duplicate_slack_dispatch_records_already_claimed(tmp_path: Path) -> None:
    registry = TaskClaimRegistry(str(tmp_path / "claims.db"))
    request = TaskClaimRequest("marcus937/riseos-agent-orchestrator", 40, None, "circuit task", "agent-integration only", "slack")

    first = registry.claim_task(request)
    second = registry.claim_task(request)

    assert first.decision == CLAIMED_DECISION
    assert second.decision == DUPLICATE_DECISION
    assert second.duplicate_of_claim_id == first.claim["claim_id"]
    assert len(registry.list_claims()) == 1
    assert len(registry.list_duplicate_dispatches()) == 1


def test_slack_and_github_duplicate_paths_share_one_active_claim(tmp_path: Path) -> None:
    registry = TaskClaimRegistry(str(tmp_path / "claims.db"))
    slack = TaskClaimRequest("marcus937/riseos-agent-orchestrator", 40, None, "circuit task", "agent-integration only", "direct_slack")
    github = TaskClaimRequest("Marcus937/RiseOS-Agent-Orchestrator", 40, None, "circuit-task", "`agent-integration` only", "github_issue")

    first = registry.claim_task(slack)
    second = registry.claim_task(github)

    assert first.decision == CLAIMED_DECISION
    assert second.decision == DUPLICATE_DECISION
    assert sum(1 for claim in registry.list_claims() if claim["status"] == "active") == 1


def test_completed_task_requeues_only_when_explicitly_allowed(tmp_path: Path) -> None:
    registry = TaskClaimRegistry(str(tmp_path / "claims.db"))
    request = TaskClaimRequest("marcus937/riseos-agent-orchestrator", 40, None, "circuit task", "agent-integration", "github_issue")
    first = registry.claim_task(request)
    registry.complete_claim(first.claim["claim_id"])

    blocked = registry.claim_task(request)
    allowed = registry.claim_task(
        TaskClaimRequest(
            "marcus937/riseos-agent-orchestrator",
            40,
            None,
            "circuit task",
            "agent-integration",
            "comment_requeue",
            allow_completed_requeue=True,
        )
    )

    assert blocked.decision == DUPLICATE_DECISION
    assert allowed.decision == "requeued"
    assert sum(1 for claim in registry.list_claims() if claim["status"] == "active") == 1


def test_validation_writes_required_artifacts(tmp_path: Path) -> None:
    result = run_validation(tmp_path)

    assert result.passed is True
    assert result.failures == []
    for artifact_name in [
        "claim-registry.json",
        "duplicate-dispatches.json",
        "queue-state.json",
        "diagnostics.json",
        "failure-summary.md",
    ]:
        artifact_path = tmp_path / artifact_name
        assert artifact_path.exists()
        assert artifact_path.read_text(encoding="utf-8").strip()

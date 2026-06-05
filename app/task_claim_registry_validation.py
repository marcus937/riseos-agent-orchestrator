from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.task_claim_registry import CLAIMED_DECISION, DUPLICATE_DECISION, REQUEUED_DECISION, TaskClaimRegistry, TaskClaimRequest


REPO = "marcus937/riseos-agent-orchestrator"
TASK_TYPE = "circuit_task"
BRANCH_RULE = "agent-integration only"
ISSUE_NUMBER = 77


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    failures: list[str]
    artifacts: dict[str, str]
    diagnostics: dict[str, Any]


def run_validation(artifacts_dir: Path) -> ValidationResult:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="task-claim-registry-") as temp_dir:
        registry = TaskClaimRegistry(str(Path(temp_dir) / "task-claims.db"))
        scenario_results = run_scenarios(registry)
        diagnostics = build_diagnostics(registry, scenario_results)
        failures = validate_diagnostics(diagnostics)
        artifacts = write_artifacts(artifacts_dir, registry, scenario_results, diagnostics, failures)
        return ValidationResult(not failures, failures, artifacts, diagnostics)


def run_scenarios(registry: TaskClaimRegistry) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    results.append(_claim(registry, "direct_slack_mention", issue_number=ISSUE_NUMBER, branch_rule="Agent-Integration ONLY"))
    results.append(_claim(registry, "duplicate_slack_dispatch", issue_number=ISSUE_NUMBER, branch_rule="agent-integration"))
    results.append(_claim(registry, "github_issue_dispatch", issue_number=ISSUE_NUMBER, branch_rule="`agent-integration` only"))
    results.append(_claim(registry, "orchestrator_slack_notification", issue_number=ISSUE_NUMBER, branch_rule="Branch: agent-integration only"))
    results.append(_claim(registry, "comment_requeue", issue_number=ISSUE_NUMBER, branch_rule="agent-integration only"))

    first_claim_id = results[0]["claim_id"]
    registry.complete_claim(first_claim_id)
    results.append(_claim(registry, "completed_requeue_blocked", issue_number=ISSUE_NUMBER, branch_rule="agent-integration only"))
    results.append(
        _claim(
            registry,
            "completed_requeue_allowed",
            issue_number=ISSUE_NUMBER,
            branch_rule="agent-integration only",
            allow_completed_requeue=True,
        )
    )
    return results


def build_diagnostics(registry: TaskClaimRegistry, scenario_results: list[dict[str, Any]]) -> dict[str, Any]:
    claims = registry.list_claims()
    duplicates = registry.list_duplicate_dispatches()
    active_claims = [claim for claim in claims if claim["status"] == "active"]
    completed_claims = [claim for claim in claims if claim["status"] == "completed"]
    return {
        "scenario_results": scenario_results,
        "claim_count": len(claims),
        "active_claim_count": len(active_claims),
        "completed_claim_count": len(completed_claims),
        "duplicate_dispatch_count": len(duplicates),
        "already_claimed_count": sum(1 for result in scenario_results if result["decision"] == DUPLICATE_DECISION),
        "requeued_count": sum(1 for result in scenario_results if result["decision"] == REQUEUED_DECISION),
        "claimed_count": sum(1 for result in scenario_results if result["decision"] == CLAIMED_DECISION),
        "claims_by_identity": _claims_by_identity(claims),
    }


def validate_diagnostics(diagnostics: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if diagnostics["claimed_count"] != 1:
        failures.append(f"Expected one initial claim, found {diagnostics['claimed_count']}.")
    if diagnostics["already_claimed_count"] != 5:
        failures.append(f"Expected five already-claimed duplicate records, found {diagnostics['already_claimed_count']}.")
    if diagnostics["requeued_count"] != 1:
        failures.append(f"Expected one explicit completed-task requeue, found {diagnostics['requeued_count']}.")
    if diagnostics["claim_count"] != 2:
        failures.append(f"Expected two total claims after explicit requeue, found {diagnostics['claim_count']}.")
    if diagnostics["active_claim_count"] != 1:
        failures.append(f"Expected exactly one active claim, found {diagnostics['active_claim_count']}.")
    if diagnostics["duplicate_dispatch_count"] != 5:
        failures.append(f"Expected five duplicate dispatch records, found {diagnostics['duplicate_dispatch_count']}.")
    for identity, claims in diagnostics["claims_by_identity"].items():
        active = [claim for claim in claims if claim["status"] == "active"]
        if len(active) > 1:
            failures.append(f"Identity {identity} has multiple active claims: {active}")
    return failures


def write_artifacts(
    artifacts_dir: Path,
    registry: TaskClaimRegistry,
    scenario_results: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    failures: list[str],
) -> dict[str, str]:
    claims = registry.list_claims()
    duplicates = registry.list_duplicate_dispatches()
    artifacts = {
        "claim-registry.json": artifacts_dir / "claim-registry.json",
        "duplicate-dispatches.json": artifacts_dir / "duplicate-dispatches.json",
        "queue-state.json": artifacts_dir / "queue-state.json",
        "diagnostics.json": artifacts_dir / "diagnostics.json",
        "failure-summary.md": artifacts_dir / "failure-summary.md",
    }
    _write_json(artifacts["claim-registry.json"], {"claims": claims})
    _write_json(artifacts["duplicate-dispatches.json"], {"duplicates": duplicates})
    _write_json(
        artifacts["queue-state.json"],
        {
            "active_claims": [claim for claim in claims if claim["status"] == "active"],
            "completed_claims": [claim for claim in claims if claim["status"] == "completed"],
            "scenario_results": scenario_results,
        },
    )
    _write_json(artifacts["diagnostics.json"], diagnostics)
    _write_failure_summary(artifacts["failure-summary.md"], failures, diagnostics)
    return {name: str(path) for name, path in artifacts.items()}


def _claim(
    registry: TaskClaimRegistry,
    source: str,
    *,
    issue_number: int,
    branch_rule: str,
    allow_completed_requeue: bool = False,
) -> dict[str, Any]:
    result = registry.claim_task(
        TaskClaimRequest(
            repo_full_name=REPO,
            issue_number=issue_number,
            pr_number=None,
            task_type=TASK_TYPE,
            branch_rule=branch_rule,
            source=source,
            allow_completed_requeue=allow_completed_requeue,
        )
    )
    return {
        "source": source,
        "decision": result.decision,
        "claim_id": result.claim["claim_id"],
        "duplicate_of_claim_id": result.duplicate_of_claim_id,
    }


def _claims_by_identity(claims: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        identity = "|".join(
            [
                str(claim["repo_full_name"]),
                str(claim["issue_number"]),
                str(claim["pr_number"]),
                str(claim["task_type"]),
                str(claim["normalized_branch_rule"]),
            ]
        )
        grouped.setdefault(identity, []).append(claim)
    return grouped


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_failure_summary(path: Path, failures: list[str], diagnostics: dict[str, Any]) -> None:
    if failures:
        lines = ["# Task Claim Registry Validation", "", "FAILED", ""]
        lines.extend(f"- {failure}" for failure in failures)
    else:
        lines = [
            "# Task Claim Registry Validation",
            "",
            "PASSED",
            "",
            "- Duplicate Slack dispatch recorded already_claimed.",
            "- Duplicate GitHub issue dispatch recorded already_claimed.",
            "- Slack plus GitHub duplicate path recorded already_claimed.",
            "- Comment requeue duplicate recorded already_claimed.",
            "- Completed task requeue was blocked unless explicitly allowed.",
            "- Exactly one active claim remains for the task identity.",
        ]
    lines.extend(
        [
            "",
            "## Diagnostics",
            f"- claim_count: {diagnostics['claim_count']}",
            f"- active_claim_count: {diagnostics['active_claim_count']}",
            f"- duplicate_dispatch_count: {diagnostics['duplicate_dispatch_count']}",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic task claim registry validation.")
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    args = parser.parse_args()
    result = run_validation(args.artifacts_dir)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

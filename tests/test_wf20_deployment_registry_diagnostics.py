from __future__ import annotations

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.github_events import parse_github_event
from app.review_queue import review_queue
from app.runtime_validation_review_bridge import create_runtime_validation_pending_item, enqueue_runtime_pending_item
from app.storage import SQLiteStateStore
from app.wf20_deployment_resume import (
    claim_waiting_workflow_for_request,
    list_waiting_deployment_items,
    mark_waiting_workflow_resumed,
    persist_waiting_for_deployment,
    select_waiting_workflow_for_request,
)

REPO = "marcus937/jarvis-mission-control"
PREVIEW_URL = "https://jmc-preview.vercel.app"


def waiting_request(
    *,
    branch: str = "codex-m2/wf20",
    sha: str = "abcdef1234567890",
    pr_number: int = 183,
    workflow_id: str | None = None,
) -> RuntimeValidationRequest:
    workflow_id = workflow_id or f"wf20-{pr_number}-{sha[:8]}"
    request = RuntimeValidationRequest(
        repo=REPO,
        pr_number=pr_number,
        branch=branch,
        base_branch="agent-integration",
        target_url=None,
        target_url_source="vercel_preview_pending",
        target_url_pending_reason="Timed out waiting for verified Vercel preview deployment readiness.",
        validation_type="playwright",
        requested_by="orchestrator_wf20",
        correlation_id=f"correlation-{workflow_id}",
        workflow_id=workflow_id,
    )
    object.__setattr__(request, "commit_sha", sha)
    return request


def ready_request(
    *,
    branch: str = "codex-m2/wf20",
    sha: str = "abcdef1234567890",
    pr_number: int = 183,
    workflow_id: str | None = None,
) -> RuntimeValidationRequest:
    request = waiting_request(branch=branch, sha=sha, pr_number=pr_number, workflow_id=workflow_id).model_copy(
        update={
            "target_url": PREVIEW_URL,
            "target_url_source": "github_verified_deployment_status_preview_url",
            "target_url_pending_reason": None,
        }
    )
    object.__setattr__(request, "commit_sha", sha)
    return request


def pr_payload(*, branch: str, sha: str, pr_number: int) -> dict[str, object]:
    return {
        "action": "opened",
        "repository": {"full_name": REPO},
        "sender": {"login": "codex"},
        "pull_request": {
            "number": pr_number,
            "head": {"ref": branch, "sha": sha, "repo": {"full_name": REPO}},
            "base": {"ref": "agent-integration", "repo": {"full_name": REPO}},
            "labels": [],
        },
    }


def deployment_payload(
    *,
    branch: str = "codex-m2/wf20",
    sha: str = "abcdef1234567890",
    deployment_id: int = 111,
    deployment_status_id: int = 222,
) -> dict[str, object]:
    return {
        "action": "success",
        "repository": {"full_name": REPO},
        "sender": {"login": "vercel"},
        "deployment": {"id": deployment_id, "ref": branch, "sha": sha, "environment": "Preview"},
        "deployment_status": {
            "id": deployment_status_id,
            "state": "success",
            "environment": "Preview",
            "target_url": PREVIEW_URL,
            "environment_url": PREVIEW_URL,
        },
    }


def create_waiting(request: RuntimeValidationRequest, *, storage: SQLiteStateStore | None = None):
    parsed = parse_github_event(
        "pull_request",
        pr_payload(branch=request.branch or "codex-m2/wf20", sha=getattr(request, "commit_sha", "abcdef"), pr_number=request.pr_number or 183),
    )
    item = create_runtime_validation_pending_item(parsed)
    if storage is None:
        item = enqueue_runtime_pending_item(item)
    return persist_waiting_for_deployment(request, item, storage=storage)


def test_workflow_successfully_enters_waiting_registry_and_matches_deployment() -> None:
    review_queue.reset()
    waiting = create_waiting(waiting_request())
    parsed = parse_github_event("deployment_status", deployment_payload())

    selected = select_waiting_workflow_for_request(ready_request(), parsed)

    assert list_waiting_deployment_items() == [waiting]
    assert selected is not None
    assert selected.id == waiting.id


def test_registry_survives_unrelated_workflow_additions_and_multiple_concurrent_waiters() -> None:
    review_queue.reset()
    create_waiting(waiting_request(branch="codex-m2/old", sha="oldsha123", pr_number=120, workflow_id="old"))
    active = create_waiting(waiting_request(branch="codex-m2/active", sha="activesha123", pr_number=183, workflow_id="active"))
    create_waiting(waiting_request(branch="codex-m2/other", sha="othersha123", pr_number=184, workflow_id="other"))
    parsed = parse_github_event("deployment_status", deployment_payload(branch="codex-m2/active", sha="activesha123"))

    selected = select_waiting_workflow_for_request(
        ready_request(branch="codex-m2/active", sha="activesha123", pr_number=183, workflow_id="active"),
        parsed,
    )

    assert len(list_waiting_deployment_items()) == 3
    assert selected is not None
    assert selected.id == active.id


def test_multiple_prs_same_repository_with_branch_reuse_match_by_sha_first() -> None:
    review_queue.reset()
    create_waiting(waiting_request(branch="codex-m2/reused", sha="sha-one", pr_number=183, workflow_id="wf-one"))
    target = create_waiting(waiting_request(branch="codex-m2/reused", sha="sha-two", pr_number=184, workflow_id="wf-two"))
    parsed = parse_github_event("deployment_status", deployment_payload(branch="codex-m2/reused", sha="sha-two"))

    selected = select_waiting_workflow_for_request(
        ready_request(branch="codex-m2/reused", sha="sha-two", pr_number=184, workflow_id="wf-two"),
        parsed,
    )

    assert selected is not None
    assert selected.id == target.id


def test_identical_sha_on_different_prs_uses_workflow_identity_before_sha() -> None:
    review_queue.reset()
    create_waiting(waiting_request(branch="codex-m2/one", sha="sharedsha", pr_number=183, workflow_id="wf-one"))
    target = create_waiting(waiting_request(branch="codex-m2/two", sha="sharedsha", pr_number=184, workflow_id="wf-two"))
    parsed = parse_github_event("deployment_status", deployment_payload(branch="codex-m2/two", sha="sharedsha"))

    selected = select_waiting_workflow_for_request(
        ready_request(branch="codex-m2/two", sha="sharedsha", pr_number=184, workflow_id="wf-two"),
        parsed,
    )

    assert selected is not None
    assert selected.id == target.id


def test_resumed_workflow_remains_diagnostic_visible_but_is_not_matchable() -> None:
    review_queue.reset()
    waiting = create_waiting(waiting_request())
    parsed = parse_github_event("deployment_status", deployment_payload())

    mark_waiting_workflow_resumed(waiting)

    diagnostic_items = list_waiting_deployment_items()
    assert diagnostic_items == [waiting]
    assert diagnostic_items[0].runtime_validation_status == "deployment_ready_resumed"
    assert select_waiting_workflow_for_request(ready_request(), parsed) is None


def test_deployment_before_persistence_does_not_match_then_matches_after_persistence() -> None:
    review_queue.reset()
    parsed = parse_github_event("deployment_status", deployment_payload())
    request = ready_request()

    assert select_waiting_workflow_for_request(request, parsed) is None
    waiting = create_waiting(waiting_request())

    selected = select_waiting_workflow_for_request(request, parsed)
    assert selected is not None
    assert selected.id == waiting.id


def test_duplicate_deployment_webhook_and_replay_do_not_rematch_after_claim() -> None:
    review_queue.reset()
    waiting = create_waiting(waiting_request())
    parsed = parse_github_event("deployment_status", deployment_payload())
    request = ready_request()

    first = claim_waiting_workflow_for_request(request, parsed)
    second = claim_waiting_workflow_for_request(request, parsed)

    assert first is not None
    assert first.id == waiting.id
    assert second is None


def test_registry_reload_after_restart_uses_persisted_waiting_items(tmp_path) -> None:
    storage = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    waiting = create_waiting(waiting_request(), storage=storage)
    reloaded_storage = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    parsed = parse_github_event("deployment_status", deployment_payload())

    selected = select_waiting_workflow_for_request(ready_request(), parsed, storage=reloaded_storage)

    assert len(list_waiting_deployment_items(storage=reloaded_storage)) == 1
    assert selected is not None
    assert selected.id == waiting.id

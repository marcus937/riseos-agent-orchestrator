import json

from app.queue_contention_validation import ARTIFACT_FILES, REVIEW_ITEM_COUNT, run_queue_contention_validation


def _load_json(tmp_path, key):
    return json.loads((tmp_path / ARTIFACT_FILES[key]).read_text(encoding="utf-8"))


def test_queue_contention_validation_generates_required_artifact_bundle(tmp_path):
    diagnostics = run_queue_contention_validation(tmp_path)

    assert diagnostics["status"] == "passed"
    for artifact_file in ARTIFACT_FILES.values():
        assert (tmp_path / artifact_file).exists()

    assert _load_json(tmp_path, "queue_state")["counters"]["review_queue_count"] == REVIEW_ITEM_COUNT
    assert _load_json(tmp_path, "diagnostics")["artifact_digest"]
    assert "PASSED" in (tmp_path / ARTIFACT_FILES["failure_summary"]).read_text(encoding="utf-8")


def test_claim_locking_claims_each_item_exactly_once(tmp_path):
    run_queue_contention_validation(tmp_path)
    worker_claims = _load_json(tmp_path, "worker_claims")
    diagnostics = _load_json(tmp_path, "diagnostics")

    assert diagnostics["checks"]["claim_locking"] is True
    assert worker_claims["claimed_count"] == REVIEW_ITEM_COUNT
    assert worker_claims["duplicate_claim_item_ids"] == []
    assert all(count == 1 for count in worker_claims["item_claim_counts"].values())


def test_duplicate_prevention_and_duplicate_processing_are_proven(tmp_path):
    run_queue_contention_validation(tmp_path)
    diagnostics = _load_json(tmp_path, "diagnostics")

    assert diagnostics["checks"]["duplicate_prevention"] is True
    assert diagnostics["checks"]["duplicate_processing_prevention"] is True
    assert all(result["prevented"] for result in diagnostics["duplicate_enqueue_results"])
    assert all(count == 1 for count in diagnostics["processing_counts"].values())


def test_worker_assignment_integrity_is_recorded(tmp_path):
    run_queue_contention_validation(tmp_path)
    worker_claims = _load_json(tmp_path, "worker_claims")
    diagnostics = _load_json(tmp_path, "diagnostics")

    assert diagnostics["checks"]["worker_assignment_integrity"] is True
    assert len(worker_claims["item_owners"]) == REVIEW_ITEM_COUNT
    assert all(owner.startswith("review-worker-") for owner in worker_claims["item_owners"].values())


def test_completion_integrity_and_writeback_lifecycle_are_recorded(tmp_path):
    run_queue_contention_validation(tmp_path)
    queue_state = _load_json(tmp_path, "queue_state")
    lifecycle = _load_json(tmp_path, "review_lifecycle")
    diagnostics = _load_json(tmp_path, "diagnostics")

    assert diagnostics["checks"]["completion_integrity"] is True
    assert diagnostics["checks"]["github_writeback_lifecycle"] is True
    assert queue_state["counters"]["approved_for_human_review_count"] == REVIEW_ITEM_COUNT
    assert all(item["status"] == "approved_for_human_review" for item in queue_state["items"])
    assert all(item["lifecycle_stage"] == "review_completed" for item in lifecycle)
    assert all(item["github_writeback_success"] is True for item in lifecycle)

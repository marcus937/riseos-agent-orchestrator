from pathlib import Path

from app.multi_worker_queue_contention_validation import run_validation


def test_multi_worker_validation_claims_each_item_once(tmp_path: Path) -> None:
    result = run_validation(tmp_path, worker_count=4, item_count=8)

    assert result.passed is True
    assert result.failures == []
    assert result.diagnostics["claim_count"] == 8
    assert result.diagnostics["unique_claimed_item_count"] == 8
    assert result.diagnostics["duplicate_claim_items"] == {}


def test_multi_worker_validation_records_worker_assignment_integrity(tmp_path: Path) -> None:
    result = run_validation(tmp_path, worker_count=3, item_count=6)
    worker_claims = (tmp_path / "worker-claims.json").read_text(encoding="utf-8")

    assert result.passed is True
    assert "worker_id" in worker_claims
    assert "contention-item-1" in worker_claims


def test_multi_worker_validation_records_completion_and_writeback_lifecycle(tmp_path: Path) -> None:
    result = run_validation(tmp_path, worker_count=4, item_count=5)

    assert result.passed is True
    assert result.diagnostics["completed_count"] == 5
    assert result.diagnostics["writeback_completed_item_count"] == 5
    assert result.diagnostics["queue_stats"]["counters"]["pending_review_count"] == 0
    assert result.diagnostics["queue_stats"]["counters"]["reviewing_count"] == 0


def test_multi_worker_validation_writes_required_artifact_bundle(tmp_path: Path) -> None:
    result = run_validation(tmp_path, worker_count=2, item_count=3)

    assert result.passed is True
    for artifact_name in [
        "queue-state.json",
        "worker-claims.json",
        "review-lifecycle.json",
        "diagnostics.json",
        "failure-summary.md",
    ]:
        artifact_path = tmp_path / artifact_name
        assert artifact_path.exists()
        assert artifact_path.read_text(encoding="utf-8").strip()

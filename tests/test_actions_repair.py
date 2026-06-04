import json

from app.actions_repair import (
    build_failure_summary,
    build_lifecycle_failure_artifact,
    write_actions_repair_artifacts,
)


def test_failure_summary_generation_extracts_failed_tests_and_error_type() -> None:
    pytest_output = """
FAILED tests/test_observability.py::test_worker_claim - AssertionError
FAILED tests/test_observability.py::test_review_completion - RuntimeError
E   AssertionError: expected worker claim
"""

    summary = build_failure_summary(workflow="ci", status="failed", pytest_output=pytest_output)

    assert summary["workflow"] == "ci"
    assert summary["status"] == "failed"
    assert summary["failed_tests"] == ["test_worker_claim", "test_review_completion"]
    assert summary["error_type"] == "AssertionError"
    assert summary["has_pytest_output"] is True


def test_artifact_creation_writes_json_and_markdown(tmp_path) -> None:
    output_dir = tmp_path / "repair"

    written = write_actions_repair_artifacts(
        output_dir=output_dir,
        workflow="ci",
        status="failed",
        pytest_output="FAILED tests/test_worker.py::test_worker_claim - AssertionError",
    )

    assert written["failure_summary_json"].exists()
    assert written["failure_summary_markdown"].exists()
    summary = json.loads(written["failure_summary_json"].read_text(encoding="utf-8"))
    assert summary["failed_tests"] == ["test_worker_claim"]
    assert "test_worker_claim" in written["failure_summary_markdown"].read_text(encoding="utf-8")


def test_lifecycle_failure_reporting_preserves_states_and_exception(tmp_path) -> None:
    output_dir = tmp_path / "repair"
    queue_state = [{"item_id": "queue-1", "status": "pending_review"}]
    worker_state = {"failed_count": 1}
    lifecycle_state = [{"item_id": "queue-1", "lifecycle_stage": "review_failed"}]

    artifact = build_lifecycle_failure_artifact(
        review_queue_state=queue_state,
        worker_state=worker_state,
        lifecycle_state=lifecycle_state,
        exception_text="RuntimeError: controlled BB2 failure",
        diagnostics_output="recent failure diagnostics",
    )
    written = write_actions_repair_artifacts(
        output_dir=output_dir,
        workflow="bb2-lifecycle-validation",
        status="failed",
        review_queue_state=queue_state,
        worker_state=worker_state,
        lifecycle_state=lifecycle_state,
        exception_text="RuntimeError: controlled BB2 failure",
        diagnostics_output="recent failure diagnostics",
    )

    persisted = json.loads(written["bb2_lifecycle_failure_json"].read_text(encoding="utf-8"))
    assert artifact["review_queue_state"] == queue_state
    assert persisted["worker_state"] == worker_state
    assert persisted["lifecycle_state"] == lifecycle_state
    assert persisted["exception_text"] == "RuntimeError: controlled BB2 failure"
    assert persisted["diagnostics_output"] == "recent failure diagnostics"

import json

from app.queue_lifecycle_validation import ARTIFACT_FILES, REQUIRED_LIFECYCLE_EVENTS, run_queue_lifecycle_validation


def test_queue_lifecycle_validation_generates_artifact_bundle(tmp_path):
    summary = run_queue_lifecycle_validation(tmp_path)

    assert summary["status"] == "passed"
    for artifact_file in ARTIFACT_FILES.values():
        assert (tmp_path / artifact_file).exists()

    timeline = json.loads((tmp_path / ARTIFACT_FILES["timeline"]).read_text(encoding="utf-8"))
    state_transitions = json.loads((tmp_path / ARTIFACT_FILES["state_transitions"]).read_text(encoding="utf-8"))
    correlation = json.loads((tmp_path / ARTIFACT_FILES["correlation_tracking"]).read_text(encoding="utf-8"))
    failures = json.loads((tmp_path / ARTIFACT_FILES["failure_diagnostics"]).read_text(encoding="utf-8"))

    assert len(timeline) >= len(REQUIRED_LIFECYCLE_EVENTS)
    assert len(state_transitions) == len(timeline)
    assert correlation["all_events_have_correlation_id"] is True
    assert failures["failure_persisted_before_retry"]["last_error"] == "controlled lifecycle validation failure"


def test_queue_lifecycle_validation_records_required_success_order(tmp_path):
    run_queue_lifecycle_validation(tmp_path)
    timeline = json.loads((tmp_path / ARTIFACT_FILES["timeline"]).read_text(encoding="utf-8"))
    success_correlation_id = json.loads(
        (tmp_path / ARTIFACT_FILES["correlation_tracking"]).read_text(encoding="utf-8")
    )["success_path_correlation_id"]

    success_events = [
        event["event"] for event in timeline if event["correlation_id"] == success_correlation_id
    ]

    assert success_events == REQUIRED_LIFECYCLE_EVENTS
    assert [event["sequence_number"] for event in timeline if event["correlation_id"] == success_correlation_id] == [1, 2, 3, 4, 5, 6]


def test_queue_lifecycle_validation_proves_retry_behavior(tmp_path):
    run_queue_lifecycle_validation(tmp_path)
    failures = json.loads((tmp_path / ARTIFACT_FILES["failure_diagnostics"]).read_text(encoding="utf-8"))
    retry = failures["retry_after_failure"]

    assert failures["failure_persisted_before_retry"]["status"] == "pending_review"
    assert failures["failure_persisted_before_retry"]["lifecycle_stage"] == "review_failed"
    assert retry["status"] == "approved_for_human_review"
    assert retry["github_writeback_success"] is True
    assert retry["failure_count_preserved"] == 1
    assert retry["last_error_preserved"] == "controlled lifecycle validation failure"

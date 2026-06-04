from pathlib import Path

import pytest

from app import end_to_end_review_validation as validation


def test_sign_payload_uses_github_signature_prefix() -> None:
    signature = validation.sign_payload("secret", b'{"ok":true}')

    assert signature.startswith("sha256=")
    assert len(signature.removeprefix("sha256=")) == 64


def test_build_pull_request_payload_is_deterministic() -> None:
    payload = validation.build_pull_request_payload()

    assert payload["repository"]["full_name"] == validation.DETERMINISTIC_REPO
    assert payload["number"] == validation.DETERMINISTIC_PR_NUMBER
    assert payload["pull_request"]["head"]["sha"] == validation.DETERMINISTIC_HEAD_SHA
    assert payload["pull_request"]["base"]["ref"] == "main"


def test_matching_queue_item_finds_deterministic_review_item() -> None:
    item = {
        "id": "review-1",
        "repo_full_name": validation.DETERMINISTIC_REPO,
        "pr_number": validation.DETERMINISTIC_PR_NUMBER,
        "commit_sha": validation.DETERMINISTIC_HEAD_SHA,
    }

    assert validation.matching_queue_item([item]) == item


def test_assert_writeback_requests_accepts_comment_and_label_requests() -> None:
    base = f"/repos/{validation.DETERMINISTIC_REPO}/issues/{validation.DETERMINISTIC_PR_NUMBER}"

    validation.assert_writeback_requests(
        [
            {
                "method": "POST",
                "path": f"{base}/comments",
                "api_base_url": "http://127.0.0.1:9001",
                "body": {"body": "review"},
            },
            {
                "method": "POST",
                "path": f"{base}/labels",
                "api_base_url": "http://127.0.0.1:9001",
                "body": {"labels": ["bb2-approved"]},
            },
        ],
        "http://127.0.0.1:9001",
    )


def test_assert_writeback_requests_rejects_missing_label_request() -> None:
    base = f"/repos/{validation.DETERMINISTIC_REPO}/issues/{validation.DETERMINISTIC_PR_NUMBER}"

    with pytest.raises(validation.ValidationError, match="label request"):
        validation.assert_writeback_requests(
            [
                {
                    "method": "POST",
                    "path": f"{base}/comments",
                    "api_base_url": "http://127.0.0.1:9001",
                    "body": {"body": "review"},
                }
            ],
            "http://127.0.0.1:9001",
        )


def test_assert_lifecycle_complete_requires_all_stage_evidence() -> None:
    validation.assert_lifecycle_complete(
        {
            "status": "approved_for_human_review",
            "lifecycle_stage": "review_completed",
            "github_writeback_success": True,
            "queued_at": "2026-06-04T21:00:00Z",
            "worker_claimed_at": "2026-06-04T21:00:01Z",
            "review_started_at": "2026-06-04T21:00:02Z",
            "github_writeback_started_at": "2026-06-04T21:00:03Z",
            "github_writeback_completed_at": "2026-06-04T21:00:04Z",
            "review_completed_at": "2026-06-04T21:00:05Z",
        }
    )


def test_write_lifecycle_summary_records_failed_stage(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle-summary.md"

    validation.write_lifecycle_summary(
        path,
        validation.ValidationResult(False, "worker_claim", "Worker never claimed the item."),
    )

    text = path.read_text(encoding="utf-8")
    assert "FAILED" in text
    assert "Failed stage: worker_claim" in text


def test_write_lifecycle_summary_records_passed_state(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle-summary.md"

    validation.write_lifecycle_summary(path, validation.ValidationResult(True), {"writeback_attempt": 2})

    text = path.read_text(encoding="utf-8")
    assert "PASSED" in text
    assert "writeback_attempt: 2" in text

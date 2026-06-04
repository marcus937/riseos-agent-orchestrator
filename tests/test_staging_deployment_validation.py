import json

import pytest

from scripts import staging_deployment_validation as validation


def test_sign_payload_uses_github_signature_prefix() -> None:
    signature = validation.sign_payload("secret", b'{"ok":true}')

    assert signature.startswith("sha256=")
    assert len(signature.removeprefix("sha256=")) == 64


def test_assert_staging_lifecycle_accepts_completed_writeback_item() -> None:
    validation.assert_staging_lifecycle(
        [
            {
                "status": "approved_for_human_review",
                "lifecycle_stage": "review_completed",
                "worker_claimed_at": "2026-06-04T20:00:00Z",
                "review_started_at": "2026-06-04T20:00:01Z",
                "review_completed_at": "2026-06-04T20:00:04Z",
                "github_writeback_started_at": "2026-06-04T20:00:02Z",
                "github_writeback_completed_at": "2026-06-04T20:00:03Z",
                "github_writeback_success": True,
            }
        ]
    )


def test_assert_staging_lifecycle_rejects_missing_worker_claim() -> None:
    with pytest.raises(validation.ValidationError, match="worker_claimed_at"):
        validation.assert_staging_lifecycle(
            [
                {
                    "status": "approved_for_human_review",
                    "lifecycle_stage": "review_completed",
                    "review_started_at": "2026-06-04T20:00:01Z",
                    "review_completed_at": "2026-06-04T20:00:04Z",
                    "github_writeback_started_at": "2026-06-04T20:00:02Z",
                    "github_writeback_completed_at": "2026-06-04T20:00:03Z",
                    "github_writeback_success": True,
                }
            ]
        )


def test_build_pull_request_payload_is_non_production() -> None:
    payload = validation.build_pull_request_payload()

    assert payload["repository"]["full_name"] == "riseos/staging-validation"
    assert payload["pull_request"]["base"]["ref"] == "main"
    assert payload["pull_request"]["head"]["ref"].startswith("circuit/")
    assert json.dumps(payload)

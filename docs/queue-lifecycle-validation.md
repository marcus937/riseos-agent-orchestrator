# Queue Lifecycle Validation

The Queue Lifecycle Validation workflow is a deterministic GitHub Actions check for the BB2 review queue lifecycle. It uses isolated SQLite test state and synthetic review work items. It does not call production services, mutate repositories, perform real GitHub writeback, deploy code, or change secrets.

## What It Validates

The validator covers the lifecycle required by BB2 Phase 3:

- `review_work_item` creation and persistence
- `worker_claimed`
- `review_started`
- `review_completed`
- `github_writeback_started`
- `github_writeback_completed`
- correlation ID propagation across lifecycle artifacts
- failure diagnostics persistence
- retry behavior after a controlled failure
- lifecycle ordering for the success path

The writeback events are mocked lifecycle markers only. They prove that the queue item records writeback start/completion metadata without posting comments, applying labels, or contacting GitHub.

## Local Command

```bash
export APP_ENV=ci
export GITHUB_WEBHOOK_SECRET=ci-webhook-secret
export ORCHESTRATOR_ADMIN_TOKEN=ci-admin-token
export ENABLE_OPENAI_REVIEW=false
export ENABLE_GITHUB_CONTEXT_HYDRATION=false
export ENABLE_GITHUB_WRITEBACK=false
export ENABLE_TASK_DISPATCH=false
pytest tests/test_queue_lifecycle_validation.py
python -m app.queue_lifecycle_validation --artifact-dir queue-lifecycle-validation-artifacts
```

## GitHub Actions Artifact Bundle

The workflow uploads `queue-lifecycle-validation-artifacts` on every run. The bundle contains:

- `lifecycle-timeline.json`: ordered lifecycle events with item IDs, correlation IDs, status, stage, and timestamps.
- `state-transition-log.json`: compact transition log for BB2 inspection.
- `correlation-tracking.json`: correlation IDs and events grouped by correlation ID.
- `failure-diagnostics.json`: controlled failure evidence, persisted exception text, and retry outcome.
- `validation-summary.json`: machine-readable pass/fail summary.
- `validation-summary.md`: human-readable summary.
- `queue-lifecycle-results.xml`: pytest JUnit report.

## Safety Guarantees

The workflow sets these flags to false:

- `ENABLE_OPENAI_REVIEW`
- `ENABLE_GITHUB_CONTEXT_HYDRATION`
- `ENABLE_GITHUB_WRITEBACK`
- `ENABLE_TASK_DISPATCH`

The artifact builder uses a temporary SQLite database inside the artifact directory and synthetic work items on a `circuit/*` branch. No production database, GitHub token, OpenAI credential, or Slack secret is required.

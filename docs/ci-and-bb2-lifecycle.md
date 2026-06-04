# CI and BB2 Lifecycle Validation

This repository uses GitHub Actions as the interim execution layer for Circuit and BB2 review loops. The workflows are test-only: they do not deploy, mutate production services, push commits, close issues, merge pull requests, or write secrets to logs.

## Local CI Test Command

Install the package and development test dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Run the same core checks as the standard CI workflow:

```bash
python -m compileall app tests
python -c "import app.main; import app.review_queue; import app.review_worker"
pytest
```

If lint tooling is later configured in `pyproject.toml`, the CI workflow will run it when the matching command is available.

## Local BB2 Lifecycle Test Command

The BB2 lifecycle validation is mocked and local-only. It does not require real OpenAI credentials or real GitHub writeback credentials.

```bash
export APP_ENV=ci
export GITHUB_WEBHOOK_SECRET=ci-webhook-secret
export ORCHESTRATOR_ADMIN_TOKEN=ci-admin-token
export ENABLE_OPENAI_REVIEW=false
export ENABLE_GITHUB_CONTEXT_HYDRATION=false
export ENABLE_GITHUB_WRITEBACK=false
export ENABLE_TASK_DISPATCH=false
pytest tests/test_bb2_lifecycle_validation.py
```

The lifecycle tests cover queue item creation, `review_work_item` persistence, worker claim transitions, lifecycle event persistence, `review_failed`, exception text visibility, `review_completed`, mocked GitHub writeback success/failure, diagnostics endpoints, and disabled feature flag paths.

## GitHub Actions Artifacts

On failure, the workflows upload artifacts:

- `ci-failure-artifacts` from the standard CI workflow.
- `bb2-lifecycle-failure-artifacts` from the BB2 lifecycle workflow.

The most useful files are the pytest JUnit XML reports:

- `pytest-results.xml`
- `bb2-lifecycle-results.xml`

Use these reports to identify failing test names, assertion messages, and error traces. If no report exists, inspect the failed Actions step log first; setup or import failures can happen before pytest writes XML.

## Circuit Repair Loop

When a Circuit PR fails Actions:

1. Open the failed workflow run for the PR commit.
2. Read the first failing step before scanning later noise.
3. Download artifacts when present and inspect the pytest XML failure names/messages.
4. Patch only the smallest code or test area tied to the failure.
5. Re-run the local command that matches the failed workflow.
6. Push another commit to the same `circuit/*` branch and let Actions rerun.

BB2 should use the Actions result, failure artifacts, and PR diff together before deciding whether the PR is ready for human review.

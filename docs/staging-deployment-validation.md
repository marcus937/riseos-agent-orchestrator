# Staging Deployment Validation

This repository includes a staging-only GitHub Actions workflow that validates the orchestrator as a running FastAPI application without touching production infrastructure.

## Safety Model

- The workflow runs on a GitHub-hosted runner only.
- The app binds to `127.0.0.1` and uses an isolated temporary SQLite database.
- `APP_ENV` is set to `staging-validation`.
- The GitHub writeback client points at a local mock GitHub API through `GITHUB_API_BASE_URL`.
- The token used by the validation run is a non-secret placeholder accepted only by the local mock API.
- No SSH, production deploy target, production secret, branch mutation, merge, or real GitHub write is used.

## What It Validates

The validation helper starts from the deployed HTTP surface, not only unit tests:

1. Waits for `/health` to report readiness.
2. Sends a signed `pull_request` webhook to `/webhooks/github`.
3. Lets the background worker claim the queued review item.
4. Processes the review in dry-run mode.
5. Executes GitHub writeback against the local mock API.
6. Verifies these lifecycle markers:
   - `worker_claimed_at`
   - `review_started_at`
   - `github_writeback_started_at`
   - `github_writeback_completed_at`
   - `review_completed_at`
7. Captures queue, lifecycle, worker, health, event, failure, app log, and mock API artifacts.

## Artifact Bundle

The workflow uploads `staging-validation-artifacts`, including:

- `health.json`
- `webhook-response.json`
- `diagnostics.json`
- `orchestrator.log`
- `mock-github.log`
- `orchestrator.db`
- `pytest-results.xml`

## Human Review Boundary

This workflow provides deployment validation evidence for BB2 and Marcus. It does not approve, merge, deploy, mutate production, or bypass branch protection.

# Production Troubleshooting

Use these read-only diagnostics when BB2 review processing stalls or GitHub writeback does not appear.

## First Checks

1. Confirm the service is alive:

   ```bash
   curl https://<orchestrator-host>/health
   ```

2. Check webhook and queue health:

   ```bash
   curl https://<orchestrator-host>/debug/health
   curl https://<orchestrator-host>/debug/review-queue/stats
   ```

3. Review the recent webhook correlation keys:

   ```bash
   curl https://<orchestrator-host>/debug/recent-events
   ```

   Each event includes a `correlation_key` such as `owner/repo:pr:123`, `owner/repo:issue:24`, or `owner/repo:commit:<sha>`.

## Failure Stage Map

Use `/debug/review-lifecycle` to see the latest known stage for each review item:

- `review_queued`: the webhook created or deduplicated a queue item.
- `worker_claimed`: a background worker claimed the item.
- `review_started`: review processing began.
- `openai_review_attempted`: OpenAI review was enabled and requested.
- `openai_review_succeeded`: OpenAI returned a validated review decision.
- `openai_review_failed`: OpenAI review failed or returned invalid output.
- `review_completed`: local review processing completed.
- `review_failed`: processing raised an exception.
- `github_writeback_started`: GitHub comment/label writeback began.
- `github_writeback_completed`: GitHub writeback finished; inspect `github_writeback_success`.

## Queue And Worker Views

Queue stats:

```bash
curl https://<orchestrator-host>/debug/review-queue/stats
```

This shows total queued items, per-status counts, pending age, and failure totals.

Worker stats:

```bash
curl https://<orchestrator-host>/debug/workers/stats
```

This shows whether auto-processing is enabled, how many items were claimed, how many are actively reviewing, and the latest claim/completion/failure timestamps.

Recent failures:

```bash
curl https://<orchestrator-host>/debug/recent-failures
```

This view persists exception text on review work items so BB2, Marcus, and Circuit can triage without direct database access.

## Secure Debug Reads

Read-only debug endpoints are public by default for local testing. In production, set:

```bash
REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true
ORCHESTRATOR_ADMIN_TOKEN=<managed-secret>
```

Then include:

```bash
X-Orchestrator-Admin-Token: <managed-secret>
```

## Expected Lifecycle

For a full webhook to GitHub writeback path with all production flags enabled, the item should advance through:

`review_queued` -> `worker_claimed` -> `review_started` -> `openai_review_attempted` -> `openai_review_succeeded` -> `review_completed` -> `github_writeback_started` -> `github_writeback_completed`

If OpenAI fails, expect `openai_review_failed` plus a `last_error`. If an unhandled processing exception occurs, expect `review_failed` plus a persisted `last_error`.
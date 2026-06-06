# Dispatch Correlation Timeline

Issue: ORCH-003 Dispatch Idempotency and Correlation Tracking

## Correlation ID

Dispatch diagnostics now use a deterministic `orch-<hash>` correlation ID derived from the stable work target:

1. Pull request: `<repo>:pr:<number>`
2. Issue: `<repo>:issue:<number>`
3. Commit-only event: `<repo>:commit:<sha>`
4. Fallback event scope: `<repo>:event:<event_type>`

This keeps all retry, duplicate, Slack, and lifecycle records for the same task joined without exposing the full source key in every operator-facing message.

## End-to-End Timeline

1. GitHub sends an `issues`, `issue_comment`, `push`, or `pull_request` webhook to `POST /webhooks/github`.
2. The orchestrator verifies the GitHub HMAC before parsing or dispatch decisions.
3. `event_record_from_parsed` stores the webhook delivery or derived webhook identity and assigns the deterministic correlation ID.
4. Duplicate webhook deliveries are suppressed before queue or Slack dispatch work. Duplicate logs now include `duplicate_source`:
   - `github_delivery_header` when suppression came from the `X-GitHub-Delivery` value.
   - `derived_webhook_identity` when suppression came from the fallback identity built from event type, repo, action, label, branch, SHA, issue number, and PR number.
5. Agent-ready issue dispatch uses the same correlation ID in the Slack dispatch result and Slack notification body.
6. Review queue and worker lifecycle logs derive the same correlation ID from the queued work item fields, so `review_queued`, `worker_claimed`, `review_started`, `review_completed`, and failure logs can be grouped with the original webhook and Slack notification.

## Duplicate Trigger Source

The current duplicate suppression source is the webhook event intake, not Slack posting itself. The orchestrator already claims issue dispatches by issue key before posting to Slack, so duplicate Slack notifications can only occur when the process-local claim registry is lost or when persistent storage is unavailable. Persistent storage (`ORCHESTRATOR_DB_PATH`) is therefore the operational control that prevents restart-driven duplicate Slack notifications.

For webhook-level duplicates, the primary source is repeated GitHub delivery IDs when the `X-GitHub-Delivery` header is present. Without that header, the source is the derived webhook identity fallback.

## Operator Query Pattern

Search logs or debug event output by `correlation_id`. A healthy single dispatch should show at most one successful Slack notification for the ID, followed by one queue/worker lifecycle for each accepted review-triggering event. Duplicate entries should show `webhook_duplicate_suppressed` or `slack_issue_dispatch_duplicate_suppressed` rather than another Slack success.

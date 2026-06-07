# JMC Telemetry Contract Audit

Issue: #70
Branch audited: `agent-integration`
Date: 2026-06-07
Task type: Documentation-only task.

## Purpose

This document audits the current RiseOS Agent Orchestrator telemetry surfaces that could feed Jarvis Mission Control (JMC), with special attention to the JMC Workers section. It inventories current HTTP APIs, event records, worker queue data, operational log events, Slack/Hermes dispatch feeds, and GitHub review writeback contracts. It also identifies missing canonical contracts and recommends where telemetry ownership should live.

## Audit Scope And Sources

Audited source files on `agent-integration`:

- `README.md`
- `app/main.py`
- `app/config.py`
- `app/event_store.py`
- `app/github_events.py`
- `app/review_workflow.py`
- `app/review_queue.py`
- `app/review_worker.py`
- `app/storage.py`
- `app/operational_logging.py`
- `app/github_context.py`
- `app/github_writeback.py`
- `app/hermes_dispatch.py`
- `app/slack_issue_dispatch.py`
- `app/task_dispatch.py`
- `pyproject.toml`

Reference context from Builder-attached files:

- `agent_files/service_ownership_map.md`
- `agent_files/Jarvis Brain 02_endpoints.md`
- `agent_files/Jarvis Brain 03_data_models.md`

Direct repository clone was unavailable from the execution environment, so this audit used GitHub file reads against the `agent-integration` branch and targeted local reference files.

## Executive Summary

The Orchestrator already exposes useful JMC-ready telemetry, but it is not yet normalized as a canonical Mission Control contract. Current telemetry is spread across debug HTTP endpoints, persisted SQLite rows, in-memory fallbacks, structured JSON log events, GitHub comments/labels, Slack dispatch messages, and Hermes dispatch payloads. The strongest current source for the JMC Workers section is the Orchestrator, because it owns queue intake, review work items, worker claims, lifecycle stage changes, review outcomes, GitHub writeback, task dispatch, and Hermes validation routing.

Jarvis Brain should not be the primary owner for Orchestrator worker telemetry. Brain should contribute product/domain telemetry and higher-order reasoning context where it owns those flows, but the Orchestrator should publish the canonical worker telemetry stream and REST snapshot contract. JMC should consume a combined model: Orchestrator as the source of truth for automation execution and agent lifecycle state, Jarvis Brain as an optional enrichment source for business context, summaries, and cross-system insight.

ARCHITECT_REVIEW_REQUIRED: The recommended canonical telemetry envelope changes the ownership boundary between Orchestrator, Jarvis Brain, Hermes, and JMC. Architecture review should confirm that Orchestrator is the system of record for worker telemetry and that Brain enrichment remains downstream or sidecar, not required for basic operational truth.

## Current Telemetry Surfaces

### HTTP Endpoints

The live FastAPI application currently exposes these telemetry-relevant endpoints:

| Endpoint | Response model | Current JMC use | Notes |
| --- | --- | --- | --- |
| `GET /health` | `dict[str, str]` | Service status indicator | Returns `{ "status": "ok" }`. |
| `GET /debug/recent-events` | `list[EventRecord]` | Event activity feed | Reads persisted SQLite events when configured, otherwise in-memory event store. |
| `GET /debug/health` | `DebugHealth` | System health counters | Includes webhook counts, uptime, and review queue counters. |
| `GET /debug/review-queue` | `list[ReviewWorkItem]` | Worker queue table | Main current snapshot of queue items and agent lifecycle state. |
| `GET /debug/review-queue/stats` | `ReviewQueueStats` | Queue summary cards | Includes queue counters, oldest pending age, newest item age, and failure counts. |
| `GET /debug/workers/stats` | `WorkerStats` | Workers overview | Includes auto-processing enabled flag, claimed count, active reviewing count, completed count, failed count, and timestamps. |
| `GET /debug/review-lifecycle` | `list[ReviewLifecycleVisibility]` | Lifecycle timeline | JMC-ready lifecycle projection for review work items. |
| `GET /debug/recent-failures` | `list[RecentFailure]` | Failure panel | Bounded recent failure list, currently defaulting to 20 in builder. |
| `GET /debug/review-queue/{item_id}` | `ReviewWorkItem` | Item detail drawer | Single queue item lookup. |
| `POST /debug/review-queue/{item_id}/process` | `ReviewProcessResponse` | Manual process action | Requires `X-Orchestrator-Admin-Token`. Mutates review item processing state. |
| `POST /webhooks/github` | `WebhookAcceptedResponse` | Intake status and activity source | Main GitHub webhook entrypoint, verifies HMAC signatures, records accepted/rejected/duplicate events. |

Debug read endpoints can be public by default for local testing. When `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true`, all `/debug/*` read endpoints require `X-Orchestrator-Admin-Token`. The process endpoint always requires the admin token.

### Websocket Streams

No websocket endpoint was found in the audited Orchestrator files. Current JMC integration should assume polling or server-side log ingestion until a canonical stream is added.

Recommended future stream:

- `GET /telemetry/stream` using Server-Sent Events, or `WS /telemetry/ws` if bidirectional control is needed.
- Event payloads should reuse the canonical telemetry envelope recommended below.
- A replay cursor should be supported once the persisted event store becomes the canonical telemetry source.

### Event Buses And Persistent Stores

The current event and queue state model has two storage modes:

| Store | Runtime class/table | Data captured | Persistence |
| --- | --- | --- | --- |
| In-memory event store | `InMemoryEventStore` | accepted/rejected/duplicate webhook counters and recent `EventRecord` objects | Process lifetime only |
| SQLite event store | `event_records` table | event identity, diagnostic stage, correlation key, repo, branch, SHA, issue, PR, received timestamp, raw action | Persistent when `ORCHESTRATOR_DB_PATH` is set |
| In-memory review queue | `InMemoryReviewQueue` | review work items and queue counters | Process lifetime only |
| SQLite review queue | `review_work_items` table | review item identity, status, lifecycle timestamps, writeback status, failure fields | Persistent when `ORCHESTRATOR_DB_PATH` is set |
| SQLite dispatch registry | `issue_dispatch_claims` table | issue dispatch deduplication claims | Persistent when `ORCHESTRATOR_DB_PATH` is set |

The current store is an internal operational store, not a public telemetry contract. It is still a strong candidate backing source for JMC snapshots.

### Operational Logging Feed

`app/operational_logging.py` emits structured JSON logs via logger `riseos_agent_orchestrator`. These logs are the closest current event feed and include stable event names such as:

- `webhook_accepted`
- `webhook_duplicate_suppressed`
- `review_queued`
- `worker_claimed`
- `review_started`
- `review_completed`
- `review_failed`
- `auto_review_processing_started`
- `auto_review_processing_succeeded`
- `auto_review_processing_failed`
- `openai_review_attempted`
- `openai_review_succeeded`
- `openai_review_failed`
- `github_writeback_started`
- `github_writeback_completed`
- `slack_issue_dispatch_succeeded`
- `slack_issue_dispatch_failed`
- `slack_issue_dispatch_duplicate_suppressed`
- `slack_issue_dispatch_missing_config`
- `slack_issue_dispatch_invalid_repo`
- `slack_issue_dispatch_skipped`
- `hermes_dispatch_succeeded`
- `hermes_dispatch_blocked`
- `hermes_dispatch_failed`
- `hermes_dispatch_duplicate_suppressed`
- `hermes_dispatch_skipped`

Hermes dispatch also logs route evaluation and eligibility details from `app/hermes_dispatch.py`, including:

- `hermes_route_evaluated`
- `hermes_dispatch_eligibility_evaluated`
- `hermes_post_attempted`
- `hermes_post_completed`
- `hermes_post_failed`

These logs include correlation IDs, repo names, event types, issue/PR numbers, SHAs, labels, node routing, status, skipped reasons, and errors where relevant. Secrets are redacted in Hermes-specific logging.

## Current API Contracts And Payload Schemas

### `WebhookAcceptedResponse`

Source: `app/github_events.py`

```json
{
  "status": "accepted",
  "event_accepted": true,
  "event_type": "issues | issue_comment | push | pull_request | pull_request_review | ping",
  "repository": "owner/repo",
  "repo": "owner/repo",
  "action": "opened",
  "task_state": "working | review_needed | ...",
  "issue_number": 70,
  "pull_request_number": null,
  "commit_sha": "abc123",
  "review_context": {},
  "next_intended_action": "Build review prompt and prepare BB/Jarvis Architect review stub."
}
```

### `EventRecord`

Source: `app/event_store.py`

```json
{
  "event_id": "github-delivery:...",
  "github_event": "issues",
  "diagnostic_stage": "webhook_accepted",
  "correlation_id": "...",
  "correlation_key": "...",
  "repo_full_name": "owner/repo",
  "branch": "agent-integration",
  "commit_sha": "abc123",
  "issue_number": 70,
  "pr_number": null,
  "received_at": "2026-06-07T19:16:40Z",
  "raw_action": "opened"
}
```

### `DebugHealth`

Source: `app/event_store.py`

```json
{
  "webhook_count": 12,
  "accepted_count": 10,
  "rejected_count": 2,
  "uptime": 123.45,
  "review_queue_count": 4,
  "pending_review_count": 1,
  "reviewing_count": 1,
  "needs_changes_count": 0,
  "approved_count": 2,
  "approved_for_human_review_count": 2,
  "blocked_count": 0
}
```

### `ReviewWorkItem`

Source: `app/review_queue.py`

```json
{
  "id": "uuid",
  "created_at": "2026-06-07T19:16:40Z",
  "updated_at": "2026-06-07T19:20:00Z",
  "repo_full_name": "owner/repo",
  "event_type": "pull_request",
  "branch": "agent-integration",
  "commit_sha": "abc123",
  "issue_number": null,
  "pr_number": 12,
  "status": "pending_review | reviewing | needs_changes | approved_for_human_review | blocked",
  "lifecycle_stage": "review_queued | worker_claimed | review_started | openai_review_attempted | openai_review_succeeded | openai_review_failed | review_completed | review_failed | github_writeback_started | github_writeback_completed",
  "worker_claimed_at": null,
  "review_started_at": null,
  "openai_review_attempted_at": null,
  "openai_review_completed_at": null,
  "review_completed_at": null,
  "github_writeback_started_at": null,
  "github_writeback_completed_at": null,
  "github_writeback_success": null,
  "failure_count": 0,
  "last_failure_at": null,
  "last_error": null
}
```

### `ReviewQueueStats`

Source: `app/review_queue.py`

```json
{
  "counters": {
    "review_queue_count": 4,
    "pending_review_count": 1,
    "reviewing_count": 1,
    "needs_changes_count": 0,
    "approved_count": 2,
    "approved_for_human_review_count": 2,
    "blocked_count": 0
  },
  "oldest_pending_age_seconds": 90.1,
  "newest_item_age_seconds": 10.2,
  "failure_count": 1,
  "recent_failure_count": 1,
  "last_failure_at": "2026-06-07T19:20:00Z"
}
```

### `WorkerStats`

Source: `app/review_queue.py`

```json
{
  "auto_processing_enabled": false,
  "claimed_count": 3,
  "active_reviewing_count": 1,
  "completed_count": 2,
  "failed_count": 1,
  "last_claimed_at": "2026-06-07T19:18:00Z",
  "last_review_completed_at": "2026-06-07T19:20:00Z",
  "last_failure_at": "2026-06-07T19:19:00Z"
}
```

### `ReviewLifecycleVisibility`

Source: `app/review_queue.py`

This is a presentation projection of `ReviewWorkItem`, carrying item ID, repo, event type, status, lifecycle stage, lifecycle timestamps, failure count, and last error. It is the strongest current match for a JMC item timeline.

### `RecentFailure`

Source: `app/review_queue.py`

```json
{
  "item_id": "uuid",
  "repo_full_name": "owner/repo",
  "event_type": "pull_request",
  "status": "reviewing",
  "lifecycle_stage": "review_failed",
  "failure_count": 1,
  "last_failure_at": "2026-06-07T19:19:00Z",
  "last_error": "Recovered stale worker claim after restart."
}
```

### `ReviewProcessResponse`

Source: `app/review_queue.py`

```json
{
  "work_item": {},
  "decision": {},
  "intended_next_actions": [],
  "changed_files": [],
  "diff_summary": null,
  "diff_patches": [],
  "patch_truncated": false,
  "github_context_available": false,
  "github_context_error": null,
  "github_writeback_attempted": false,
  "github_writeback_success": false,
  "github_writeback_error": null,
  "task_dispatch_attempted": false,
  "task_dispatch_success": false,
  "task_dispatch_issue_number": null,
  "task_dispatch_error": null,
  "openai_review_attempted": false,
  "openai_review_success": false,
  "openai_review_error": null,
  "reviewer_model": null,
  "dry_run": true
}
```

### `GitHubContextResult`

Source: `app/github_context.py`

```json
{
  "changed_files": ["app/main.py"],
  "diff_summary": "compare main...agent-integration: 1 changed file(s), +10/-2.",
  "diff_patches": [
    {
      "filename": "app/main.py",
      "status": "modified",
      "additions": 10,
      "deletions": 2,
      "patch": "@@ ..."
    }
  ],
  "patch_truncated": false,
  "github_context_available": true,
  "github_context_error": null
}
```

### `SlackIssueDispatchResult`

Source: `app/slack_issue_dispatch.py`

```json
{
  "attempted": true,
  "success": true,
  "issue_key": "owner/repo#70",
  "correlation_id": "...",
  "skipped_reason": null,
  "error": null,
  "message": "@circuit-forge Circuit task ready..."
}
```

### `HermesDispatchResult`

Source: `app/hermes_dispatch.py`

```json
{
  "attempted": true,
  "success": false,
  "status": "PASSED | FAILED | BLOCKED | SKIPPED",
  "hermes_node": "M2 | DGX",
  "dispatch_key": "M2:owner/repo:pr:12:sha:abc123:target:https://...",
  "correlation_id": "hermes-m2-owner-repo-pr-12-abc123",
  "skipped_reason": null,
  "error": null,
  "message": null,
  "comment": null,
  "label": "agent-verified | agent-revisions | agent-blocked",
  "job_id": "job-123"
}
```

### `TaskDispatchResult`

Source: `app/task_dispatch.py`

```json
{
  "attempted": true,
  "success": true,
  "issue_number": 42,
  "error": null,
  "assignment_body": "## Circuit Assignment..."
}
```

## Data Currently Available By JMC Domain

| JMC domain | Current data exists? | Current source | Notes |
| --- | --- | --- | --- |
| Workers | Partial | `/debug/workers/stats`, `ReviewWorkItem`, operational logs | Supports aggregate counts and lifecycle timestamps, but not worker identity, heartbeat, capacity, version, or host. |
| Circuit | Partial | Slack issue dispatch, task dispatch labels/comments, queue items | Circuit assignments are represented through issue labels/comments, not a first-class agent telemetry model. |
| Hermes | Partial | `HermesDispatchResult`, Hermes logs, GitHub comments/labels | Strong routing and result metadata, but no canonical artifact URL contract or live job status stream. |
| BB2 | Partial | review decision labels, `ReviewProcessResponse`, writeback comments | BB2 decisions are visible as labels and review response fields; no dedicated BB2 review entity endpoint. |
| Routing decisions | Partial | `build_review_workflow`, Hermes route logs, task dispatch selector | Routing outcomes exist but are not exposed as a normalized route-decision history. |
| Queue depth | Yes | `/debug/review-queue/stats`, `/debug/health` | Current best-covered JMC metric. |
| Runtime validations | Partial | Hermes dispatch payload/result, GitHub labels/comments | Runtime validation requests and results exist but require cross-reading logs, labels, comments, and Hermes job output. |
| Agent lifecycle events | Partial | `ReviewLifecycleStage`, operational logs, lifecycle endpoint | Good review lifecycle, but not a generic multi-agent lifecycle contract. |

## Gap Analysis For JMC Workers Section

The current Orchestrator can power an MVP worker dashboard, but not a full operational Workers section.

### Existing Enough For MVP

- Queue depth and status breakdown.
- Pending/reviewing/approved/blocked counts.
- Oldest pending age.
- Active reviewing count.
- Review item lifecycle timestamps.
- Recent failures.
- Review outcome status.
- Auto-processing enabled flag.
- Accepted/rejected webhook counters.
- Correlation IDs across webhook, queue, Slack, Hermes, and review flows.

### Missing Or Weak Contracts

| Missing contract | Why JMC needs it | Recommended owner |
| --- | --- | --- |
| Worker identity | Distinguish Circuit Forge, BB2 reviewer worker, Hermes dispatcher, and future agents | Orchestrator |
| Worker heartbeat | Show online, idle, stale, degraded, or offline state | Orchestrator |
| Worker capacity/concurrency | Show whether work is bottlenecked by capacity | Orchestrator |
| Worker version/build | Diagnose whether an agent or runtime is on the expected release | Orchestrator with deployment metadata |
| Worker host/node | Separate M2, DGX, cloud worker, or local process | Orchestrator and Hermes |
| Queue item ownership | Show which worker claimed which item | Orchestrator |
| Claim timeout and retry policy | Explain stale or recycled work | Orchestrator |
| Route decision trace | Explain why an item went to BB2, Circuit, Hermes M2, or DGX | Orchestrator |
| Canonical event stream | Allow JMC to update without polling debug endpoints | Orchestrator |
| Artifact references | Link screenshots, logs, traces, and validation output | Hermes, normalized by Orchestrator |
| Security/read scope | Replace debug endpoints with production telemetry endpoints | Orchestrator |
| JMC component schema | Keep frontend from coupling to debug/internal models | Orchestrator + JMC |
| Brain enrichment contract | Add human-readable summaries and domain context without blocking operational truth | Jarvis Brain |

## Recommended Canonical Telemetry Contract

The recommended model is a combined snapshot plus event stream contract.

### Telemetry Envelope

Every telemetry event should use this envelope:

```json
{
  "schema_version": "jmc.telemetry.v1",
  "event_id": "uuid-or-derived-id",
  "event_type": "worker.lifecycle.changed",
  "occurred_at": "2026-06-07T19:20:00Z",
  "emitted_at": "2026-06-07T19:20:01Z",
  "source": "orchestrator",
  "correlation_id": "orch-...",
  "repo_full_name": "owner/repo",
  "branch": "agent-integration",
  "commit_sha": "abc123",
  "subject": {
    "type": "issue | pr | commit | queue_item | hermes_job",
    "number": 70,
    "url": "https://github.com/owner/repo/issues/70"
  },
  "actor": {
    "type": "worker | system | user | github | hermes | bb2",
    "id": "circuit-forge",
    "display_name": "Circuit Forge"
  },
  "payload": {},
  "links": []
}
```

### Worker Snapshot

Recommended endpoint: `GET /telemetry/workers`

```json
{
  "schema_version": "jmc.workers.v1",
  "generated_at": "2026-06-07T19:20:00Z",
  "workers": [
    {
      "worker_id": "orchestrator-review-worker",
      "display_name": "Orchestrator Review Worker",
      "kind": "review_processor",
      "status": "idle | active | degraded | offline | disabled",
      "auto_processing_enabled": false,
      "active_item_count": 0,
      "claimed_count": 3,
      "completed_count": 2,
      "failed_count": 1,
      "last_heartbeat_at": "2026-06-07T19:20:00Z",
      "last_claimed_at": "2026-06-07T19:18:00Z",
      "last_completed_at": "2026-06-07T19:20:00Z",
      "last_failure_at": null,
      "capacity": {
        "max_concurrent": 1,
        "available_slots": 1
      },
      "runtime": {
        "service": "riseos-agent-orchestrator",
        "version": "0.1.0",
        "env": "local"
      }
    }
  ]
}
```

### Queue Snapshot

Recommended endpoint: `GET /telemetry/queue`

```json
{
  "schema_version": "jmc.queue.v1",
  "generated_at": "2026-06-07T19:20:00Z",
  "counters": {},
  "oldest_pending_age_seconds": 90.1,
  "items": [
    {
      "queue_item_id": "uuid",
      "status": "pending_review",
      "lifecycle_stage": "review_queued",
      "repo_full_name": "owner/repo",
      "branch": "agent-integration",
      "commit_sha": "abc123",
      "issue_number": 70,
      "pr_number": null,
      "assigned_worker_id": null,
      "queued_at": "2026-06-07T19:16:40Z",
      "updated_at": "2026-06-07T19:16:40Z",
      "failure_count": 0,
      "last_error": null
    }
  ]
}
```

### Route Decision Event

Recommended event type: `routing.decision.made`

```json
{
  "schema_version": "jmc.telemetry.v1",
  "event_type": "routing.decision.made",
  "source": "orchestrator",
  "payload": {
    "route": "pull_request_opened_circuit_hermes",
    "decision": "dispatch_to_hermes_m2",
    "eligible": true,
    "reasons": ["head_ref=agent-integration", "base_ref=main"],
    "blocked_reason": null,
    "target_worker_id": "hermes-m2"
  }
}
```

### Runtime Validation Event

Recommended event types:

- `runtime_validation.requested`
- `runtime_validation.started`
- `runtime_validation.completed`
- `runtime_validation.blocked`

```json
{
  "schema_version": "jmc.telemetry.v1",
  "event_type": "runtime_validation.completed",
  "source": "orchestrator",
  "payload": {
    "provider": "hermes",
    "hermes_node": "M2",
    "status": "PASSED",
    "job_id": "job-123",
    "target_url": "https://example.invalid/redacted-or-safe-target",
    "evidence": [
      { "type": "summary", "name": "summary.json", "url": null },
      { "type": "screenshot", "name": "screenshot.png", "url": null }
    ],
    "label": "agent-verified"
  }
}
```

### BB2 Review Event

Recommended event types:

- `bb2.review.requested`
- `bb2.review.completed`
- `bb2.review.failed`

```json
{
  "schema_version": "jmc.telemetry.v1",
  "event_type": "bb2.review.completed",
  "source": "orchestrator",
  "payload": {
    "decision": "approved_for_human_review",
    "risk_level": "low",
    "human_review_required": true,
    "required_changes_count": 0,
    "changed_files_count": 3,
    "github_writeback_attempted": true,
    "github_writeback_success": true,
    "label": "bb2-approved"
  }
}
```

## Backend Payload To JMC Component Mapping

| JMC component | Current source | Recommended canonical source |
| --- | --- | --- |
| Top health badge | `GET /health`, `GET /debug/health` | `GET /telemetry/health` |
| Webhook intake chart | `EventRecord`, `DebugHealth` | `telemetry events` filtered by `webhook.*` |
| Queue depth cards | `GET /debug/review-queue/stats` | `GET /telemetry/queue` counters |
| Oldest pending SLA card | `ReviewQueueStats.oldest_pending_age_seconds` | `GET /telemetry/queue.oldest_pending_age_seconds` |
| Worker list | `GET /debug/workers/stats` aggregate only | `GET /telemetry/workers` |
| Worker detail drawer | `ReviewWorkItem` plus logs | `GET /telemetry/workers/{worker_id}` and queue item links |
| Lifecycle timeline | `GET /debug/review-lifecycle` | `telemetry events` by `correlation_id` |
| Recent failures panel | `GET /debug/recent-failures` | `GET /telemetry/failures` plus `telemetry events` |
| Circuit assignment feed | Slack message + `TaskDispatchResult` | `task.dispatch.*` events |
| Hermes validation panel | `HermesDispatchResult`, GitHub comments/labels | `runtime_validation.*` events and artifact links |
| BB2 review panel | `ReviewProcessResponse`, GitHub writeback | `bb2.review.*` events |
| Route explanation panel | workflow trigger + Hermes route logs | `routing.decision.made` events |
| Repo/branch filter | `repo_full_name`, `branch`, `commit_sha` across models | telemetry envelope fields |

## Ownership Recommendation

### Orchestrator Should Own

- Worker queue truth.
- Review work item lifecycle.
- Worker claim and retry state.
- GitHub webhook intake telemetry.
- Routing decisions from GitHub events to review, task dispatch, Slack, and Hermes.
- BB2 review request/result telemetry when invoked by Orchestrator.
- GitHub writeback attempt/result telemetry.
- JMC operational telemetry endpoints and event stream.

### Hermes Should Own

- Runtime validation execution details.
- Job status, evidence artifacts, browser/runtime logs, screenshots, network traces, and validation result internals.
- Hermes node health and capacity.

The Orchestrator should normalize Hermes status and artifact references for JMC but should not duplicate Hermes job internals beyond summary metadata.

### Jarvis Brain Should Own

- Domain/product context and business reasoning.
- Human-readable explanations derived from operational events.
- Cross-system summaries and recommendations.
- Optional enrichment of JMC displays.

Jarvis Brain should not be required for JMC to know whether a worker is active, a queue is blocked, or a validation failed.

### JMC Should Own

- UI composition.
- Client-side filtering and presentation state.
- Visualization choices.
- User-facing language layered over stable backend telemetry contracts.

## Recommended Implementation Sequence

1. Add production-safe read-only telemetry endpoints under `/telemetry/*` that wrap existing debug models without exposing debug naming.
2. Add a canonical telemetry envelope builder and emit it from current lifecycle transitions and operational log points.
3. Add worker identity and heartbeat fields for Orchestrator review worker, Circuit assignment dispatcher, BB2 review path, Slack dispatcher, and Hermes dispatcher.
4. Add route decision records for review workflow, task dispatch, and Hermes dispatch.
5. Add artifact link placeholders to Hermes normalization so JMC can render evidence when Hermes provides URLs.
6. Add SSE or websocket streaming after the persisted event store can support replay cursors.
7. Add Brain enrichment as optional metadata after operational telemetry is stable.

## Risks And Constraints

- Current `/debug/*` endpoints are operationally useful but should not become the long-term public JMC contract by name.
- In-memory stores lose event history on restart; JMC should require SQLite or another persistent backing store for production telemetry.
- There is no websocket or streaming transport today.
- Worker identity is currently implicit. JMC cannot distinguish specific agent processes from aggregate review worker counts.
- Hermes DGX dispatch is explicitly blocked in current code until supported.
- Current artifact evidence is named but not linked; JMC cannot deep-link to evidence without an artifact URL contract.
- Debug read authentication is configurable and disabled by default; production telemetry should have an explicit auth model.

## Conclusion

The Orchestrator is the correct canonical source for JMC worker and automation telemetry because it owns the queue, workflow, lifecycle transitions, routing, writeback, Slack notifications, and Hermes dispatch decisions. JMC can use existing debug endpoints for an MVP, especially queue stats, worker stats, lifecycle visibility, and recent failures. Before JMC depends on this in production, the Orchestrator should add stable `/telemetry/*` snapshots and a canonical event stream using the recommended envelope, while Jarvis Brain contributes optional enrichment rather than core operational truth.

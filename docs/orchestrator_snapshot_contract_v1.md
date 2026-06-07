# Orchestrator Snapshot Contract v1

## Endpoint

`GET /api/v1/orchestrator/snapshot`

This endpoint is the canonical Orchestrator telemetry snapshot for Jarvis Mission Control (JMC). JMC should call this single endpoint for Orchestrator operational state instead of stitching together multiple debug endpoints.

When `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true`, this endpoint requires the same `X-Orchestrator-Admin-Token` header as the protected debug read endpoints.

## Schema Version

`orchestrator.snapshot.v1`

## Canonical Payload

```json
{
  "schema_version": "orchestrator.snapshot.v1",
  "generated_at": "2026-06-07T19:45:00.000000Z",
  "overview": {
    "status": "ok",
    "app_env": "local",
    "work_branch": "agent-integration",
    "base_branch": "main",
    "webhook_count": 1,
    "accepted_count": 1,
    "rejected_count": 0,
    "review_queue_count": 1,
    "pending_review_count": 1,
    "active_reviewing_count": 0,
    "approved_for_human_review_count": 0,
    "blocked_count": 0,
    "recent_failure_count": 0
  },
  "agents": [],
  "issues": [],
  "prs": [],
  "events": [],
  "queue": {},
  "health": {},
  "runtime": {
    "auto_processing_enabled": false,
    "github_context_hydration_enabled": false,
    "github_writeback_enabled": false,
    "task_dispatch_enabled": false,
    "debug_reads_require_admin_token": false,
    "hermes_dispatch": {
      "default_target_configured": false,
      "m2_dispatch_enabled": false,
      "m2_configured": false,
      "dgx_dispatch_enabled": false,
      "dgx_configured": false
    }
  },
  "recent_failures": []
}
```

## Field Definitions

| Field | Type | Definition |
| --- | --- | --- |
| `schema_version` | string | Stable contract identifier. Current value is `orchestrator.snapshot.v1`. |
| `generated_at` | datetime | UTC timestamp when the snapshot was assembled. |
| `overview` | object | Compact summary for top-level JMC status cards. |
| `agents` | array | Review lifecycle projection built from existing `ReviewLifecycleVisibility` records. |
| `issues` | array | Review work items attached to GitHub issues and not PRs. |
| `prs` | array | Review work items attached to GitHub pull requests. |
| `events` | array | Recent accepted webhook events from the existing `EventRecord` source. |
| `queue` | object | Existing `ReviewQueueStats` payload. |
| `health` | object | Existing `DebugHealth` payload. |
| `runtime` | object | Current Orchestrator runtime and dispatch configuration status. |
| `recent_failures` | array | Existing bounded `RecentFailure` projection. Additive helper field for JMC failure panels. |

## Overview Fields

| Field | Source | Definition |
| --- | --- | --- |
| `status` | Orchestrator service | Service status for JMC display. Currently `ok` when the app can assemble the snapshot. |
| `app_env` | `Settings.app_env` | Runtime environment label. |
| `work_branch` | `Settings.work_branch` | Circuit work branch, normally `agent-integration`. |
| `base_branch` | `Settings.base_branch` | Human review base branch, normally `main`. |
| `webhook_count` | `DebugHealth` | Accepted plus rejected webhook count. |
| `accepted_count` | `DebugHealth` | Accepted webhook/event count. |
| `rejected_count` | `DebugHealth` | Rejected webhook count. |
| `review_queue_count` | `DebugHealth` | Total review queue item count. |
| `pending_review_count` | `DebugHealth` | Items awaiting review. |
| `active_reviewing_count` | `WorkerStats` | Items currently in review. |
| `approved_for_human_review_count` | `DebugHealth` | Items approved for human review. |
| `blocked_count` | `DebugHealth` | Blocked review items. |
| `recent_failure_count` | `ReviewQueueStats` | Count of queue items with recent failures. |

## Embedded Existing Models

The snapshot intentionally reuses existing Orchestrator telemetry sources rather than duplicating business logic:

- `WorkerStats` contributes worker activity fields to `overview` and `runtime` interpretation.
- `ReviewQueueStats` is embedded at `queue`.
- `ReviewWorkItem` is embedded in `issues` and `prs`.
- `EventRecord` is embedded in `events`.
- `RecentFailure` is embedded in `recent_failures`.
- `ReviewLifecycleVisibility` is embedded in `agents`.
- Hermes dispatch configuration is normalized into `runtime.hermes_dispatch` as `HermesDispatchStatus`.

## Update Cadence

The endpoint returns an on-demand snapshot. It does not cache by default.

Recommended JMC polling cadence:

- Normal dashboard view: every 15-30 seconds.
- Active incident or review queue view: every 5-10 seconds.
- Background tab or minimized view: every 60 seconds or pause polling.

If future load requires caching, cache the fully assembled snapshot for a short TTL, such as 5 seconds. Do not cache individual debug endpoint responses separately for JMC, because this contract is the frontend integration boundary.

## Ownership Rules

Backend owns aggregation. The Orchestrator service is the source of truth for queue, worker, lifecycle, webhook, writeback, dispatch, and Hermes routing telemetry.

Frontend owns rendering. JMC should render this canonical payload and should not be required to call multiple Orchestrator workforce endpoints to build a page.

Jarvis Brain may enrich JMC with product or reasoning context, but Brain is not required for basic Orchestrator operational truth.

## Future Extension Strategy

- Keep `schema_version` stable for additive fields that do not change existing field meaning.
- Add new optional fields rather than changing existing field types.
- Use nested objects for new domains, for example `runtime.hermes_dispatch` or a future `runtime.openai_review`.
- Introduce `orchestrator.snapshot.v2` only for breaking changes.
- Prefer reusing existing internal models and builders before adding new snapshot-only business logic.
- Keep frontend compatibility by treating unknown fields as safe to ignore.

## Verification Notes

The contract is covered by endpoint tests that verify:

- `GET /api/v1/orchestrator/snapshot` returns `schema_version: orchestrator.snapshot.v1`.
- Existing webhook, event, queue, lifecycle, health, and runtime data are aggregated into one payload.
- The endpoint follows the debug-read access policy when token protection is enabled.

# Orchestrator Snapshot Contract v2

## Endpoint

`GET /api/v1/orchestrator/snapshot`

This endpoint is the canonical Orchestrator telemetry snapshot for Jarvis Mission Control (JMC). JMC should call this single endpoint for Orchestrator operational state instead of stitching together multiple debug endpoints.

When `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true`, this endpoint requires the same `X-Orchestrator-Admin-Token` header as the protected debug read endpoints.

## Schema Version

`orchestrator.snapshot.v2`

## Canonical Payload

```json
{
  "schema_version": "orchestrator.snapshot.v2",
  "generated_at": "2026-06-07T19:45:00.000000Z",
  "workforce": {
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
    "meta": {
      "agents": { "returned": 0, "total": 0, "limit": 50, "truncated": false },
      "issues": { "returned": 0, "total": 0, "limit": 50, "truncated": false },
      "prs": { "returned": 0, "total": 0, "limit": 50, "truncated": false },
      "events": { "returned": 0, "total": 0, "limit": 25, "truncated": false }
    },
    "agents": [],
    "issues": [],
    "prs": [],
    "events": []
  },
  "workflows": {
    "active": 1,
    "blocked": 0,
    "reviewing": 0,
    "verified": 0
  },
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
| `schema_version` | string | Stable contract identifier. Current value is `orchestrator.snapshot.v2`. |
| `generated_at` | datetime | UTC timestamp when the snapshot was assembled. |
| `workforce` | object | JMC Workforce section payload. Contains `overview`, `agents`, `issues`, `prs`, and `events`. |
| `workflows` | object | Canonical workflow summary counts. Detailed workflow records are owned by the workflow endpoints. |
| `queue` | object | Existing `ReviewQueueStats` payload for operational queue counters and ages. |
| `health` | object | Existing `DebugHealth` payload for service and webhook counters. |
| `runtime` | object | Current Orchestrator runtime and dispatch configuration status. Secret values are never included. |
| `recent_failures` | array | Existing bounded `RecentFailure` projection. Additive helper field for JMC failure panels. |

## Workforce Fields

| Field | Type | Definition |
| --- | --- | --- |
| `workforce.overview` | object | Compact summary for top-level JMC Workforce status cards. |
| `workforce.meta` | object | Collection count, limit, and truncation metadata for each bounded workforce list. |
| `workforce.agents` | array | Compact review lifecycle projection built from existing `ReviewLifecycleVisibility` records. |
| `workforce.issues` | array | Compact review work item projection for GitHub issue work items and not PRs. |
| `workforce.prs` | array | Compact review work item projection for GitHub pull request work items. |
| `workforce.events` | array | Compact recent accepted webhook event projection from the existing `EventRecord` source. |

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

## Payload Bounds

The snapshot is a dashboard contract, not an archival export. It is intentionally bounded so Mission Control can poll it safely:

- `workforce.agents`, `workforce.issues`, and `workforce.prs` return at most 50 records each.
- `workforce.events` returns at most 25 records.
- Workforce records preserve compact workflow summary fields, including `workflow_id`, `workflow_state`, `canonical_workflow_state`, `current_owner`, `workflow_event_count`, and `workflow_events_truncated`.
- Workforce records intentionally do not include embedded `workflow_events` or `workflow_state_history`; use `/api/v1/workflows/{workflow_id}` or `/api/v1/workflows/{workflow_id}/timeline` for lifecycle detail.
- `labels` returns at most 20 labels per work item while preserving `label_count` and `labels_truncated`.
- `recent_failures` returns at most 20 records.
- Error strings in workforce records and `recent_failures` are capped at 2048 characters and expose a matching `*_truncated` boolean.
- Full `runtime_validation_context` payloads are not included in the snapshot. Runtime status remains available through bounded fields such as `runtime_validation_id`, `runtime_validation_status`, `runtime_validation_digest`, and `runtime_validation_completed_at`.

Collection metadata uses this shape:

```json
{
  "returned": 50,
  "total": 73,
  "limit": 50,
  "truncated": true
}
```

## Embedded Existing Models

The snapshot intentionally reuses existing Orchestrator telemetry sources and canonical workflow builders rather than duplicating business logic:

- `WorkerStats` contributes worker activity fields to `workforce.overview` and runtime interpretation.
- `ReviewQueueStats` is embedded at `queue`.
- `ReviewWorkItem` contributes compact fields to `workforce.issues` and `workforce.prs`.
- `EventRecord` contributes compact fields to `workforce.events`.
- `RecentFailure` contributes bounded fields to `recent_failures`.
- `ReviewLifecycleVisibility` contributes compact fields to `workforce.agents`.
- Hermes dispatch configuration is normalized into `runtime.hermes_dispatch` as `HermesDispatchStatus`.

Canonical ownership remains in the workflow lifecycle layer. Snapshot records expose compact workflow summary fields derived from those canonical builders. Workflow lists should use `/api/v1/workflows`; detail views and full timelines should use `/api/v1/workflows/{workflow_id}` and `/api/v1/workflows/{workflow_id}/timeline`.

## Access And Security

The snapshot exposes operational queue, event, health, and runtime configuration status. It must follow the debug-read access policy:

- Local/default mode: readable without an admin token when `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=false`.
- Protected mode: requires `X-Orchestrator-Admin-Token` when `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true`.
- Runtime configuration fields expose configured/enabled booleans only. They must not expose webhook URLs, bot tokens, Hermes tokens, GitHub tokens, admin tokens, or default target strings.

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

- This compact bounded contract is published as `orchestrator.snapshot.v2` because it changes existing list semantics and omits full `runtime_validation_context` payloads from workforce records.
- Keep `schema_version` stable for additive fields that do not change existing field meaning.
- Add new optional fields rather than changing existing field types.
- Use nested objects for new domains, for example `runtime.hermes_dispatch` or a future `runtime.openai_review`.
- Introduce `orchestrator.snapshot.v3` only for breaking changes after v2.
- Prefer reusing existing internal models and builders before adding new snapshot-only business logic.
- Keep frontend compatibility by treating unknown fields as safe to ignore.

## Verification Notes

The contract is covered by endpoint tests that verify:

- `GET /api/v1/orchestrator/snapshot` returns `schema_version: orchestrator.snapshot.v2`.
- Existing webhook, event, queue, lifecycle, health, and runtime data are aggregated into one payload with JMC Workforce data under `workforce`.
- Workforce list payloads are bounded and omit full runtime validation context payloads.
- The endpoint follows the debug-read access policy when token protection is enabled.
- Runtime status does not expose configured secret values.

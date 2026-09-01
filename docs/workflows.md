# Canonical Workflow Contract

The orchestrator owns canonical workflow lifecycle state for Jarvis Mission Control.
JMC should render workflow cards, workflow detail views, timelines, state badges,
assigned agents, Hermes status, BB2 status, and routing history from these read-only
workflow payloads instead of reconstructing lifecycle state from GitHub labels.

Existing snapshot-facing lifecycle summary fields keep their legacy values, and
canonical workflow state is layered on top through explicit canonical fields and
the workflow API resources. Timeline detail is exposed only by workflow detail
and timeline resources.

## States

`WorkflowState` uses these canonical values:

- `CREATED`
- `ASSIGNED`
- `CIRCUIT_WORKING`
- `PR_OPENED`
- `HERMES_VALIDATING`
- `HERMES_FAILED`
- `BB2_REVIEWING`
- `CHANGES_REQUESTED`
- `APPROVED`
- `COMPLETED`
- `MERGED`
- `CLOSED_UNMERGED`
- `ABANDONED`
- `DEPLOYED`
- `VERIFIED`
- `BLOCKED`

## Legacy Snapshot Compatibility

Snapshot consumers that already read `workflow_state` continue receiving the
legacy lifecycle values, including:

- `ISSUE_CREATED`
- `AGENT_READY`
- `CIRCUIT_IN_PROGRESS`
- `BB2_REVIEW_REQUESTED`
- `READY_TO_MERGE`

New consumers should migrate to:

- workflow API `current_state`
- snapshot `canonical_workflow_state`
- event `canonical_state`
- event `new_state`

The legacy values remain available so existing dashboard and automation consumers do
not need to migrate in lockstep with the canonical API rollout.

## Transition Rules

Canonical workflow state is derived deterministically from durable orchestrator inputs.

| Input | Canonical state |
| --- | --- |
| Issue opened | `CREATED` |
| Issue labeled, edited, or reopened | `ASSIGNED` |
| Push event | `CIRCUIT_WORKING` |
| Pull request opened, reopened, or synchronized | `PR_OPENED` |
| AgentTask queued or assigned | `ASSIGNED` |
| AgentTask claimed, running, or in progress | `CIRCUIT_WORKING` |
| AgentTask ready for review | `BB2_REVIEWING` |
| AgentTask completed | `COMPLETED` |
| AgentTask failed or cancelled | `BLOCKED` |
| Worker claimed or review started | `HERMES_VALIDATING` |
| OpenAI/BB2 review attempted or GitHub writeback started | `BB2_REVIEWING` |
| OpenAI review failed or review failed | `HERMES_FAILED` |
| Review decision is needs changes | `CHANGES_REQUESTED` |
| Review decision is approved for human review | `APPROVED` |
| Pull request closed with `merged: true` | `MERGED` |
| Pull request closed without `merged: true` | `CLOSED_UNMERGED` |
| Human deployment evidence recorded | `DEPLOYED` |
| Human verification evidence recorded | `VERIFIED` |
| Review item is blocked | `BLOCKED` |

`APPROVED` is active and awaiting human merge. It is not terminal because only Marcus may
merge and deploy. `COMPLETED`, `MERGED`, `CLOSED_UNMERGED`, `ABANDONED`, `DEPLOYED`,
and `VERIFIED` are terminal workflow states for summary-count purposes.

`HERMES_FAILED` is entered when the automated validation/review path records an OpenAI
review failure or review failure lifecycle timestamp. It is counted as blocked evidence
until a later event or work item status moves the workflow forward.

`VERIFIED` is reserved for explicit human verification evidence after deployment or
acceptance. The current implementation keeps the state in the canonical enum and
summary contract, but does not infer verification from approval or merge events.

## Endpoints

### `GET /api/v1/workflows`

Returns a bounded page of normalized workflow summary records. By default, the endpoint
returns active workflows plus terminal workflows with activity in the last 14 days,
sorted by most recent activity with a stable `workflow_id` tie-breaker.

Query parameters:

| Parameter | Default | Notes |
| --- | --- | --- |
| `limit` | `50` | Page size. Must be between `1` and `100`. |
| `offset` | `0` | Zero-based offset into the filtered workflow list. Must be between `0` and `1000` so storage-backed polling remains query-bounded. |
| `filter` | `active_recent` | One of `active_recent`, `active`, `recent`, or `all`. |
| `recent_days` | `14` | Recent activity window. Must be between `1` and `90`. |

The `workflows` array is intentionally compact for polling clients. It omits
`timeline` and `route_history`; fetch `GET /api/v1/workflows/{workflow_id}` or
`GET /api/v1/workflows/{workflow_id}/timeline` when a view needs full detail.
Correlated GitHub event records are collapsed into one event-backed workflow;
event-backed workflows are de-duplicated against review work items by issue/PR
subject when present, by branch and commit for ref-only events, or by fallback
workflow record ID when neither subject nor ref is available. `pagination.total`
and `pagination.unfiltered_total` count normalized workflows, not raw event rows.
Correlated event workflow summaries preserve the first event as `created_at`
and the latest event as `updated_at`/`last_activity_at`; full event sequences
remain detail-only.
When SQLite storage is active, AgentTask workflows are merged into the list only
from a matching SQLite AgentTask store. In-memory AgentTask stores are ignored
for storage-backed polling so global process state cannot inflate or leak into
the persisted workflow collection.
Storage-backed list assembly fetches at most `offset + limit` compact summary
rows from each workflow source, computes totals with count queries, merges the
candidate summaries by activity, then slices the requested page. This keeps the
polling payload and query result sets bounded while preserving cross-source
ordering. Full workflow and timeline hydration is reserved for
`GET /api/v1/workflows/{workflow_id}` and
`GET /api/v1/workflows/{workflow_id}/timeline`.
`pagination.truncated` means more filtered workflows exist beyond the returned
page. `pagination.has_next` is true only when `next_offset` is within the
bounded offset window.
`pagination` is additive metadata:

```json
{
  "workflows": [
    {
      "workflow_id": "wf-...",
      "correlation_id": "orch-...",
      "repo_full_name": "owner/repo",
      "issue_number": 42,
      "pr_number": 17,
      "current_state": "BB2_REVIEWING",
      "assigned_agent": "circuit-forge",
      "hermes_job_id": null,
      "last_actor": "BB2",
      "created_at": "...",
      "updated_at": "...",
      "last_activity_at": "..."
    }
  ],
  "pagination": {
    "limit": 50,
    "offset": 0,
    "returned": 1,
    "total": 1,
    "unfiltered_total": 1,
    "truncated": false,
    "has_next": false,
    "next_offset": null,
    "filter": "active_recent",
    "recent_days": 14
  }
}
```

### `GET /api/v1/workflows/{workflow_id}`

Returns one full workflow record, including `timeline` and `route_history`, or
`404` when the workflow is unknown.

### `GET /api/v1/workflows/{workflow_id}/timeline`

Returns canonical lifecycle events for one workflow while preserving legacy event state:

```json
{
  "workflow_id": "wf-...",
  "events": [
    {
      "event_type": "workflow.lifecycle.changed",
      "state": "PR_OPENED",
      "canonical_state": "HERMES_VALIDATING",
      "previous_state": "PR_OPENED",
      "new_state": "HERMES_VALIDATING",
      "actor": "Hermes",
      "timestamp": "..."
    }
  ]
}
```

## Snapshot Integration

`GET /api/v1/orchestrator/snapshot` schema `orchestrator.snapshot.v3` includes workflow summary counts:

```json
{
  "workflows": {
    "active": 12,
    "blocked": 1,
    "reviewing": 3,
    "verified": 7
  }
}
```

Workforce entries include both legacy and canonical values:

```json
{
  "workflow_id": "wf-...",
  "workflow_state": "CIRCUIT_IN_PROGRESS",
  "canonical_workflow_state": "CIRCUIT_WORKING",
  "workflow_event_count": 1,
  "workflow_events_truncated": true
}
```

Snapshot v3 workforce entries omit `workflow_events` and `workflow_state_history`.
These fields are dashboard summaries. Detailed UI surfaces should use the
workflow endpoints above.

Snapshot `workflows` counts use the same normalized summary semantics as the
workflow list: review work items, AgentTask workflows, and de-duplicated
event-backed workflows are counted without embedding timeline detail.

# Canonical Workflow Contract

The orchestrator owns canonical workflow lifecycle state for Jarvis Mission Control.
JMC should render workflow cards, workflow detail views, timelines, state badges,
assigned agents, Hermes status, BB2 status, and routing history from these read-only
workflow payloads instead of reconstructing lifecycle state from GitHub labels.

This contract is additive. Existing snapshot-facing lifecycle fields keep their
legacy values, and canonical workflow state is layered on top through explicit
canonical fields and the workflow API resources.

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
- `MERGED`
- `CLOSED_UNMERGED`
- `ABANDONED`
- `DEPLOYED`
- `VERIFIED`
- `BLOCKED`

## Legacy Snapshot Compatibility

Snapshot consumers that already read `workflow_state`, `workflow_state_history[].state`,
or `workflow_events[].state` continue receiving the legacy lifecycle values, including:

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
merge and deploy. `MERGED`, `CLOSED_UNMERGED`, `ABANDONED`, `DEPLOYED`, and `VERIFIED`
are terminal workflow states for summary-count purposes.

`HERMES_FAILED` is entered when the automated validation/review path records an OpenAI
review failure or review failure lifecycle timestamp. It is counted as blocked evidence
until a later event or work item status moves the workflow forward.

`VERIFIED` is reserved for explicit human verification evidence after deployment or
acceptance. The current implementation keeps the state in the canonical enum and
summary contract, but does not infer verification from approval or merge events.

## Endpoints

### `GET /api/v1/workflows`

Returns normalized workflow records:

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
      "last_activity_at": "...",
      "timeline": [],
      "route_history": []
    }
  ]
}
```

### `GET /api/v1/workflows/{workflow_id}`

Returns one workflow record or `404` when the workflow is unknown.

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

`GET /api/v1/orchestrator/snapshot` includes workflow summary counts:

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
  "workflow_state": "CIRCUIT_IN_PROGRESS",
  "canonical_workflow_state": "CIRCUIT_WORKING"
}
```

These counts are dashboard summaries. Detailed UI surfaces should use the workflow
endpoints above.

# Canonical Workflow Contract

The orchestrator owns canonical workflow lifecycle state for Jarvis Mission Control.
JMC should render workflow cards, workflow detail views, timelines, state badges,
assigned agents, Hermes status, BB2 status, and routing history from these read-only
workflow payloads instead of reconstructing lifecycle state from GitHub labels.

## States

`WorkflowState` is additive and uses these canonical values:

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
- `DEPLOYED`
- `VERIFIED`
- `BLOCKED`

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
      "last_actor": "bb2",
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

Returns canonical lifecycle events for one workflow:

```json
{
  "workflow_id": "wf-...",
  "events": [
    {
      "event_type": "workflow.lifecycle.changed",
      "previous_state": "PR_OPENED",
      "new_state": "HERMES_VALIDATING",
      "actor": "hermes",
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

These counts are dashboard summaries. Detailed UI surfaces should use the workflow
endpoints above.

# Agent Task Submission API

Date: 2026-06-17

## Architecture Summary

RiseOS Orchestrator now has a first-class direct API path for coding task intake:

```text
POST /api/v1/agent-tasks
-> AgentTask
-> queued lifecycle event
-> workflow visibility
```

This is intentionally limited to canonical task creation and queue visibility. It does not execute workers, invoke Codex CLI, create branches, perform Git operations, open PRs, merge, or complete work.

The long-term convergence target is:

```text
GitHub Issue Path:
GitHub Issue -> Orchestrator -> AgentTask

Direct API Path:
POST /api/v1/agent-tasks -> AgentTask
```

This change implements the direct API path and the canonical model needed for both paths to converge later.

## New Schemas

### AgentTaskCreateRequest

```json
{
  "repo_full_name": "owner/repo",
  "title": "task title",
  "objective": "high level objective",
  "instructions": ["instruction 1", "instruction 2"],
  "acceptance_criteria": ["criteria 1", "criteria 2"],
  "target_agent": "codex-m2",
  "priority": "normal",
  "correlation_id": "optional"
}
```

Supported priority values:

- `low`
- `normal`
- `high`
- `urgent`

### AgentTaskCreateResponse

```json
{
  "task_id": "agtask-...",
  "status": "queued",
  "created_at": "2026-06-17T00:00:00Z",
  "target_agent": "codex-m2"
}
```

### AgentTask

Canonical task state includes:

- `task_id`
- `repo_full_name`
- `title`
- `objective`
- `instructions`
- `acceptance_criteria`
- `target_agent`
- `priority`
- `correlation_id`
- `status`
- `source`
- `issue_number`
- `created_at`
- `updated_at`
- `queued_at`
- `lifecycle_events`

New direct API tasks use `source=direct_api`, start at `status=queued`, and receive lifecycle events:

- `created`
- `queued`

## New Storage Model

Agent tasks are persisted by `app.agent_tasks`.

When `ORCHESTRATOR_DB_PATH` is configured, the API stores tasks in SQLite table `agent_tasks` inside the same orchestrator database file. When SQLite is unavailable or no path is configured, the API falls back to an in-memory store for local/dev behavior.

SQLite table:

```sql
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id TEXT PRIMARY KEY,
    repo_full_name TEXT NOT NULL,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    instructions TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    target_agent TEXT NOT NULL,
    priority TEXT NOT NULL,
    correlation_id TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    issue_number INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    queued_at TEXT,
    lifecycle_events TEXT NOT NULL
)
```

`instructions`, `acceptance_criteria`, and `lifecycle_events` are JSON-encoded arrays.

## Routes

### POST /api/v1/agent-tasks

Creates and queues a canonical AgentTask.

Validation:

- `repo_full_name` must exist in the repository registry.
- Repository must have `orchestration_enabled=true`.
- Repository must not be archived.

The endpoint returns `403` when the repository is not enabled for orchestration.

### GET /api/v1/agent-tasks

Returns all canonical AgentTask records, newest first.

### GET /api/v1/agent-tasks/{task_id}

Returns one canonical AgentTask record or `404` if it does not exist.

## Workflow Visibility

Direct API tasks are included in:

- `GET /api/v1/workflows`
- `GET /api/v1/workflows/{workflow_id}`
- `GET /api/v1/workflows/{workflow_id}/timeline`

Workflow IDs for direct tasks use:

```text
wf-agent-task-{task_id}
```

A queued direct task maps to canonical workflow state `ASSIGNED`, with `assigned_agent` set to the request `target_agent`.

## Tests

`tests/test_agent_tasks.py` covers:

- POST creates a queued task and lifecycle events.
- SQLite persistence and reload.
- GET returns canonical task state.
- Disabled/unregistered repositories are rejected.
- AgentTasks are discoverable through workflow APIs and timeline APIs.

## Migration Notes

No destructive migration is required.

The SQLite store creates `agent_tasks` with `CREATE TABLE IF NOT EXISTS`, so existing orchestrator databases can be reused. The existing review queue and webhook event tables are unchanged.

For deployment:

1. Keep `ORCHESTRATOR_DB_PATH` configured so AgentTasks survive restarts.
2. Ensure repository discovery has populated the repository registry before external systems submit tasks.
3. Submit tasks only for repositories with `orchestration_enabled=true`.
4. Treat this API as queue/intake only until the worker execution path is explicitly implemented.

## Example

```bash
curl -sS -X POST "$ORCHESTRATOR_BASE_URL/api/v1/agent-tasks" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_full_name": "marcus937/riseos-agent-orchestrator",
    "title": "Add a small orchestrator improvement",
    "objective": "Implement the requested change without executing worker code.",
    "instructions": ["Keep the change minimal", "Add tests"],
    "acceptance_criteria": ["Tests pass", "Workflow API shows the task"],
    "target_agent": "codex-m2",
    "priority": "normal",
    "correlation_id": "external-system-123"
  }'
```

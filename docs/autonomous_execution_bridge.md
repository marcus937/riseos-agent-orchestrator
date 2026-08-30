# Autonomous Execution Bridge

Date: 2026-06-17

## Current Flow

The Orchestrator now accepts direct AgentTask submissions:

```text
POST /api/v1/agent-tasks
-> AgentTask created
-> AgentTask persisted
-> lifecycle events: created, queued
-> workflow visibility
-> assigned_agent set from target_agent
```

Before this bridge, the Direct API path stopped at Orchestrator state. It did not hand execution work to Agent Bus and had no callback contract for worker results.

## Target Flow

This bridge connects Orchestrator AgentTasks to the existing Agent Bus work queue:

```text
POST /api/v1/agent-tasks
-> AgentTask created
-> AgentTask queued
-> Agent Bus WorkItem created
-> AgentTask assigned
-> codex-m2 consumes Agent Bus inbox/queue
-> codex-m2 claims/runs the WorkItem through Agent Bus
-> codex-m2 posts execution result to Orchestrator
-> AgentTask stores execution evidence
-> workflow state becomes COMPLETED or BLOCKED
```

This PR intentionally does not implement Codex execution, Git operations, branch creation, PR creation, merges, or a second worker queue.

## Agent Bus Integration

Agent Bus remains the only worker queue. Orchestrator creates one Agent Bus WorkItem when an AgentTask is submitted and Agent Bus dispatch is enabled.

Required Orchestrator configuration:

```env
ENABLE_AGENT_BUS_DISPATCH=true
AGENT_BUS_BASE_URL=https://agent-bus.riseconnect.us
AGENT_BUS_TOKEN=<existing-agent-bus-token-if-required-by-deployment>
AGENT_BUS_TIMEOUT_SECONDS=30
AGENT_BUS_REVIEW_AGENT=bb2
```

WorkItem creation uses:

```text
POST {AGENT_BUS_BASE_URL}/work-items
```

The WorkItem contains these bridge fields in `metadata`:

```json
{
  "task_id": "agtask-...",
  "workflow_id": "wf-agent-task-agtask-...",
  "repo_full_name": "owner/repo",
  "objective": "high level objective",
  "instructions": ["instruction 1"],
  "acceptance_criteria": ["criteria 1"],
  "target_agent": "codex-m2",
  "source": "riseos-agent-orchestrator.agent_task",
  "callback": {
    "method": "POST",
    "path": "/api/v1/agent-tasks/{task_id}/execution-result"
  }
}
```

Top-level WorkItem fields include:

- `title`
- `repository`
- `issue_number`
- `priority`
- `owner_agent`
- `review_agent`

The Orchestrator stores the returned `work_item_id` as `agent_bus_work_item_id` and moves the AgentTask to `assigned`.

## Worker Poll API

Agent Bus already exposes the worker intake lifecycle. Workers should use these existing endpoints and must not poll Orchestrator for work:

```text
GET /agents/{agent_id}/inbox
GET /agents/{agent_id}/queue
GET /work-items/{work_item_id}
POST /work-items/{work_item_id}/claim
POST /work-items/{work_item_id}/transition
POST /evidence-packets
POST /work-items/{work_item_id}/evidence
```

Minimum `codex-m2` worker lifecycle:

1. Register/heartbeat with Agent Bus.
2. Poll `GET /agents/codex-m2/inbox` or `GET /agents/codex-m2/queue`.
3. Claim the assigned WorkItem with `POST /work-items/{work_item_id}/claim`.
4. Transition to `in_progress` with `POST /work-items/{work_item_id}/transition`.
5. Execute Codex outside this PR's scope.
6. Create and attach Agent Bus evidence if available.
7. POST the Orchestrator execution callback.

## Worker Lifecycle

Canonical AgentTask states now support:

- `queued`
- `assigned`
- `claimed`
- `running`
- `completed`
- `failed`
- `cancelled`

Compatibility states still accepted by the model:

- `in_progress`
- `ready_for_review`

Workflow mapping:

| AgentTask status | Workflow state |
| --- | --- |
| `queued` | `ASSIGNED` |
| `assigned` | `ASSIGNED` |
| `claimed` | `CIRCUIT_WORKING` |
| `running` | `CIRCUIT_WORKING` |
| `completed` | `COMPLETED` |
| `failed` | `BLOCKED` |
| `cancelled` | `BLOCKED` |

## Execution Callback Contract

Endpoint:

```text
POST /api/v1/agent-tasks/{task_id}/execution-result
```

Payload:

```json
{
  "agent_id": "codex-m2",
  "status": "completed",
  "commit_sha": "abc123",
  "branch": "agent-integration",
  "changed_files": ["app/example.py"],
  "evidence": {
    "tests": "not_run",
    "summary": "manual simulation"
  }
}
```

Behavior:

- Verifies `agent_id` matches the task `target_agent`.
- Updates AgentTask status.
- Stores `commit_sha`, `branch`, `changed_files`, and `execution_evidence`.
- Appends a lifecycle event named after the submitted status.
- Makes `/api/v1/workflows/{workflow_id}` reflect the new workflow state.

For a successful manual simulation, `status=completed` moves the workflow to `COMPLETED`.

## Validation Goal

After this PR merges, validation should be possible without Codex execution:

1. Create an AgentTask:

```bash
curl -sS -X POST "$ORCHESTRATOR_BASE_URL/api/v1/agent-tasks" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_full_name": "marcus937/riseos-agent-orchestrator",
    "title": "Manual bridge validation",
    "objective": "Prove AgentTask dispatches to Agent Bus and accepts worker completion.",
    "instructions": ["Do not run Codex yet"],
    "acceptance_criteria": ["Agent Bus WorkItem exists", "Workflow becomes COMPLETED after callback"],
    "target_agent": "codex-m2",
    "priority": "normal",
    "correlation_id": "manual-bridge-validation"
  }'
```

2. Observe the Agent Bus WorkItem:

```bash
curl -sS "$AGENT_BUS_BASE_URL/work-items?owner_agent=codex-m2&status=queued"
```

3. Simulate `codex-m2` completion:

```bash
curl -sS -X POST "$ORCHESTRATOR_BASE_URL/api/v1/agent-tasks/$TASK_ID/execution-result" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "codex-m2",
    "status": "completed",
    "commit_sha": "manual-simulation",
    "branch": "agent-integration",
    "changed_files": [],
    "evidence": {"simulation": true}
  }'
```

4. Observe workflow transition:

```bash
curl -sS "$ORCHESTRATOR_BASE_URL/api/v1/workflows/wf-agent-task-$TASK_ID"
```

Expected workflow state:

```json
{
  "current_state": "COMPLETED",
  "assigned_agent": "codex-m2"
}
```

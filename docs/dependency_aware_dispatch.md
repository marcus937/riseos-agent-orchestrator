# Dependency-Aware Agent Dispatch

The orchestrator treats dependency metadata as scheduler input. Slack is informational only; it must not determine execution order.

## Layers

- Orchestrator: scheduler and dependency gate.
- Agent Bus: execution layer for Codex-M2, Circuit, Hermes, and future agents.
- Slack: optional notification layer after scheduler decisions.

## Public AgentTask API

`POST /api/v1/agent-tasks` accepts task-id dependencies with `dependency_task_ids`:

```json
{
  "repo_full_name": "marcus937/jarvis-codex-worker",
  "title": "Task B",
  "body": "Runs after Task A",
  "dependency_task_ids": ["agtask-123"]
}
```

Task responses expose dependency state:

```json
{
  "task_id": "agtask-456",
  "dependency_task_ids": ["agtask-123"],
  "blocked": true,
  "blocked_by": ["agtask-123"]
}
```

Invalid dependency task IDs are rejected with `422` and a `dependency_task_ids` detail payload.

## Supported Issue Metadata

Issue bodies and direct AgentTask objectives may also declare GitHub issue predecessors with either format:

```yaml
depends_on:
  - issue:72
  - issue:91
```

```yaml
predecessor_issue: 72
```

When both formats are present, `depends_on` takes precedence.

## Eligibility

A task with no dependencies is eligible immediately.

A task with task-id dependencies is eligible only when every dependency task has `status == completed`. Created, queued, assigned, running, failed, cancelled, and any other non-completed states keep the dependent task blocked.

A task with issue metadata dependencies is eligible only when every predecessor is complete. Missing predecessors, malformed dependency metadata, incomplete predecessors, and dependency cycles keep the task queued.

An issue predecessor is complete when either condition is true:

- The predecessor issue has both `bb2-approved` and `ready-to-merge`.
- A linked PR exposed to the scheduler has `ready-to-merge`.

## Dispatch Points

Dependency checks happen before execution dispatch:

- GitHub issue queue selection filters `agent-ready` issues before applying `agent-next` or creating Agent Bus work items.
- Direct AgentTask dispatch evaluates dependencies before calling Agent Bus `create_work_item`.

Dependency-blocked direct AgentTasks remain queued. They are not marked failed unless Agent Bus itself fails after dependency clearance.

## Sequential Chains

For a chain like #72 -> #73 -> #74 -> #75 -> #76, only #72 is initially eligible. Each dependent issue becomes eligible only after its predecessor reaches the existing BB2/ready-to-merge completion state.

For AgentTasks, Task B with `dependency_task_ids: [Task A]` remains blocked until Task A reaches `completed`. Multiple dependencies require all predecessor tasks to be completed.

## Example Curl

```bash
curl -X POST https://orchestrator.riseconnect.us/api/v1/agent-tasks \
  -H "Content-Type: application/json" \
  -d '{
    "repo_full_name":"marcus937/jarvis-codex-worker",
    "title":"Task B",
    "body":"Runs after Task A",
    "dependency_task_ids":["agtask-123"]
  }'
```

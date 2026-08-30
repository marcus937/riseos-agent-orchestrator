# Agent Bus Dispatch Flow

## Overview

Orchestrator now keeps the existing Slack and GitHub notification path, and adds Agent Bus WorkItem creation when a task is assigned.

```text
GitHub Issue
  labels: agent-task, agent-ready
        |
        v
RiseOS Orchestrator
  dispatch_next_agent_task()
  - selects oldest unclaimed ready issue
  - builds Agent Bus WorkItem payload
  - POST /work-items
  - stores returned work_item_id on workflow state
  - records Agent Bus lifecycle timestamps
        |
        +--> GitHub
        |    - applies agent-next
        |    - posts Circuit Assignment comment
        |
        v
Agent Bus
  WorkItem
  - owner_agent: codex-m2
  - review_agent: bb2
  - metadata objective, branch, issue URL
        |
        v
Codex Worker
  - polls inbox or queue
  - claims WorkItem
  - executes task
  - reports completion or failure
```

## Integration Points

- `app/clients/agent_bus.py`: HTTP client for `POST /work-items`.
- `app/task_dispatch.py`: builds the WorkItem payload during task assignment.
- `app/main.py`: wires Agent Bus dispatch into the existing approved-review GitHub writeback path.
- `app/review_queue.py`: stores Agent Bus dispatch result fields and lifecycle timestamps.
- `app/storage.py`: persists Agent Bus dispatch fields in SQLite.

## Required Settings

- `ENABLE_TASK_DISPATCH=true`
- `ENABLE_AGENT_BUS_DISPATCH=true`
- `AGENT_BUS_BASE_URL=<agent bus base URL>`
- `AGENT_BUS_TOKEN=<optional bearer token>`
- `AGENT_BUS_OWNER_AGENT=codex-m2`
- `AGENT_BUS_REVIEW_AGENT=bb2`
- `WORK_BRANCH=agent-integration`

## WorkItem Payload

```json
{
  "title": "<GitHub issue title>",
  "repository": "<owner/repo>",
  "issue_number": 123,
  "priority": "normal",
  "owner_agent": "codex-m2",
  "review_agent": "bb2",
  "metadata": {
    "objective": "<GitHub issue body>",
    "branch": "agent-integration",
    "issue_url": "https://github.com/<owner>/<repo>/issues/123",
    "source": "riseos-agent-orchestrator",
    "dispatch_label": "agent-next",
    "labels": ["agent-ready", "agent-task"],
    "routing": {
      "owner_agent": "codex-m2",
      "owner_capabilities": ["coding", "github", "testing"],
      "owner_agent_type": "implementation",
      "review_agent": "bb2",
      "reviewer_capabilities": ["pr_review"],
      "reviewer_agent_type": "review"
    }
  }
}
```

## Lifecycle Recording

When Agent Bus dispatch is enabled, Orchestrator records:

- `agent_bus_dispatch_started_at`
- `agent_bus_dispatch_completed_at`
- `agent_bus_dispatch_success`
- `agent_bus_work_item_id`
- `agent_bus_dispatch_error`

These fields appear on review work items and in lifecycle debug visibility.

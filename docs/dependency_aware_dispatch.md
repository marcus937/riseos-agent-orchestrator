# Dependency-Aware Agent Dispatch

The orchestrator treats dependency metadata as scheduler input. Slack is informational only; it must not determine execution order.

## Layers

- Orchestrator: scheduler and dependency gate.
- Agent Bus: execution layer for Codex-M2, Circuit, Hermes, and future agents.
- Slack: optional notification layer after scheduler decisions.

## Supported Metadata

Issue bodies and direct AgentTask objectives may declare predecessors with either format:

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

A task with dependencies is eligible only when every predecessor is complete. Missing predecessors, malformed dependency metadata, incomplete predecessors, and dependency cycles keep the task queued.

A predecessor is complete when either condition is true:

- The predecessor issue has both `bb2-approved` and `ready-to-merge`.
- A linked PR exposed to the scheduler has `ready-to-merge`.

## Dispatch Points

Dependency checks happen before execution dispatch:

- GitHub issue queue selection filters `agent-ready` issues before applying `agent-next` or creating Agent Bus work items.
- Direct AgentTask dispatch evaluates dependencies before calling Agent Bus `create_work_item`.

Dependency-blocked direct AgentTasks remain queued. They are not marked failed unless Agent Bus itself fails after dependency clearance.

## Sequential Chains

For a chain like #72 -> #73 -> #74 -> #75 -> #76, only #72 is initially eligible. Each dependent issue becomes eligible only after its predecessor reaches the existing BB2/ready-to-merge completion state.

# Engineering Workforce Scheduler

## Purpose

The Engineering Workforce Scheduler is an optional Orchestrator decision layer for unresolved engineering tasks. It chooses between known coding workers without replacing the current explicit dispatch path:

```text
Workflow -> dependency evaluation -> runnable task -> scheduler -> Agent Bus -> assigned coder -> evidence -> review
```

The scheduler is deliberately small: it decides the worker only. It does not create Agent Bus work items, wake Circuit, or change review behavior.

The first supported workers are:

- `codex-m2`
- `circuit-forge`

## Safety Flags

Defaults preserve current behavior.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `ENABLE_ENGINEERING_WORKFORCE_SCHEDULER` | `false` | Enables scheduler mode for generic or omitted engineering targets. |
| `CIRCUIT_ENGINEERING_WORKER_ENABLED` | `true` | Allows Circuit Forge to be selected when scheduler mode is enabled. |
| `CODEX_M2_ENGINEERING_WORKER_ENABLED` | `true` | Allows codex-m2 to be selected when scheduler mode is enabled. |

When `ENABLE_ENGINEERING_WORKFORCE_SCHEDULER=false`, Orchestrator preserves the task `target_agent` and dispatches exactly as before.

## Backwards Compatibility Guarantees

Explicit targets are not silently overridden.

These explicit canonical targets continue through the existing direct dispatch path unchanged:

- `target_agent: codex-m2`
- `target_agent: circuit-forge`

Known aliases are normalized to canonical worker IDs before downstream dispatch so Agent Bus does not receive mixed identities:

- `codex` -> `codex-m2`
- `Codex` -> `codex-m2`
- `circuit` -> `circuit-forge`
- `Circuit Forge` -> `circuit-forge`

The public workflow API remains backwards compatible. `WorkflowTask.target_agent` still defaults to `codex-m2`, but workflow creation records whether the author actually supplied that field:

- `target_agent_explicit: true` means the author selected an agent.
- `target_agent_explicit: false` means the author omitted the field and the scheduler may choose when enabled.

Scheduler mode is only considered when the workflow author omitted `target_agent` or supplied one of these generic engineering targets:

- blank target
- `engineering`
- `coding`
- `engineer`
- `auto-engineer`

## Scheduling Timing

The scheduler runs only after dependency resolution says a task is runnable.

Blocked tasks do not receive:

- scheduler decisions
- scheduler metadata
- Agent Bus work item dispatch

This prevents queued dependency chains from consuming stale worker decisions before their predecessors complete.

## Worker Availability States

Worker availability has three production routing states plus disabled/unavailable handling:

| State | Meaning | Routing behavior |
| --- | --- | --- |
| `available` | Worker health/queue signals indicate the worker can take work. | Candidate may be selected. |
| `busy` | Worker is healthy but has active work or explicit busy status. | Scheduler may consider a safe fallback. |
| `unknown` | Status lookup failed or could not be trusted. | Scheduler preserves the preferred worker and does not automatically reroute. |
| `unavailable` | Worker is disabled, offline, blocked, or unhealthy. | Scheduler may consider a safe fallback. |

`unknown` is intentionally different from `unavailable`. A transient Agent Bus status lookup failure must not move work from `codex-m2` to `circuit-forge` by accident.

## Routing Rules

Initial routing is conservative:

1. Prefer `codex-m2` when enabled, healthy, available, and not busy.
2. Preserve `codex-m2` when its status is `unknown` unless task metadata explicitly prefers another eligible worker.
3. Select `circuit-forge` when `codex-m2` is busy or unavailable and the task is safe for Circuit.
4. Select `circuit-forge` when task metadata explicitly prefers Circuit and Circuit is eligible.
5. Treat Circuit as a safe fallback for backend, documentation, and general coding work.
6. Do not route frontend-specific work to Circuit by default.
7. Do not route frontend repositories to Circuit unless metadata or labels explicitly allow it.

## Repository Guardrails

Repository profile metadata is additive and backwards compatible. The scheduler looks for these keys in task execution evidence or routing metadata:

- `repo_profile`
- `repository_profile`
- `repo_type`
- `project_type`

Current profile rules:

| Profile | Preferred | Fallback |
| --- | --- | --- |
| frontend | `codex-m2` | none unless Circuit frontend is explicitly allowed |
| backend | `codex-m2` | `circuit-forge` when codex is busy/unavailable |
| docs/documentation | either worker | `circuit-forge` is allowed when codex is busy/unavailable |
| general/unknown | `codex-m2` | `circuit-forge` when codex is busy/unavailable and no frontend signal exists |

Frontend guardrail keywords include `frontend`, `front-end`, `ui`, `ux`, `react`, `next.js`, `browser`, `css`, `tailwind`, and `playwright`.

Circuit frontend allow markers include:

- metadata: `allow_circuit_frontend=true`
- metadata: `circuit_frontend_allowed=true`
- metadata: `allow_frontend_for_circuit=true`
- label: `allow-circuit-frontend`
- label: `circuit-frontend-ok`
- label: `circuit-frontend-allowed`

## Worker Assumptions

### codex-m2

`codex-m2` may be busy or unavailable. The scheduler uses Agent Bus signals when the client exposes them:

- `GET /agents/{agent_id}/status`
- `GET /agents/{agent_id}/queue`

If no status or queue methods are available, the scheduler assumes codex-m2 is available to preserve the historical codex-first behavior. If a status lookup is attempted and fails, codex-m2 is marked `unknown` and remains preferred.

### circuit-forge

Circuit Forge is effectively available once triggered, but it does not yet publish a true production heartbeat. The scheduler treats Circuit as available by default unless `CIRCUIT_ENGINEERING_WORKER_ENABLED=false`, subject to repository and frontend guardrails.

## Scheduler Metadata

When scheduler mode selects a worker, Orchestrator records non-invasive metadata on the AgentTask and Agent Bus work item:

- `scheduler_selected_agent`
- `scheduler_reason`
- `scheduler_candidates`
- `scheduler_mode: true`
- `original_target_agent`
- `target_agent_explicit`

The candidate snapshot includes each worker's canonical ID, availability state, busy flag, disabled flag, eligibility, reason, and capabilities.

## Logs

The scheduler emits `[WORKFORCE]` logs for:

- scheduler disabled and target preservation
- explicit target preservation
- alias canonicalization
- task evaluation
- candidate availability
- selected worker and selection reason

## Future Heartbeat Plan

Do not implement Circuit heartbeat scheduling in this milestone.

A future synthetic heartbeat can infer Circuit activity from Agent Bus events already produced by the normal engineering loop:

- work lookup
- claim
- transition
- evidence creation
- review submission
- completion
- dispatch acknowledge/start/block/complete/fail

When Circuit publishes a true heartbeat, the scheduler can switch from default-available Circuit behavior to the same Agent Bus status/queue signal model used for codex-m2.

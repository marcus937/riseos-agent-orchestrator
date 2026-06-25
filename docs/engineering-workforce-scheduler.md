# Engineering Workforce Scheduler

## Purpose

The Engineering Workforce Scheduler is an optional Orchestrator layer for unresolved engineering tasks. It chooses between known coding workers without replacing the current explicit dispatch path:

```text
Workflow -> Orchestrator -> Agent Bus -> assigned coder -> evidence -> review
```

The first supported workers are:

- `codex-m2`
- `circuit-forge`

## Safety Flags

Defaults preserve current behavior.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `ENABLE_ENGINEERING_WORKFORCE_SCHEDULER` | `false` | Enables scheduler mode for generic engineering targets. |
| `CIRCUIT_ENGINEERING_WORKER_ENABLED` | `true` | Allows Circuit Forge to be selected when scheduler mode is enabled. |
| `CODEX_M2_ENGINEERING_WORKER_ENABLED` | `true` | Allows codex-m2 to be selected when scheduler mode is enabled. |

When `ENABLE_ENGINEERING_WORKFORCE_SCHEDULER=false`, Orchestrator preserves the task `target_agent` and dispatches exactly as before.

## Explicit Target Agent Compatibility

Explicit targets are never silently overridden.

These values continue through the existing path unchanged:

- `target_agent: codex-m2`
- `target_agent: codex`
- `target_agent: circuit-forge`
- `target_agent: circuit`
- `target_agent: Circuit Forge`

Scheduler mode is only considered for generic engineering targets:

- missing or blank target
- `engineering`
- `coding`
- `engineer`
- `auto-engineer`

## Routing Rules

Initial routing is conservative:

1. Prefer `codex-m2` when enabled, healthy, available, and not busy.
2. Select `circuit-forge` when `codex-m2` is busy or unavailable and Circuit is enabled.
3. Select `circuit-forge` when task metadata explicitly prefers Circuit.
4. Treat Circuit as safe fallback for docs, backend, and general coding work.
5. Do not route frontend-specific work to Circuit by default.
6. Frontend work may route to Circuit only when task metadata or labels explicitly allow it.

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

If no status or queue methods are available, the scheduler assumes codex-m2 is available to preserve the historical codex-first behavior.

### circuit-forge

Circuit Forge is effectively available once triggered, but it does not yet publish a true production heartbeat. The scheduler treats Circuit as available by default unless `CIRCUIT_ENGINEERING_WORKER_ENABLED=false`.

## Scheduler Metadata

When scheduler mode selects a worker, Orchestrator records non-invasive metadata on the AgentTask and Agent Bus work item:

- `scheduler_selected_agent`
- `scheduler_reason`
- `scheduler_candidates`
- `scheduler_mode: true`
- `original_target_agent`

This metadata is additive and does not change the public workflow response contract.

## Logs

The scheduler emits `[WORKFORCE]` logs for:

- scheduler disabled and explicit target preservation
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

# PR Workflow State Machine

This document defines the canonical Coding Crew PR label workflow for RiseOS Agent Orchestrator.

## Guardrails

- The orchestrator must not merge PRs.
- The orchestrator must not deploy.
- The orchestrator must not mutate branches.
- Human review remains required before merge.
- GitHub writeback is limited to comments and label additions when `ENABLE_GITHUB_WRITEBACK=true`.
- Trigger labels must remain usable and must not be removed by the orchestrator.

## Trigger Labels That Must Remain Available

- `agent-ready` starts Circuit issue pickup.
- `bb-review-needed` requests BB2 review.
- `bb2-needs-changes` routes work back to Circuit for rework.
- `ready-to-merge` is the final human-readable merge-readiness label.

## State Precedence

The orchestrator is label-add-only, so historical labels can remain on a PR after newer labels are added. When labels are mixed, `workflow_state_from_labels()` uses this canonical precedence:

1. BB2 rework or block labels: `bb2-needs-changes`, `bb2-blocked`.
2. Hermes rework or block labels: `agent-revisions`, `agent-blocked`.
3. Final readiness: `ready-to-merge`.
4. BB2 approval or review request: `bb2-approved`, `bb-review-needed`.
5. Hermes verification or request labels: `agent-verified`, `runtime-agent`, `playwright`.
6. Circuit work labels: `agent-working`, `agent-ready`, `agent-next`.

Stale `ready-to-merge` labels must never override newer blocker or rework labels. This keeps a rejected or blocked PR out of the ready-to-merge state until a later approved transition can add fresh readiness evidence.

## States

| State | Canonical labels | Meaning | Next expected handoff |
|---|---|---|---|
| Circuit ready | `agent-ready` | Issue is ready for Circuit pickup. | Circuit claims work. |
| Circuit working | `agent-working` | Circuit is actively working the task. | Circuit opens or updates PR. |
| Hermes requested | `runtime-agent`, optionally `playwright` | Runtime validation is requested. | Hermes validates the PR. |
| Hermes verified | `agent-verified` | Hermes validation passed. This is runtime evidence, not merge approval. | BB2 reviews proof packet. |
| Hermes blocked | `agent-blocked` | Hermes could not run. | Human or agent resolves blocker. |
| Hermes revisions | `agent-revisions` | Hermes found runtime failure. | Circuit updates PR and Hermes can run again on the new commit. |
| BB2 review requested | `bb-review-needed` | BB2 review is requested. | BB2 approves, blocks, or requests changes. |
| BB2 needs changes | `bb2-needs-changes`, `agent-next` | BB2 rejected the packet and sends work back to Circuit. | Circuit reworks on `agent-integration`, then requests validation again. |
| BB2 blocked | `bb2-blocked` | BB2 blocked the packet or escalated it to Marcus. | Human direction is required before work continues. |
| BB2 approved | `bb2-approved` | BB2 approved for human review only. | Orchestrator may add `ready-to-merge` only if all criteria pass. |
| Ready to merge | `ready-to-merge` | Coding Crew loop is complete and human merge review can proceed. | Marcus or another authorized human reviews and merges. |

## Supported Transitions

The current orchestrator supports label-add transitions only. It intentionally does not remove older labels because the supported GitHub client exposes comment and label-add writes, not a safe canonical label replacement operation.

### Circuit Completion To Hermes

When a same-repo PR is opened, synchronized, or marked ready for review from `agent-integration` into `main`, Hermes dispatch can add the canonical trigger labels:

- `runtime-agent`
- `playwright`
- `bb-review-needed`

The Hermes dispatch key includes the commit SHA, so a later Circuit update can trigger validation again for the new commit.

### Hermes Result

Hermes writeback adds exactly one result label:

- pass: `agent-verified`
- runtime failure: `agent-revisions`
- blocked dispatch: `agent-blocked`

### BB2 Decision

BB2 writeback uses `app.pr_workflow_state.bb2_decision_transition_labels`.

- `APPROVED_FOR_HUMAN_REVIEW` adds `bb2-approved`.
- It also adds `ready-to-merge` only when the current labels already include `agent-verified` and do not include blocker or rework labels.
- `NEEDS_CHANGES` adds `bb2-needs-changes` and `agent-next` so Circuit can pick the work back up.
- `BLOCKED` and `ESCALATE_TO_MARCUS` add `bb2-blocked`.

## Ready-To-Merge Criteria

The orchestrator may add `ready-to-merge` only when all of these are true:

- `bb2-approved` is present or being added by the current BB2 decision.
- `agent-verified` is already present as Hermes runtime evidence.
- None of these blocker or rework labels are present: `agent-blocked`, `agent-revisions`, `bb2-needs-changes`, `bb2-blocked`.

The orchestrator does not treat missing runtime labels as proof that runtime validation was intentionally not required. That exception needs an explicit future signal before it can safely add `ready-to-merge` without `agent-verified`.

## Known Limitation

Because label removal is not part of the current writeback contract, a PR may still display historical labels. Consumers should treat the state machine helper as canonical for new transitions and prefer the highest-priority state in `workflow_state_from_labels` when labels are mixed.

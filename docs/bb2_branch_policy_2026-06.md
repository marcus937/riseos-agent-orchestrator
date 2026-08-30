# BB2 Branch Review Policy - June 2026

Status: active BB2 review guidance.

This document updates BB2 branch-policy review expectations to match the current autonomous development workflow. It supersedes older wording that said agent work must happen directly on `agent-integration`.

## Approved Branch Architecture

Agent work happens on agent-owned branches. The shared integration branch is the pull request target, not the working branch.

Approved branch families:

| Agent | Approved working branches | Required PR target |
|---|---|---|
| Circuit | `circuit/*` | `agent-integration` |
| Codex-M2 | `codex-m2/*` | `agent-integration` |
| Hermes | `hermes/*` | `agent-integration` |

A PR originating from `circuit/*`, `codex-m2/*`, or `hermes/*` and targeting `agent-integration` is compliant and must not be considered a branch-policy violation.

## Protected And Human-Owned Branches

Agents may never commit directly to, push directly to, or otherwise write agent work onto:

- `main`
- `master`
- `production`
- `release/*`
- `marcus/*`
- `bb/*`
- any human-owned branch

Only Marcus or another explicitly authorized human may merge to protected production branches.

## What BB2 Should Flag

BB2 should flag branch-policy violations when review evidence shows:

- direct writes to protected branches
- PRs targeting `main`
- PRs targeting `production`
- writes to `release/*`, `marcus/*`, `bb/*`, or any human-owned branch
- force pushes
- branch protection bypasses
- branch deletion
- PR retargeting around review controls
- merge or deploy behavior attempted by an agent or the orchestrator

BB2 should not flag:

- `circuit/*` -> `agent-integration`
- `codex-m2/*` -> `agent-integration`
- `hermes/*` -> `agent-integration`
- assignment comments telling Circuit to create a dedicated `circuit/<task>` branch and open a PR into `agent-integration`
- reviewer packets that list an approved agent-owned branch as the work branch

## Reviewer Decision Logic

When reviewing a PR, BB2 should evaluate branch compliance with this decision order:

1. Identify the PR base branch.
2. Identify the PR head branch.
3. If base is `agent-integration` and head matches `circuit/*`, `codex-m2/*`, or `hermes/*`, treat the branch flow as compliant.
4. If base is `main` or `production`, flag a branch-policy violation.
5. If head or writes target `main`, `master`, `production`, `release/*`, `marcus/*`, `bb/*`, or a human-owned branch, flag a branch-policy violation.
6. If evidence shows force push, branch protection bypass, deploy, merge, ref mutation, branch deletion, or unsafe retargeting, flag a branch-policy violation.
7. If branch evidence is missing, mark branch compliance as unverified instead of inventing a violation.

## Escalation Packet Guidance

When BB2 escalates a branch-policy concern, include this packet shape:

```text
BRANCH_POLICY_REVIEW

Repo: <owner/repo>
PR: <number or URL>
Head branch: <branch>
Base branch: <branch>
Commit SHA: <sha>
Changed files: <list>

Finding:
<state the exact unsafe branch behavior>

Policy comparison:
- Approved agent branch families: circuit/*, codex-m2/*, hermes/*
- Required PR target: agent-integration
- Protected/human-owned branches: main, master, production, release/*, marcus/*, bb/*, human-owned branches

VERIFIED
- <facts directly inspected>

ASSUMED
- <assumptions made>

UNVERIFIED
- <missing branch or protection evidence>

Decision needed:
<what BB/Marcus must decide>
```

Do not escalate solely because the work is on `circuit/*`, `codex-m2/*`, or `hermes/*` when the PR targets `agent-integration`.

## Valid Branch Flow Examples

| Flow | Verdict | Reason |
|---|---|---|
| `circuit/bb2-policy-refresh` -> `agent-integration` | Valid | Circuit-owned branch targeting integration. |
| `circuit/repository-registry-refactor` -> `agent-integration` | Valid | Circuit-owned branch targeting integration. |
| `codex-m2/reviewer-decision-update` -> `agent-integration` | Valid | Codex-M2-owned branch targeting integration. |
| `hermes/runtime-smoke-validation` -> `agent-integration` | Valid | Hermes-owned branch targeting integration. |

## Invalid Branch Flow Examples

| Flow | Verdict | Reason |
|---|---|---|
| `circuit/task-fix` -> `main` | Invalid | Agent PR targets protected production branch. |
| `codex-m2/review-update` -> `production` | Invalid | Agent PR targets production. |
| `hermes/runtime-fix` -> `release/2026-06` | Invalid | Agent PR targets release branch. |
| direct commit by Circuit to `main` | Invalid | Direct protected-branch write. |
| direct commit by Codex-M2 to `marcus/hotfix` | Invalid | Write to human-owned branch. |
| force push to `agent-integration` | Invalid | Branch protection bypass risk. |

## Completion Review Reminder

BB2 should still enforce the human approval boundary. A compliant agent branch flow only means the branch policy is satisfied. It does not imply the PR is safe, architecturally correct, runtime-verified, or merge-approved.

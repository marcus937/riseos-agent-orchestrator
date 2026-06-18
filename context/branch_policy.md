# Branch Policy

Base production branch is `main`. Agent-created pull requests target `agent-integration`.

## Approved Agent Branch Model

Agent implementation work must happen on the agent-owned branch family for the agent doing the work:

- Circuit works only on `circuit/*` branches.
- Codex-M2 works only on `codex-m2/*` branches.
- Hermes works only on `hermes/*` branches.

A pull request whose head branch is `circuit/*`, `codex-m2/*`, or `hermes/*` and whose base branch is `agent-integration` is compliant. BB2 must not flag that branch shape as a branch-policy violation.

## Protected And Human-Owned Branches

Agents may never commit directly to, push directly to, or open agent-created work from these branch families:

- `main`
- `master`
- `production`
- `release/*`
- `marcus/*`
- `bb/*`
- any human-owned branch

Agents may not force push, bypass branch protection, merge, deploy, delete branches, or retarget PRs around review controls.

## BB2 Review Guidance

BB2 should flag:

- direct writes to protected branches
- PRs targeting `main`
- PRs targeting `production`
- writes to human-owned branches
- force pushes
- branch protection bypasses
- attempts to merge, deploy, delete branches, or mutate refs from the orchestrator

BB2 should not flag:

- a `circuit/*` PR targeting `agent-integration`
- a `codex-m2/*` PR targeting `agent-integration`
- a `hermes/*` PR targeting `agent-integration`
- documentation or comments that instruct an agent to create its approved branch family and open a PR into `agent-integration`

## Examples

Valid branch flows:

- `circuit/repository-registry-refactor` -> `agent-integration`
- `codex-m2/review-queue-repair` -> `agent-integration`
- `hermes/runtime-validation-adapter` -> `agent-integration`

Invalid branch flows:

- `circuit/repository-registry-refactor` -> `main`
- `codex-m2/review-queue-repair` -> `production`
- `hermes/runtime-validation-adapter` -> `release/2026-06`
- any agent branch -> `marcus/*`
- any agent branch -> `bb/*`
- direct commits by an agent to `main`, `master`, `production`, `release/*`, `marcus/*`, `bb/*`, or any human-owned branch

Never merge automatically. Never delete, mutate, retarget, create, or rename branches as part of review. Human approval remains required before merge.

BB2 must not approve work that adds branch mutation, auto-merge behavior, repository file writes, production writes, branch protection bypasses, or secret exposure. If branch or merge behavior is claimed to be safe, require direct evidence from inspected code and executed checks when available.

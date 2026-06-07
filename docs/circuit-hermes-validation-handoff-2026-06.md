# Circuit Hermes Validation Handoff - June 2026

Issue: #60 - Transition Circuit Validation Handoff from GitHub Actions to Hermes

## Purpose

Hermes is the primary runtime validation engine after Circuit creates or updates a pull request. GitHub Actions remains a lightweight safety layer for repository sanity checks, dependency checks, and fallback visibility, but it is no longer the canonical runtime evidence source for Circuit completion.

## Canonical Hermes Trigger Labels

Circuit PRs use this canonical trigger set:

- `runtime-agent`
- `playwright`
- `bb-review-needed`

These labels mean the PR should enter the Hermes runtime validation path. They are not merge approval and do not replace BB2 or Marcus review.

## Final Agent Workflow

1. A GitHub Issue is approved for Circuit with `agent-ready` or `agent-next`.
2. Circuit works only inside the allowed branch rule, currently `agent-integration` for this orchestrator lane.
3. Circuit opens or updates a draft PR into `main` from `agent-integration`.
4. The orchestrator accepts the `pull_request` webhook.
5. If the PR head branch is `agent-integration`, the orchestrator treats the PR as Hermes-eligible even when the trigger labels are not already present.
6. When GitHub writeback is enabled, the orchestrator applies `runtime-agent`, `playwright`, and `bb-review-needed` to the PR.
7. The orchestrator sends Hermes a validation job payload containing repo, PR number, branch, commit SHA, target URL, trigger route, and canonical labels.
8. Hermes runs the configured runtime validation profile and returns status plus evidence.
9. The orchestrator comments the Hermes packet and applies the result label:
   - `agent-verified` for passed validation.
   - `agent-revisions` for failed validation.
   - `agent-blocked` when Hermes cannot validate.
10. BB2 reviews the Circuit packet and Hermes runtime packet together.
11. Marcus remains the only merge authority.

## GitHub Actions Role

GitHub Actions should be retained only for lightweight checks such as:

- unit tests or syntax checks when available
- dependency install sanity
- static safety checks
- fallback artifact storage when Hermes delegates a profile to Actions

GitHub Actions should not be the primary runtime handoff gate for Circuit PR readiness.

## Runtime Safety Boundaries

Hermes must remain read-only by default:

- no merge
- no deploy
- no force push
- no branch deletion
- no branch protection bypass
- no production writes
- no secrets in comments, labels, screenshots, or artifacts

Blocked Hermes runs should produce a `BLOCKED` packet explaining what input or environment is needed before BB2 continues.

## VERIFIED

- Circuit PRs from `agent-integration` now enter Hermes routing on `opened`, `synchronize`, and `ready_for_review` events.
- The canonical Hermes trigger labels are `runtime-agent`, `playwright`, and `bb-review-needed`.
- When GitHub writeback is enabled, those trigger labels are applied automatically before the Hermes job is dispatched.
- Hermes job payloads include the canonical labels for Circuit PRs even if the webhook payload did not already contain them.

## ASSUMED

- `agent-integration` remains the current Circuit work branch for this orchestrator lane.
- `HERMES_DEFAULT_TARGET` is configured to the intended preview, staging, local, or simulator target before live validation is enabled.
- `ENABLE_GITHUB_WRITEBACK=true` is required for the orchestrator to apply labels or comment back to GitHub.

## UNVERIFIED

- Live Hermes M2 runtime execution was not performed by this documentation update.
- GitHub Actions workflow definitions were not changed in this task.
- Production deployment and merge behavior remain untouched.

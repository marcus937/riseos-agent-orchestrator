# GitHub Issue Task Workflow

Status: operational contract for Circuit and BB2 issue-based task dispatch.

## Purpose

GitHub Issues are the shared task queue between Circuit and BB2. Circuit works one queued task at a time on `agent-integration`; BB2 reviews the completed commit. When BB2 approves work for human review, the orchestrator may assign the next ready issue.

This workflow never merges, mutates branches, closes issues, opens PRs, or writes repository files.

## Feature Flags

| Flag | Default | Effect |
| --- | ---: | --- |
| `ENABLE_GITHUB_WRITEBACK` | `false` | Allows review comments and labels on issues/PRs. |
| `ENABLE_TASK_DISPATCH` | `false` | Allows next-task discovery and assignment comments/labels after approved BB2 review. |

Task dispatch requires both flags to be `true`. If either flag is false, no task assignment comment or dispatch label is posted.

## Labels

Task queue labels:

- `agent-task`: issue is part of the agent task queue.
- `agent-ready`: issue is ready for Circuit selection.
- `agent-working`: optional marker for a task currently in progress.
- `agent-next`: orchestrator selected this issue as the next Circuit assignment.

BB2 review labels:

- `bb2-review-needed`: task is waiting for BB2 review.
- `bb2-approved`: BB2 approved the completed work for human review.
- `bb2-needs-changes`: BB2 requested changes.
- `bb2-blocked`: BB2 blocked or escalated the task.

Decision label mapping:

| BB2 decision | GitHub label |
| --- | --- |
| `APPROVED_FOR_HUMAN_REVIEW` | `bb2-approved` |
| `NEEDS_CHANGES` | `bb2-needs-changes` |
| `BLOCKED` | `bb2-blocked` |
| `ESCALATE_TO_MARCUS` | `bb2-blocked` |

## Ready Issue Selection

The orchestrator selects the next task with these rules:

1. Open issues only.
2. Must have `agent-task` and `agent-ready`.
3. Must not have `bb2-blocked`.
4. Pull requests returned by the GitHub Issues API are ignored.
5. Oldest created issue wins.

## Dispatch Behavior

When a review queue item is processed and GitHub writeback succeeds:

1. The BB2 review comment is posted to the PR or issue currently being reviewed.
2. The BB2 decision label is applied.
3. If the decision is `APPROVED_FOR_HUMAN_REVIEW` and `ENABLE_TASK_DISPATCH=true`, the orchestrator searches the same repo for the next ready issue.
4. If a ready issue exists, the orchestrator posts a Circuit assignment comment and applies `agent-next`.
5. If no ready issue exists, the process response includes `task_dispatch_error: "No queued agent-ready issue found"`.

No other GitHub writes are allowed.

## Assignment Comment Shape

Example:

```markdown
## Circuit Assignment

Issue: #42 - Add read-only Mission Control traffic contract

Branch: `agent-integration` only.

Reminders:
- Stay on `agent-integration`.
- Comment `Status: Done` with the completed commit SHA when finished.
- Do not merge.
- Do not open a PR unless explicitly requested.
- Do not mutate branches.

Task summary:
Implement the task from the issue body.
```

## Process Response Fields

The process endpoint includes task dispatch result fields:

```json
{
  "task_dispatch_attempted": true,
  "task_dispatch_success": true,
  "task_dispatch_issue_number": 42,
  "task_dispatch_error": null
}
```

When dispatch is disabled, all fields remain false or null. When no ready issue exists, `task_dispatch_attempted` is true, `task_dispatch_success` is false, and `task_dispatch_error` explains the condition.

## Human Approval Boundary

`bb2-approved` means approved for human review only. It does not authorize a merge. Humans remain responsible for final approval and merge decisions.

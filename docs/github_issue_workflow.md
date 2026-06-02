# GitHub Issue Workflow

## MVP Goal

Turn GitHub activity into clear planning signals for RiseOS coding agents without allowing autonomous code writes or merges.

## Supported Triggers

| Event | MVP Use |
|---|---|
| `issue_comment` | Detect planning commands, review requests, and task updates. |
| `push` | Detect branch activity for an assigned task. |
| `pull_request` | Detect PR lifecycle changes and review readiness. |

## Suggested Labels

- `agent:pending`
- `agent:assigned`
- `agent:working`
- `agent:review-needed`
- `agent:needs-changes`
- `agent:blocked`
- `agent:done`

## Human Review Boundary

The orchestrator can prepare review comments and labels, but humans remain responsible for approving and merging PRs.

## No Auto-Merge

Auto-merge is out of scope. The GitHub client wrapper intentionally has no merge method.

## Future Flow

1. Parse issue comment intent.
2. Fetch relevant commit or branch context.
3. Build an OpenAI reviewer prompt.
4. Request a review decision.
5. Post a comment and/or apply a label.
6. Leave final approval and merge to humans.

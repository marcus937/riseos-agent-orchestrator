# Operations

## Runtime Safety

The orchestrator is default-safe. GitHub context hydration, OpenAI review, GitHub writeback, and task dispatch are disabled unless their feature flags are set. Auto-merge, branch mutation, repository file writes, releases, deletes, and issue closing are out of scope.

Read-only debug endpoints are public by default when `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=false`:

- `GET /debug/health`
- `GET /debug/recent-events`
- `GET /debug/review-queue`
- `GET /debug/review-queue/{id}`

Processing is protected:

```bash
curl -X POST "https://orchestrator.riseconnect.us/debug/review-queue/<work-item-id>/process" \
  -H "X-Orchestrator-Admin-Token: $ORCHESTRATOR_ADMIN_TOKEN"
```

Protected debug read mode:

```bash
curl "https://orchestrator.riseconnect.us/debug/health" \
  -H "X-Orchestrator-Admin-Token: $ORCHESTRATOR_ADMIN_TOKEN"
```

Set `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true` to require this header on every `/debug/*` endpoint. The process endpoint always requires the header regardless of the flag.

## Restart

```bash
sudo systemctl restart riseos-agent-orchestrator
sudo systemctl status riseos-agent-orchestrator --no-pager
```

## Logs

```bash
sudo journalctl -u riseos-agent-orchestrator -n 100 --no-pager
sudo journalctl -u riseos-agent-orchestrator -f
```

Structured log events include:

- `webhook_accepted`
- `queue_item_created`
- `review_processing_started`
- `openai_review_attempted`
- `openai_review_succeeded`
- `openai_review_failed`
- `github_writeback_attempted`
- `github_writeback_succeeded`
- `github_writeback_failed`

Task dispatch status is returned on the process response with `task_dispatch_attempted`, `task_dispatch_success`, `task_dispatch_issue_number`, and `task_dispatch_error`.

## DB Backup

The default SQLite path is `/var/lib/riseos-agent-orchestrator/orchestrator.db`.

```bash
sudo install -d -m 750 -o riseos -g riseos /var/backups/riseos-agent-orchestrator
sudo sqlite3 /var/lib/riseos-agent-orchestrator/orchestrator.db \
  ".backup '/var/backups/riseos-agent-orchestrator/orchestrator-$(date +%Y%m%d-%H%M%S).db'"
```

## Queue Limit

`ORCHESTRATOR_MAX_REVIEW_ITEMS` defaults to `500`. When the persisted queue exceeds the limit, the service prunes only the oldest processed items. Pending and reviewing items are preserved.

Duplicate pending work items are suppressed for the same repo, event type, commit SHA, PR number, and issue number.

## Feature Flags

| Flag | Default | Effect |
|---|---:|---|
| `ENABLE_GITHUB_CONTEXT_HYDRATION` | `false` | Fetches read-only commit or branch comparison context. |
| `ENABLE_OPENAI_REVIEW` | `false` | Requests validated OpenAI `ReviewDecision` JSON. Requires `OPENAI_API_KEY`. |
| `ENABLE_BB_CONTEXT_PACK` | `true` | Adds BB Architect context packs to OpenAI review prompts. |
| `ENABLE_GITHUB_WRITEBACK` | `false` | Posts a review comment and one BB2 decision label after processing. Requires GitHub token permissions. |
| `ENABLE_TASK_DISPATCH` | `false` | After an approved BB2 review, selects the oldest queued `agent-ready` issue and posts a Circuit assignment. Requires `ENABLE_GITHUB_WRITEBACK=true`. |
| `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS` | `false` | Requires `X-Orchestrator-Admin-Token` on read-only `/debug/*` routes. |

`ENABLE_GITHUB_WRITEBACK=true` is the only flag that enables GitHub review writes. `ENABLE_TASK_DISPATCH=true` adds only issue comments and labels, and only after writeback is also enabled.

## Task Dispatch

Task dispatch turns GitHub Issues into the shared task queue between Circuit and BB2.

Eligibility rules:

- Issue is open.
- Issue has `agent-task`.
- Issue has `agent-ready`.
- Issue does not have `bb2-blocked`.
- Oldest created eligible issue is selected first.

When an approved BB2 review is processed and both writeback flags are enabled, the orchestrator posts the existing BB2 review comment/label first. It then attempts to select the next ready issue in the same repository. If one is found, it posts a `Circuit Assignment` comment and applies `agent-next`. If none is found, the process response includes `task_dispatch_error: "No queued agent-ready issue found"`.

Task dispatch must not:

- close issues
- merge PRs
- mutate branches
- open PRs
- write repository files
- remove queued labels unless a future requirement explicitly adds that behavior

BB2 decision labels:

| Decision | Label |
|---|---|
| `APPROVED_FOR_HUMAN_REVIEW` | `bb2-approved` |
| `NEEDS_CHANGES` | `bb2-needs-changes` |
| `BLOCKED` | `bb2-blocked` |
| `ESCALATE_TO_MARCUS` | `bb2-blocked` |

## Common Failures

`401 Invalid GitHub webhook signature`: Confirm `GITHUB_WEBHOOK_SECRET` matches the GitHub webhook shared secret.

`401 Invalid orchestrator admin token`: Include `X-Orchestrator-Admin-Token` with the value from `/etc/riseos-agent-orchestrator.env`.

`403 ORCHESTRATOR_ADMIN_TOKEN is required`: Set `ORCHESTRATOR_ADMIN_TOKEN` and restart the service.

`OpenAI review failed`: Check `OPENAI_API_KEY`, `OPENAI_REVIEW_MODEL`, and the `openai_review_error` field in the process response.

`GitHub writeback failed`: Check `GITHUB_TOKEN` permissions. Writeback needs issue/PR comment and label access.

`Task dispatch did not run`: Confirm `ENABLE_GITHUB_WRITEBACK=true`, `ENABLE_TASK_DISPATCH=true`, the BB2 decision is `APPROVED_FOR_HUMAN_REVIEW`, and the target repository has an open issue labeled `agent-task` and `agent-ready` without `bb2-blocked`.

`No queued agent-ready issue found`: Add or relabel the next issue with `agent-task` and `agent-ready`, or leave the queue empty until Marcus/BB adds the next task.

`Empty recent-events list`: Use GitHub webhook Redeliver and check NGINX/systemd logs.

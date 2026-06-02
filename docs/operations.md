# Operations

## Runtime Safety

The orchestrator is default-safe. GitHub context hydration, OpenAI review, and GitHub writeback are disabled unless their feature flags are set. Auto-merge, branch mutation, repository file writes, releases, and deletes are out of scope.

Read-only debug endpoints are public for now:

- `GET /debug/health`
- `GET /debug/recent-events`
- `GET /debug/review-queue`
- `GET /debug/review-queue/{id}`

Processing is protected:

```bash
curl -X POST "https://orchestrator.riseconnect.us/debug/review-queue/<work-item-id>/process" \
  -H "X-Orchestrator-Admin-Token: $ORCHESTRATOR_ADMIN_TOKEN"
```

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
| `ENABLE_GITHUB_WRITEBACK` | `false` | Posts a comment and one label after processing. Requires GitHub token permissions. |

`ENABLE_GITHUB_WRITEBACK=true` is the only flag that enables GitHub writes, and those writes remain limited to issue/PR comments and labels.

## Common Failures

`401 Invalid GitHub webhook signature`: Confirm `GITHUB_WEBHOOK_SECRET` matches the GitHub webhook shared secret.

`401 Invalid orchestrator admin token`: Include `X-Orchestrator-Admin-Token` with the value from `/etc/riseos-agent-orchestrator.env`.

`403 ORCHESTRATOR_ADMIN_TOKEN is required`: Set `ORCHESTRATOR_ADMIN_TOKEN` and restart the service.

`OpenAI review failed`: Check `OPENAI_API_KEY`, `OPENAI_REVIEW_MODEL`, and the `openai_review_error` field in the process response.

`GitHub writeback failed`: Check `GITHUB_TOKEN` permissions. Writeback needs issue/PR comment and label access.

`Empty recent-events list`: Use GitHub webhook Redeliver and check NGINX/systemd logs.

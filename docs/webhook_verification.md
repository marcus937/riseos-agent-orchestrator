# Webhook Verification

Use this checklist to confirm GitHub webhooks are reaching the deployed orchestrator at `orchestrator.riseconnect.us` and being captured by the in-memory debug event store.

## Assumptions

- The deployed service is listening locally on `127.0.0.1:8015`.
- NGINX proxies public HTTPS traffic for `orchestrator.riseconnect.us`.
- The GitHub webhook secret in GitHub matches `GITHUB_WEBHOOK_SECRET` on the server.
- The debug event store is in memory, so recent events reset when the service restarts.

## Verification Checklist

- [ ] Server-local `/health` returns `{"status":"ok"}`.
- [ ] Server-local `/debug/health` returns counters and uptime.
- [ ] Server-local `/debug/recent-events` returns a JSON list.
- [ ] Public `/health` returns `{"status":"ok"}`.
- [ ] Public `/debug/health` returns counters and uptime.
- [ ] Public `/debug/recent-events` returns a JSON list.
- [ ] GitHub webhook is configured with the correct URL, content type, secret, and events.
- [ ] GitHub Redeliver succeeds with a `2xx` response.
- [ ] A new EventRecord appears in `/debug/recent-events`.

## Server-Local Checks

Run these commands on the Vultr server:

```bash
curl -sS http://127.0.0.1:8015/health
curl -sS http://127.0.0.1:8015/debug/health
curl -sS http://127.0.0.1:8015/debug/recent-events
```

Expected `/debug/health` shape:

```json
{
  "webhook_count": 1,
  "accepted_count": 1,
  "rejected_count": 0,
  "uptime": 123.456
}
```

## Public Checks

Run these from any machine with internet access:

```bash
curl -sS https://orchestrator.riseconnect.us/health
curl -sS https://orchestrator.riseconnect.us/debug/health
curl -sS https://orchestrator.riseconnect.us/debug/recent-events
```

If public checks fail but server-local checks pass, inspect NGINX and TLS first.

## GitHub Webhook Setup

In GitHub, open the repository settings and create or edit the webhook:

- Payload URL: `https://orchestrator.riseconnect.us/webhooks/github`
- Content type: `application/json`
- Secret: use the same value as `GITHUB_WEBHOOK_SECRET` on the server
- SSL verification: enabled
- Events:
  - `push`
  - `pull_request`
  - `issue_comment`
  - `issues`

The orchestrator currently accepts `push`, `pull_request`, and `issue_comment`. The `issues` event can be enabled now for GitHub-side workflow visibility, but unsupported event types will not create accepted EventRecords until runtime support is added.

## Use GitHub Redeliver

To resend a webhook delivery:

1. Open the GitHub repository.
2. Go to Settings -> Webhooks.
3. Select the orchestrator webhook.
4. Open the Recent Deliveries tab.
5. Pick a delivery for `push`, `pull_request`, or `issue_comment`.
6. Click Redeliver.
7. Confirm GitHub shows a `2xx` response.
8. Check `https://orchestrator.riseconnect.us/debug/recent-events`.

## Expected EventRecord Examples

Push to `agent-integration`:

```json
{
  "event_id": "generated-uuid",
  "github_event": "push",
  "repo_full_name": "marcus937/riseos-agent-orchestrator",
  "branch": "agent-integration",
  "commit_sha": "abc123",
  "issue_number": null,
  "pr_number": null,
  "received_at": "2026-06-02T18:51:53.079736Z",
  "raw_action": null
}
```

Issue comment with `Status: Done`:

```json
{
  "event_id": "generated-uuid",
  "github_event": "issue_comment",
  "repo_full_name": "marcus937/riseos-agent-orchestrator",
  "branch": null,
  "commit_sha": null,
  "issue_number": 42,
  "pr_number": null,
  "received_at": "2026-06-02T18:52:10.123456Z",
  "raw_action": "created"
}
```

Pull request `opened` or `synchronize` from `agent-integration`:

```json
{
  "event_id": "generated-uuid",
  "github_event": "pull_request",
  "repo_full_name": "marcus937/riseos-agent-orchestrator",
  "branch": "agent-integration",
  "commit_sha": "def456",
  "issue_number": null,
  "pr_number": 7,
  "received_at": "2026-06-02T18:53:20.654321Z",
  "raw_action": "synchronize"
}
```

## Troubleshooting

### 401 Invalid Signature

- Confirm GitHub webhook Secret matches `GITHUB_WEBHOOK_SECRET` exactly.
- Confirm the deployed service was restarted after editing `/etc/riseos-agent-orchestrator.env`.
- Redeliver after updating the secret. Old deliveries signed with an old secret will still fail.

### 400 Missing Event Header

- Confirm requests include `X-GitHub-Event`.
- GitHub sends this header automatically. Manual `curl` tests must include it.

### 404 Not Found

- Confirm the URL path is exactly `/webhooks/github`, `/health`, `/debug/health`, or `/debug/recent-events`.
- Confirm NGINX allows the debug paths. If NGINX only proxies `/health` and `/webhooks/github`, add proxy locations for `/debug/health` and `/debug/recent-events`.

### 502 From NGINX

- Confirm Uvicorn is running on `127.0.0.1:8015`.
- Check service status: `sudo systemctl status riseos-agent-orchestrator --no-pager`.
- Check logs: `sudo journalctl -u riseos-agent-orchestrator -n 100 --no-pager`.
- Confirm the NGINX upstream port matches the service port.

### Empty Recent Events List

- Confirm GitHub Redeliver returns a `2xx` response.
- Confirm the event type is currently accepted: `push`, `pull_request`, or `issue_comment`.
- Remember the store is in memory; restarting the service clears recent events.
- Check `/debug/health`: if `accepted_count` is `0`, the service has not accepted a webhook since startup.

## Safety Notes

- This verification flow does not require OpenAI.
- This verification flow does not comment on GitHub.
- This verification flow does not modify repositories.
- This verification flow does not merge anything.

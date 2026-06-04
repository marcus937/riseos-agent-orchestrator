# Vultr Deployment: RiseOS Agent Orchestrator

This guide deploys the FastAPI orchestrator at `https://orchestrator.riseconnect.us` on a Vultr server behind NGINX and HTTPS. The app remains dry-run safe: it accepts signed GitHub webhooks, identifies review-needed events, and returns a review stub without merging, changing branches, or writing production data.

## Assumptions

- DNS A record for `orchestrator.riseconnect.us` points to the Vultr server.
- Ubuntu 22.04 or 24.04 is installed.
- Deployment user is `riseos`.
- App path is `/opt/riseos-agent-orchestrator`.
- SQLite state path is `/var/lib/riseos-agent-orchestrator/orchestrator.db`.
- Uvicorn listens on `127.0.0.1:8010` only.
- NGINX is the public HTTPS entrypoint.

## 1. Install System Packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx certbot python3-certbot-nginx
```

## 2. Create Service User And Clone Repo

```bash
sudo useradd --system --create-home --shell /bin/bash riseos || true
sudo mkdir -p /opt
sudo chown riseos:riseos /opt
sudo install -d -m 750 -o riseos -g riseos /var/lib/riseos-agent-orchestrator
sudo -u riseos git clone https://github.com/marcus937/riseos-agent-orchestrator.git /opt/riseos-agent-orchestrator
cd /opt/riseos-agent-orchestrator
sudo -u riseos git checkout agent-integration
```

## 3. Create Virtual Environment And Install Dependencies

```bash
cd /opt/riseos-agent-orchestrator
sudo -u riseos python3 -m venv .venv
sudo -u riseos .venv/bin/pip install --upgrade pip
sudo -u riseos .venv/bin/pip install -e .
```

For test tooling on the server, install the dev extra instead:

```bash
sudo -u riseos .venv/bin/pip install -e '.[dev]'
```

## 4. Create Environment File

Create `/etc/riseos-agent-orchestrator.env` with root-only permissions.

```bash
sudo install -m 600 -o root -g root /dev/null /etc/riseos-agent-orchestrator.env
sudo nano /etc/riseos-agent-orchestrator.env
```

Example contents:

```bash
APP_ENV=production
GITHUB_WEBHOOK_SECRET=replace-with-github-webhook-secret
GITHUB_TOKEN=replace-with-fine-grained-token-if-needed
OPENAI_API_KEY=
OPENAI_REVIEW_MODEL=gpt-5.5-thinking
ENABLE_OPENAI_REVIEW=false
ENABLE_BB_CONTEXT_PACK=true
BB_CONTEXT_MAX_CHARS=20000
ENABLE_GITHUB_CONTEXT_HYDRATION=false
ENABLE_GITHUB_WRITEBACK=false
WORK_BRANCH=agent-integration
BASE_BRANCH=main
ORCHESTRATOR_DB_PATH=/var/lib/riseos-agent-orchestrator/orchestrator.db
ORCHESTRATOR_ADMIN_TOKEN=replace-with-long-random-admin-token
ORCHESTRATOR_MAX_REVIEW_ITEMS=500
REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=false
```

Do not commit real values. `ENABLE_OPENAI_REVIEW=false` keeps review processing deterministic and prevents live OpenAI calls. When it is set to `true`, `OPENAI_API_KEY` is required and `OPENAI_REVIEW_MODEL` is used to request validated `ReviewDecision` JSON.

`ENABLE_BB_CONTEXT_PACK=true` adds BB Architect context packs to OpenAI review prompts, including the global architect prompt, review rubric, branch policy, and a matching repo profile when available. `BB_CONTEXT_MAX_CHARS=20000` bounds the context included in the prompt. Set `ENABLE_BB_CONTEXT_PACK=false` to omit this context and preserve the previous prompt shape.

`ORCHESTRATOR_DB_PATH` enables SQLite persistence for accepted webhook events and review queue items. The service creates the database file and tables at startup. Keep `/var/lib/riseos-agent-orchestrator` owned by `riseos:riseos` so the systemd service can write there.

`ORCHESTRATOR_ADMIN_TOKEN` protects `POST /debug/review-queue/{id}/process`. `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=false` leaves read-only debug endpoints public for local-style verification. Set it to `true` in production if `/debug/*` metadata should require `X-Orchestrator-Admin-Token`. `ORCHESTRATOR_MAX_REVIEW_ITEMS=500` caps persisted review queue rows; only oldest processed items are pruned.

`ENABLE_GITHUB_CONTEXT_HYDRATION=false` keeps review processing fully deterministic and offline. Set it to `true` only when `GITHUB_TOKEN` has the read permissions needed for commits and branch comparisons. Hydration remains read-only and does not comment, label, mutate repositories, or merge.

`ENABLE_GITHUB_WRITEBACK=false` prevents production GitHub writes. Set it to `true` only when dry-run review decisions should be posted back as comments and one status label. Writeback remains limited to `post_issue_comment` and `apply_label`; it does not merge, change branches, write repository files, release, or delete anything.

## 5. Install Systemd Service

```bash
cd /opt/riseos-agent-orchestrator
sudo cp deploy/riseos-agent-orchestrator.service /etc/systemd/system/riseos-agent-orchestrator.service
sudo systemctl daemon-reload
sudo systemctl enable --now riseos-agent-orchestrator
sudo systemctl status riseos-agent-orchestrator --no-pager
```

If the data directory was not created earlier, create it before starting the service:

```bash
sudo install -d -m 750 -o riseos -g riseos /var/lib/riseos-agent-orchestrator
```

Local service check:

```bash
curl -sS http://127.0.0.1:8010/health
```

Expected response:

```json
{"status":"ok"}
```

## 6. Configure NGINX

```bash
cd /opt/riseos-agent-orchestrator
sudo cp deploy/nginx/orchestrator.riseconnect.us.conf /etc/nginx/sites-available/orchestrator.riseconnect.us.conf
sudo ln -sf /etc/nginx/sites-available/orchestrator.riseconnect.us.conf /etc/nginx/sites-enabled/orchestrator.riseconnect.us.conf
sudo nginx -t
sudo systemctl reload nginx
```

Before HTTPS, verify HTTP routing:

```bash
curl -i http://orchestrator.riseconnect.us/health
```

## 7. Enable HTTPS With Certbot

```bash
sudo certbot --nginx -d orchestrator.riseconnect.us
sudo systemctl reload nginx
```

Verify renewal is configured:

```bash
sudo certbot renew --dry-run
```

## 8. Public Health Check

```bash
curl -sS https://orchestrator.riseconnect.us/health
```

Expected response:

```json
{"status":"ok"}
```

## 9. Signed Webhook Test

Generate a signed test payload using the same secret stored in `/etc/riseos-agent-orchestrator.env`.

```bash
GITHUB_WEBHOOK_SECRET='replace-with-github-webhook-secret' python3 - <<'PY'
import hashlib
import hmac
import json
import os

secret = os.environ['GITHUB_WEBHOOK_SECRET'].encode()
payload = json.dumps({
    'repository': {'full_name': 'marcus937/riseos-agent-orchestrator'},
    'sender': {'login': 'agent'},
    'ref': 'refs/heads/agent-integration',
    'after': 'abc123',
}, separators=(',', ':')).encode()
print('signature=sha256=' + hmac.new(secret, payload, hashlib.sha256).hexdigest())
print(payload.decode())
PY
```

Send the request with the generated signature:

```bash
curl -i https://orchestrator.riseconnect.us/webhooks/github \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: push' \
  -H 'X-Hub-Signature-256: sha256=replace-with-generated-signature' \
  --data '{"repository":{"full_name":"marcus937/riseos-agent-orchestrator"},"sender":{"login":"agent"},"ref":"refs/heads/agent-integration","after":"abc123"}'
```

Expected dry-run response includes:

```json
{
  "status": "accepted",
  "event_accepted": true,
  "task_state": "review_needed",
  "repo": "marcus937/riseos-agent-orchestrator",
  "commit_sha": "abc123",
  "next_intended_action": "Build review prompt and prepare BB/Jarvis Architect review stub."
}
```

## 10. Operations

Restart after code or env changes:

```bash
sudo systemctl restart riseos-agent-orchestrator
sudo journalctl -u riseos-agent-orchestrator -n 100 --no-pager
```

Update deployment from `agent-integration`:

```bash
cd /opt/riseos-agent-orchestrator
sudo -u riseos git fetch origin
sudo -u riseos git checkout agent-integration
sudo -u riseos git pull --ff-only origin agent-integration
sudo -u riseos .venv/bin/pip install -e .
sudo systemctl restart riseos-agent-orchestrator
```

## Safety Notes

- Keep `ENABLE_OPENAI_REVIEW=false` unless live OpenAI review decision generation is intentionally enabled.
- If `ENABLE_OPENAI_REVIEW=true`, invalid model output becomes a `BLOCKED` dry-run decision and human approval remains required.
- Keep `ENABLE_BB_CONTEXT_PACK=true` unless OpenAI review prompts should omit BB Architect context.
- Keep `ENABLE_GITHUB_CONTEXT_HYDRATION=false` unless read-only GitHub context hydration is intentionally enabled.
- Keep `ENABLE_GITHUB_WRITEBACK=false` unless comment/label writeback is intentionally enabled.
- GitHub webhook writes remain disabled by default.
- No auto-merge behavior is part of this deployment.
- SQLite writes are limited to `/var/lib/riseos-agent-orchestrator/orchestrator.db`.
- NGINX exposes only `/health` and `/webhooks/github`; all other paths return 404.

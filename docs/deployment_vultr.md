# Vultr Deployment: RiseOS Agent Orchestrator

This guide deploys the FastAPI orchestrator at `https://orchestrator.riseconnect.us` on a Vultr server behind NGINX and HTTPS. The app remains dry-run safe: it accepts signed GitHub webhooks, identifies review-needed events, and returns a review stub without merging, changing branches, or writing production data.

## Assumptions

- DNS A record for `orchestrator.riseconnect.us` points to the Vultr server.
- Ubuntu 22.04 or 24.04 is installed.
- Deployment user is `riseos`.
- App path is `/opt/riseos-agent-orchestrator`.
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
ENABLE_OPENAI_REVIEW=false
WORK_BRANCH=agent-integration
BASE_BRANCH=main
```

Do not commit real values. `ENABLE_OPENAI_REVIEW=false` keeps the reviewer placeholder from making live OpenAI calls.

## 5. Install Systemd Service

```bash
cd /opt/riseos-agent-orchestrator
sudo cp deploy/riseos-agent-orchestrator.service /etc/systemd/system/riseos-agent-orchestrator.service
sudo systemctl daemon-reload
sudo systemctl enable --now riseos-agent-orchestrator
sudo systemctl status riseos-agent-orchestrator --no-pager
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

- The service must keep `ENABLE_OPENAI_REVIEW=false` until live reviewer calls are intentionally implemented.
- GitHub webhook writes remain disabled by default.
- No auto-merge behavior is part of this deployment.
- NGINX exposes only `/health` and `/webhooks/github`; all other paths return 404.

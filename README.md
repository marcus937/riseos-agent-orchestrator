# RiseOS Agent Orchestrator

Planning-first external automation layer for RiseOS coding agents.

This MVP accepts GitHub webhooks, verifies GitHub signatures, parses supported events, and provides placeholders for comment/label-only GitHub actions and OpenAI review decisions. It does not implement production secrets or auto-merge behavior.

## Guardrails

- No auto-merge behavior.
- No repository write actions beyond future comments and labels.
- Production secrets are not committed.
- GitHub write placeholders are limited to `post_issue_comment` and `apply_label`.

## Supported Events

- `issue_comment`
- `push`
- `pull_request`

## Environment Variables

| Variable | Required | Purpose |
|---|---:|---|
| `GITHUB_WEBHOOK_SECRET` | Yes | Shared secret used to verify `X-Hub-Signature-256`. |
| `GITHUB_TOKEN` | GitHub client | Token for read/review GitHub API calls. |
| `GITHUB_APP_ID` | Later | Placeholder for GitHub App authentication. |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Later | Placeholder path for GitHub App private key. |
| `OPENAI_API_KEY` | Later | Placeholder for reviewer integration. |
| `ENABLE_OPENAI_REVIEW` | No | Must be `true` before future OpenAI reviewer calls are allowed. |
| `APP_ENV` | No | Runtime environment label. Defaults to `local`. |

## GitHub Token Permissions

`GITHUB_TOKEN` should be scoped to the smallest permissions needed for the target repository. The client uses read access for commits and branch comparisons, plus issue/PR write access for comments and labels. It does not merge, delete branches, or write repository files.

Recommended fine-grained token permissions:

- Contents: read
- Metadata: read
- Issues: read/write for comments and labels
- Pull requests: read/write for PR comments and labels

## Local Run

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Run tests:

```bash
pytest
```

Start the dev server:

```bash
export GITHUB_WEBHOOK_SECRET='dev-secret'
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://localhost:8000/health
```

## Signed Webhook Test

```bash
python - <<'PY'
import hashlib, hmac, json
secret = b'dev-secret'
payload = json.dumps({
    'action': 'created',
    'repository': {'full_name': 'riseos/example'},
    'sender': {'login': 'marcus'},
    'issue': {'number': 1},
}).encode()
print('sha256=' + hmac.new(secret, payload, hashlib.sha256).hexdigest())
print(payload.decode())
PY
```

Send the payload with `X-GitHub-Event: issue_comment` and the generated `X-Hub-Signature-256` header.

## Deployment Notes

Deploy behind HTTPS. Configure the GitHub webhook secret in the hosting secret manager. Point GitHub webhooks at `POST /webhooks/github`. Keep GitHub App credentials and OpenAI credentials in managed secrets only.

For the MVP, the service should only comment on issues/PRs or apply labels after future integrations are implemented. Human review remains required for merges.

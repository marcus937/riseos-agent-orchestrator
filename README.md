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
| `OPENAI_API_KEY` | OpenAI review | Required only when `ENABLE_OPENAI_REVIEW=true`. |
| `OPENAI_REVIEW_MODEL` | No | Model for BB/Jarvis Architect review decisions. Defaults to `gpt-5.5-thinking`. |
| `ENABLE_OPENAI_REVIEW` | No | Set to `true` to request validated OpenAI `ReviewDecision` JSON. Defaults to `false`. |
| `ENABLE_GITHUB_CONTEXT_HYDRATION` | No | Set to `true` to let dry-run processing fetch read-only commit or compare context from GitHub. Defaults to `false`. |
| `ENABLE_GITHUB_WRITEBACK` | No | Set to `true` to post dry-run review comments and labels. Defaults to `false`. |
| `APP_ENV` | No | Runtime environment label. Defaults to `local`. |
| `ORCHESTRATOR_DB_PATH` | No | SQLite path for persisted webhook events and review queue items. If unset or unavailable, the service uses in-memory state. |
| `ORCHESTRATOR_ADMIN_TOKEN` | Process endpoint | Required for `POST /debug/review-queue/{id}/process`. |
| `ORCHESTRATOR_MAX_REVIEW_ITEMS` | No | Max persisted review queue items. Defaults to `500`; oldest processed items may be pruned. |
| `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS` | No | Set to `true` to require `X-Orchestrator-Admin-Token` on all `/debug/*` endpoints. Defaults to `false`. |

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

Optional local SQLite persistence:

```bash
export ORCHESTRATOR_DB_PATH="$PWD/.local/orchestrator.db"
mkdir -p "$(dirname "$ORCHESTRATOR_DB_PATH")"
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


## Webhook Dry-Run Review Behavior

The webhook endpoint accepts supported GitHub events and returns a dry-run review stub when review is needed. It does not call GitHub live, merge, write files, or change branches.

Review-needed triggers:

- `push` to `refs/heads/agent-integration` returns `task_state: review_needed` with the pushed commit SHA.
- `issue_comment` containing `Status: Done` returns `task_state: review_needed` with the issue number.
- `pull_request` with action `opened` or `synchronize` returns `task_state: review_needed` with the PR number and head SHA.

Example dry-run response shape:

```json
{
  "status": "accepted",
  "event_accepted": true,
  "event_type": "push",
  "repository": "marcus937/riseos-agent-orchestrator",
  "repo": "marcus937/riseos-agent-orchestrator",
  "task_state": "review_needed",
  "commit_sha": "abc123",
  "review_context": {
    "repo": "marcus937/riseos-agent-orchestrator",
    "commit_sha": "abc123",
    "event_type": "push",
    "trigger": "push_agent_integration"
  },
  "next_intended_action": "Build review prompt and prepare BB/Jarvis Architect review stub."
}
```

## Debug Review Queue

Review-needed events create `ReviewWorkItem` records. This queue is dry-run safe by default. It does not call OpenAI, comment on GitHub, apply labels, mutate repositories, or merge anything unless the relevant feature flags are explicitly enabled. Merge, branch, and repository file writes remain out of scope.

When `ORCHESTRATOR_DB_PATH` is set and writable, webhook events and review queue items are also stored in SQLite and reload after service restart. The app creates the DB directory and tables at startup and does not delete existing rows. If the DB path is unset or cannot initialize, the service falls back to in-memory state.

List pending and processed work items:

```bash
curl http://localhost:8000/debug/review-queue
```

Protected read mode:

```bash
curl http://localhost:8000/debug/review-queue \
  -H "X-Orchestrator-Admin-Token: $ORCHESTRATOR_ADMIN_TOKEN"
```

Inspect one work item:

```bash
curl http://localhost:8000/debug/review-queue/<work-item-id>
```

Process one work item in dry-run mode:

```bash
curl -X POST http://localhost:8000/debug/review-queue/<work-item-id>/process \
  -H "X-Orchestrator-Admin-Token: $ORCHESTRATOR_ADMIN_TOKEN"
```

The processor temporarily moves `pending_review` items to `reviewing`, then sets a final dry-run status. Missing `repo_full_name`, missing both `commit_sha` and `pr_number`, or unsupported event types become `blocked`. Valid work items become `approved_for_human_review`.

Read-only debug endpoints are public by default for local testing. Set `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true` to require `X-Orchestrator-Admin-Token` for all `/debug/*` routes. The processing endpoint always requires `ORCHESTRATOR_ADMIN_TOKEN`. Duplicate pending queue items are suppressed for the same repo, event type, commit SHA, PR number, and issue number.

By default, processing does not call GitHub or OpenAI. To include read-only GitHub context in the dry-run response, set `ENABLE_GITHUB_CONTEXT_HYDRATION=true` and provide `GITHUB_TOKEN`. Commit work items fetch commit metadata. PR work items compare `BASE_BRANCH` to the work item branch when branch context is available. Hydration never comments, labels, mutates repositories, or merges.

OpenAI BB/Jarvis Architect review generation is disabled by default. When `ENABLE_OPENAI_REVIEW=true`, `OPENAI_API_KEY` is required and the processor asks `OPENAI_REVIEW_MODEL` for structured JSON matching `ReviewDecision`. The prompt includes the work item, changed files, diff summary, hydrated GitHub context, branch policy, no-auto-merge policy, and the human approval boundary. Invalid or unvalidated model output becomes a `BLOCKED` dry-run decision with `openai_review_error`.

GitHub writeback is also disabled by default. When `ENABLE_GITHUB_WRITEBACK=true`, processing may call only `post_issue_comment` and `apply_label`. The comment target is the PR number when present, otherwise the issue number. If no issue or PR number is available, writeback is skipped and `github_writeback_error` explains why. Labels map to the structured decision: `agent-approved-human-review`, `agent-needs-changes`, `agent-blocked`, or `agent-escalate-marcus`.

Example process response:

```json
{
  "work_item": {
    "id": "generated-uuid",
    "created_at": "2026-06-02T19:30:00Z",
    "repo_full_name": "riseos/example",
    "event_type": "push",
    "branch": "agent-integration",
    "commit_sha": "abc123",
    "issue_number": null,
    "pr_number": null,
    "status": "approved_for_human_review"
  },
  "decision": {
    "decision": "APPROVED_FOR_HUMAN_REVIEW",
    "confidence": 1.0,
    "risk_level": "LOW",
    "summary": "Dry-run review processor accepted this work item for human review.",
    "required_changes": [],
    "next_task_prompt": null,
    "human_review_required": true
  },
  "intended_next_actions": [
    "Send the dry-run decision to BB/Jarvis Architect for human review.",
    "Do not merge automatically."
  ],
  "changed_files": [
    "app/main.py",
    "tests/test_webhooks.py"
  ],
  "diff_summary": "commit abc123: 2 changed file(s), +16/-1.",
  "github_context_available": true,
  "github_context_error": null,
  "github_writeback_attempted": false,
  "github_writeback_success": false,
  "github_writeback_error": null,
  "openai_review_attempted": false,
  "openai_review_success": false,
  "openai_review_error": null,
  "reviewer_model": null,
  "dry_run": true
}
```

Example OpenAI review decision response when `ENABLE_OPENAI_REVIEW=true` and the model output validates:

```json
{
  "decision": {
    "decision": "NEEDS_CHANGES",
    "confidence": 0.86,
    "risk_level": "MEDIUM",
    "summary": "One test is missing.",
    "required_changes": ["Add coverage for the processor."],
    "next_task_prompt": "Add the missing processor test.",
    "human_review_required": true
  },
  "openai_review_attempted": true,
  "openai_review_success": true,
  "openai_review_error": null,
  "reviewer_model": "gpt-5.5-thinking",
  "dry_run": true
}
```

Example writeback comment body:

```markdown
## Review Decision
APPROVED_FOR_HUMAN_REVIEW

## Risk Level
LOW

## Summary
Dry-run review processor accepted this work item for human review.

## Required Changes
- None

## Changed Files
- app/main.py

## Diff Summary
commit abc123: 1 changed file(s), +4/-1.

## Dry-run Status
approved_for_human_review

## Human Review Required
True
```

## Deployment Notes

Deploy behind HTTPS. Configure the GitHub webhook secret in the hosting secret manager. Point GitHub webhooks at `POST /webhooks/github`. Keep GitHub App credentials and OpenAI credentials in managed secrets only.

For the MVP, enabled GitHub writeback may only comment on issues/PRs or apply labels. Human review remains required for merges.

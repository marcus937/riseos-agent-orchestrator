# RiseOS Agent Orchestrator

Planning-first external automation layer for RiseOS coding agents.

The orchestrator accepts GitHub webhooks, verifies GitHub signatures, persists review queue items, hydrates read-only GitHub context, can request BB/Jarvis Architect review decisions from OpenAI, can optionally write review comments and labels back to GitHub, and can notify Slack when approved `agent-ready` issues are queued. It does not implement auto-merge behavior, branch mutation, deploy behavior, or repository file writes.

## Guardrails

- No auto-merge behavior.
- No deploy behavior.
- No branch mutation.
- No repository file writes.
- No issue closing by the orchestrator.
- Production secrets are not committed.
- GitHub writes are limited to comments and labels when explicitly enabled.
- Slack dispatch is notification-only.
- Human approval remains required before merge.

## Supported Events

- `issue_comment`
- `issues`
- `push`
- `pull_request`

## Environment Variables

| Variable | Required | Purpose |
|---|---:|---|
| `GITHUB_WEBHOOK_SECRET` | Yes | Shared secret used to verify `X-Hub-Signature-256`. |
| `GITHUB_TOKEN` | GitHub client | Token for read/review GitHub API calls and optional comment/label writeback. |
| `GITHUB_APP_ID` | Later | Placeholder for GitHub App authentication. |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Later | Placeholder path for GitHub App private key. |
| `OPENAI_API_KEY` | OpenAI review | Required only when `ENABLE_OPENAI_REVIEW=true`. |
| `OPENAI_REVIEW_MODEL` | No | Model for BB/Jarvis Architect review decisions. Defaults to `gpt-5.5-thinking`. |
| `ENABLE_OPENAI_REVIEW` | No | Set to `true` to request validated OpenAI `ReviewDecision` JSON. Defaults to `false`. |
| `ENABLE_BB_CONTEXT_PACK` | No | Set to `false` to omit BB Architect context packs from OpenAI review prompts. Defaults to `true`. |
| `BB_CONTEXT_MAX_CHARS` | No | Maximum BB context pack characters included in the prompt. Defaults to `20000`. |
| `ENABLE_GITHUB_CONTEXT_HYDRATION` | No | Set to `true` to fetch read-only commit or compare context from GitHub. Defaults to `false`. |
| `ENABLE_GITHUB_WRITEBACK` | No | Set to `true` to post review comments and labels. Defaults to `false`. |
| `ENABLE_TASK_DISPATCH` | No | Set to `true` to let approved BB2 reviews assign the next queued GitHub Issue task. Requires `ENABLE_GITHUB_WRITEBACK=true`. Defaults to `false`. |
| `SLACK_WEBHOOK_URL` | Slack dispatch | Incoming webhook URL for Slack issue notifications. If unset, `SLACK_BOT_TOKEN` can be used instead. |
| `SLACK_BOT_TOKEN` | Slack dispatch | Slack bot token used with `chat.postMessage` when no webhook URL is configured. |
| `SLACK_CHANNEL` | No | Slack channel for Circuit notifications. Defaults to `#project_riseos`. |
| `APP_ENV` | No | Runtime environment label. Defaults to `local`. |
| `ORCHESTRATOR_DB_PATH` | No | SQLite path for persisted webhook events and review queue items. If unset or unavailable, the service uses in-memory state. |
| `ORCHESTRATOR_ADMIN_TOKEN` | Process endpoint | Required for `POST /debug/review-queue/{id}/process`. |
| `ORCHESTRATOR_MAX_REVIEW_ITEMS` | No | Max persisted review queue items. Defaults to `500`; oldest processed items may be pruned. |
| `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS` | No | Set to `true` to require `X-Orchestrator-Admin-Token` on all `/debug/*` endpoints. Defaults to `false`. |

## GitHub Token Permissions

`GITHUB_TOKEN` should be scoped to the smallest permissions needed for the target repository. The client uses read access for commits, branch comparisons, and open issues, plus issue/PR write access for comments and labels. It does not merge, delete branches, mutate refs, close issues, or write repository files.

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

## Webhook Dry-Run Review Behavior

The webhook endpoint accepts supported GitHub events and returns a dry-run review stub when review is needed. It does not call GitHub live, merge, deploy, write files, or change branches.

Review-needed triggers:

- `push` to `refs/heads/agent-integration` returns `task_state: review_needed` with the pushed commit SHA.
- `issue_comment` containing `Status: Done` returns `task_state: review_needed` with the issue number.
- `pull_request` with action `opened` or `synchronize` returns `task_state: review_needed` with the PR number and head SHA.

Review-needed events create `ReviewWorkItem` records. This queue is dry-run safe by default. It does not call OpenAI, comment on GitHub, apply labels, dispatch tasks, mutate repositories, deploy, or merge anything unless the relevant feature flags are explicitly enabled.

## GitHub Issue To Slack Circuit Dispatcher

The `issues` webhook can notify Slack when a Circuit-ready issue enters the queue. The HMAC signature is verified with the same `GITHUB_WEBHOOK_SECRET` logic before any Slack dispatch is considered.

Dispatch rules:

- Event must be `issues` with action `opened` or `labeled`.
- Repository must exactly match an approved full name: `marcus937/Project-Jarvis`, `marcus937/jarvis-mission-control`, `marcus937/riseos-agent-orchestrator`, or `marcus937/Rylinn-Field-App-Codex`.
- Issue must be open.
- Issue must have label `agent-ready`.
- For `labeled` actions, the added label must be `agent-ready`.
- Already-dispatched issue IDs are deduplicated in process memory.
- Slack message posts to `SLACK_CHANNEL`, defaulting to `#project_riseos`.

Slack messages mention `@circuit-forge` and include repo, issue number, title, labels, URL, branch rule, and no-merge/no-deploy reminder. User-controlled issue fields are escaped before posting so Slack control sequences such as channel-wide or user mentions are rendered as text.

This dispatcher is notification-only. It does not close issues, mutate branches, open PRs, merge, deploy, or write repository files.

Follow-up recommendation: Priority 3A Persistent Dispatch Registry should move deduplication from process memory to persisted storage so service restarts cannot re-notify the same issue.

## Debug Review Queue

List pending and processed work items:

```bash
curl http://localhost:8000/debug/review-queue
```

Process one work item in dry-run mode:

```bash
curl -X POST http://localhost:8000/debug/review-queue/<work-item-id>/process \
  -H "X-Orchestrator-Admin-Token: $ORCHESTRATOR_ADMIN_TOKEN"
```

Read-only debug endpoints are public by default for local testing. Set `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true` to require `X-Orchestrator-Admin-Token` for all `/debug/*` routes. The processing endpoint always requires `ORCHESTRATOR_ADMIN_TOKEN`. Duplicate pending queue items are suppressed for the same repo, event type, commit SHA, PR number, and issue number.

By default, processing does not call GitHub or OpenAI. To include read-only GitHub context in the dry-run response, set `ENABLE_GITHUB_CONTEXT_HYDRATION=true` and provide `GITHUB_TOKEN`. Commit work items fetch commit metadata. PR work items compare `BASE_BRANCH` to the work item branch when branch context is available. Hydration never comments, labels, mutates repositories, or merges.

OpenAI BB/Jarvis Architect review generation is disabled by default. When `ENABLE_OPENAI_REVIEW=true`, `OPENAI_API_KEY` is required and the processor asks `OPENAI_REVIEW_MODEL` for structured JSON matching `ReviewDecision`. The prompt includes the BB Architect context pack when `ENABLE_BB_CONTEXT_PACK=true`, plus the work item, changed files, diff summary, diff patches, hydrated GitHub context, branch policy, no-auto-merge policy, and the human approval boundary. `BB_CONTEXT_MAX_CHARS` bounds the included context pack. Set `ENABLE_BB_CONTEXT_PACK=false` to preserve the previous prompt shape without BB context. Invalid or unvalidated model output becomes a `BLOCKED` dry-run decision with `openai_review_error`.

GitHub writeback is disabled by default. When `ENABLE_GITHUB_WRITEBACK=true`, processing may call only `post_issue_comment` and `apply_label`. The comment target is the PR number when present, otherwise the issue number. If no issue or PR number is available, writeback is skipped and `github_writeback_error` explains why. Labels map to the structured decision:

| Decision | Label |
|---|---|
| `APPROVED_FOR_HUMAN_REVIEW` | `bb2-approved` |
| `NEEDS_CHANGES` | `bb2-needs-changes` |
| `BLOCKED` | `bb2-blocked` |
| `ESCALATE_TO_MARCUS` | `bb2-blocked` |

## GitHub Issue Task Dispatch

`ENABLE_TASK_DISPATCH=false` by default. When `ENABLE_GITHUB_WRITEBACK=true` and `ENABLE_TASK_DISPATCH=true`, an approved BB2 review may dispatch the next GitHub Issue task for Circuit in the same repository.

Queued issues must be open and have both labels:

- `agent-task`
- `agent-ready`

Issues with `bb2-blocked` are skipped. The selector chooses the oldest created eligible issue first. Dispatch posts a Circuit assignment comment and applies `agent-next`. It does not close issues, merge, mutate branches, open PRs, or write repository files.

Task labels:

- `agent-task`
- `agent-ready`
- `agent-working`
- `bb2-review-needed`
- `bb2-approved`
- `bb2-needs-changes`
- `bb2-blocked`
- `agent-next`

Example assignment comment:

```markdown
## Circuit Assignment

Issue: #42 - Implement queue metrics

Branch: `agent-integration` only.

Reminders:
- Stay on `agent-integration`.
- Comment `Status: Done` with the completed commit SHA when finished.
- Do not merge.
- Do not open a PR unless explicitly requested.
- Do not mutate branches.

Task summary:
Add queue metrics for review processing.
```

The process response includes task-dispatch fields:

```json
{
  "task_dispatch_attempted": true,
  "task_dispatch_success": true,
  "task_dispatch_issue_number": 42,
  "task_dispatch_error": null
}
```

If no eligible issue is found, `task_dispatch_error` is `No queued agent-ready issue found`.

## Deployment Notes

Deploy behind HTTPS. Configure the GitHub webhook secret in the hosting secret manager. Point GitHub webhooks at `POST /webhooks/github`. Keep GitHub App credentials, OpenAI credentials, and Slack credentials in managed secrets only.

For the MVP, enabled GitHub writeback and task dispatch may only comment on issues/PRs or apply labels. Slack dispatch may only post notifications. Human review remains required for merges.

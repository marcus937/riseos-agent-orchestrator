# Circuit GitHub-to-Slack Issue Dispatcher Plan

Date: 2026-06-03
Repo: `marcus937/riseos-agent-orchestrator`
Target branch: `agent-integration`
Status: docs-first implementation plan only

## Objective

Build a GitHub webhook-driven dispatcher that routes eligible GitHub Issues to Circuit in Slack.

When an issue is opened or labeled `agent-ready` in an approved Jarvis repository, or when an issue comment explicitly mentions `@circuit-forge`, the orchestrator should post a structured Slack message that mentions Circuit and gives enough context for the agent to decide whether to begin work.

This dispatcher must not start coding automatically unless routing rules pass. It must not deploy, merge, mutate branches, open PRs, or write repository files.

## Approved Repositories

Only these repositories are eligible for dispatch:

- `marcus937/Project-Jarvis`
- `marcus937/jarvis-mission-control`
- `marcus937/riseos-agent-orchestrator`

Any webhook event from another repository must be accepted as non-actionable or ignored without Slack dispatch.

## Existing Orchestrator Fit

The repository already has a FastAPI webhook surface at `POST /webhooks/github`, GitHub HMAC signature verification, persisted event/review queue options, and planning-first guardrails.

The dispatcher should extend that model instead of becoming a separate service:

- keep GitHub webhook verification in the existing request path
- parse supported issue events into a dedicated dispatch decision
- persist dispatch state through the existing SQLite-backed storage layer or a small adjacent table
- keep Slack posting behind explicit env flags
- return dry-run-safe responses when dispatch is disabled

## Event Scope

### GitHub Event: `issues`

Supported actions:

- `opened`
- `labeled`
- optionally `reopened` if the issue already has `agent-ready`

Routing rules:

- repo must be approved
- issue must be open
- issue must not be a pull request
- dispatch only when:
  - action is `opened` and labels include `agent-ready`, or
  - action is `labeled` and `label.name == "agent-ready"`

Non-actionable cases:

- wrong repo
- missing issue number/title
- pull request masquerading as issue
- issue is closed
- issue lacks `agent-ready`
- label event is not for `agent-ready`

### GitHub Event: `issue_comment`

Supported action:

- `created`

Routing rules:

- repo must be approved
- issue must be open
- issue must not be a pull request unless explicitly approved later
- comment body must contain explicit `@circuit-forge`

Optional follow-up behavior:

- If the issue also has `agent-ready`, use normal task dispatch wording.
- If the issue does not have `agent-ready`, route as an explicit mention but mark `required_next_action` as `triage_only` unless the comment says to proceed.

Non-actionable cases:

- wrong repo
- missing comment body
- comment does not mention `@circuit-forge`
- pull request comment unless PR routing is added later
- duplicate mention already dispatched for the same issue/comment

## Dispatch Decision Model

Add an internal model such as `CircuitDispatchDecision`:

```python
class CircuitDispatchDecision(BaseModel):
    should_dispatch: bool
    reason: str
    trigger: Literal["issue_opened_agent_ready", "issue_labeled_agent_ready", "issue_comment_mention"] | None
    repo_full_name: str | None
    issue_number: int | None
    issue_title: str | None
    issue_url: str | None
    labels: list[str]
    branch_policy: str = "agent-integration only"
    required_next_action: str | None
    dedupe_key: str | None
```

Recommended `required_next_action` values:

- `inspect_issue_and_begin_if_requirements_are_clear`
- `triage_mention_before_work`
- `ignore_not_eligible`

## Duplicate Suppression

Duplicate dispatch prevention is required because GitHub can redeliver webhooks and labels/comments can be reprocessed.

Recommended dedupe keys:

- `issues.opened`: `repo_full_name#issue_number#agent-ready`
- `issues.labeled`: `repo_full_name#issue_number#agent-ready`
- `issue_comment.created`: `repo_full_name#issue_number#comment_id#circuit-forge`

Storage should record:

- dedupe key
- repo full name
- issue number
- triggering event type
- triggering action
- GitHub delivery ID from `X-GitHub-Delivery`
- Slack channel ID
- Slack message timestamp if posting succeeded
- created timestamp
- result status: `posted`, `skipped_duplicate`, `failed`
- error text when applicable

Preferred table name:

```text
circuit_issue_dispatches
```

Minimum unique constraint:

```text
UNIQUE(dedupe_key)
```

If SQLite is unavailable and the app falls back to memory, duplicate suppression can be best-effort for local testing, but production must use durable storage.

## Slack Posting Contract

Post only to the configured channel. Do not infer the channel from GitHub payloads.

Required message fields:

- Circuit mention: configured Slack user or user group for `@circuit-forge`
- repository full name
- issue number and title
- issue URL
- labels
- trigger reason
- branch policy: `agent-integration only`
- required next action
- safety reminders

Suggested Slack message:

```text
<@CIRCUIT_SLACK_USER_ID> agent-ready issue dispatched

Repo: marcus937/jarvis-mission-control
Issue: #7 - Restore Mission Control debug panel access in Vercel/JMC agent staging
Labels: agent-ready, jmc, debug, frontend
Trigger: issue_labeled_agent_ready
Branch policy: agent-integration only
Required next action: inspect_issue_and_begin_if_requirements_are_clear

Guardrails:
- Do not merge.
- Do not open a PR unless explicitly asked.
- Commit only to agent-integration.
- Comment Status: Done with commit SHA when complete.
```

Slack send failures should not cause GitHub webhook retry storms unless the failure is clearly transient and retry-safe. Prefer recording failure state and returning a successful webhook response with a debug-visible error.

## Environment Variables

Existing variables to reuse:

| Variable | Required | Purpose |
|---|---:|---|
| `GITHUB_WEBHOOK_SECRET` | Yes | Verify `X-Hub-Signature-256`. |
| `ORCHESTRATOR_DB_PATH` | Production | Durable dispatch dedupe storage. |
| `APP_ENV` | No | Runtime label for logs/debug output. |
| `ORCHESTRATOR_ADMIN_TOKEN` | Debug/process endpoints | Existing admin token. |
| `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS` | Recommended | Protect debug endpoints when deployed. |

New variables to add:

| Variable | Required | Purpose |
|---|---:|---|
| `ENABLE_CIRCUIT_SLACK_DISPATCH` | Yes for posting | Master flag. Defaults to `false`. |
| `SLACK_BOT_TOKEN` | Yes for posting | Slack bot token with `chat:write` for the target channel. |
| `CIRCUIT_SLACK_CHANNEL_ID` | Yes for posting | Slack channel ID to receive dispatches. |
| `CIRCUIT_SLACK_MENTION` | Yes for posting | Exact mention token, for example `<@U...>` or `<!subteam^S...>`. |
| `CIRCUIT_APPROVED_REPOS` | No | Comma-separated override for approved repo allowlist. Defaults to the three approved repos in this plan. |
| `CIRCUIT_DISPATCH_LABEL` | No | Defaults to `agent-ready`. |
| `CIRCUIT_DISPATCH_MENTION` | No | Defaults to `@circuit-forge`. |
| `CIRCUIT_DISPATCH_DRY_RUN` | No | Defaults to `true` until staging validation is complete. |

## Security Requirements

### HMAC Verification

Every GitHub webhook must be verified before parsing or dispatching:

- read raw request body
- require `X-Hub-Signature-256`
- compute `sha256=` HMAC with `GITHUB_WEBHOOK_SECRET`
- compare with `hmac.compare_digest`
- reject invalid signatures with `401`

Never trust repository name, labels, issue URL, or sender fields before signature verification.

### Repository Allowlist

Dispatch only from approved repositories. The allowlist is a hard security boundary, not just a convenience filter.

### Slack Token Safety

- Store `SLACK_BOT_TOKEN` only in deployment secrets.
- Never log the token.
- Redact Slack API authorization headers from diagnostics.
- Restrict bot permissions to the minimum needed for posting.

### Mention Abuse

`@circuit-forge` should only trigger from approved repositories and valid signed GitHub payloads. Do not let arbitrary webhook senders or unauthenticated HTTP requests mention Circuit in Slack.

### Duplicate and Replay Risk

GitHub redelivery can replay old payloads. Dedupe by event semantics, not only delivery ID. Keep `X-GitHub-Delivery` for audit, but do not rely on it as the only replay protection.

### Auto-Work Boundary

The dispatcher posts a structured Slack request. It must not directly run code, checkout repositories, open PRs, merge, close issues, or mutate branches.

Circuit may begin work only after the Slack-triggered agent route receives the message and its own routing/safety rules pass.

## Implementation Steps

1. Extend event parsing to support `issues` while preserving existing `issue_comment`, `push`, and `pull_request` behavior.
2. Add `CircuitDispatchDecision` and pure routing logic with no Slack side effects.
3. Add approved repo, label, and mention configuration to `Settings`.
4. Add a `SlackClient` wrapper with one method: `post_message(channel_id, text)`.
5. Add durable `circuit_issue_dispatches` storage with dedupe lookup and insert/update result status.
6. Wire the dispatcher after webhook signature verification and payload parsing.
7. Keep dispatch disabled by default with `ENABLE_CIRCUIT_SLACK_DISPATCH=false` and `CIRCUIT_DISPATCH_DRY_RUN=true`.
8. Add debug visibility for recent dispatch decisions and failures, protected by existing debug-read policy.
9. Add tests for routing, signature behavior, duplicate suppression, and Slack dry-run/post failure behavior.
10. Validate in staging only. Do not production deploy from this task.

## Suggested Module Boundaries

```text
app/circuit_dispatch.py       # pure decision logic
app/slack_client.py           # Slack API wrapper
app/dispatch_store.py         # durable dedupe/result storage if not folded into storage.py
app/config.py                 # new env vars
app/github_events.py          # issues event parser extension
app/main.py                   # webhook wiring only
```

Keep side effects out of pure routing tests. The decision function should be testable with plain payload dictionaries.

## Test Plan

### Unit Tests

- valid `issues.opened` with `agent-ready` in approved repo dispatches
- valid `issues.labeled` where `label.name == agent-ready` dispatches
- `issues.labeled` for any other label does not dispatch
- `issues.opened` without `agent-ready` does not dispatch
- wrong repo does not dispatch
- closed issue does not dispatch
- pull request payload does not dispatch
- valid `issue_comment.created` with `@circuit-forge` dispatches
- `issue_comment.created` without mention does not dispatch
- duplicate issue dedupe key is skipped
- duplicate comment dedupe key is skipped
- message formatting includes repo, issue number, title, labels, branch policy, and required next action

### Webhook Tests

- unsigned payload rejected before routing
- invalid signature rejected before routing
- valid signature accepted
- GitHub `issues` event is parsed correctly
- unsupported GitHub event remains non-actionable
- dry-run mode records decision without Slack post
- disabled dispatcher records no Slack post

### Slack Client Tests

- posts to configured channel with configured mention
- redacts token in logs/errors
- handles Slack API non-200 response as recorded dispatch failure
- handles network timeout as recorded dispatch failure

### Storage Tests

- inserting the same dedupe key twice yields one posted dispatch and one skipped duplicate
- records GitHub delivery ID and Slack timestamp when available
- persists across app restart when SQLite path is configured

### Manual Staging Test

1. Configure staging secrets with dry run enabled.
2. Send a signed fixture for `issues.opened` with `agent-ready` from an approved repo.
3. Confirm debug dispatch decision is `should_dispatch=true` and no Slack post occurs.
4. Disable dry run in staging.
5. Redeliver the same fixture once and confirm only one Slack message is posted.
6. Create or label a real test issue in `marcus937/riseos-agent-orchestrator` with `agent-ready`.
7. Confirm Slack receives the structured Circuit mention.
8. Confirm Circuit does not start coding unless Slack routing rules pass.
9. Remove test labels or close the test issue manually.

## Deployment Plan

No production deploy for this task.

Future rollout should be:

1. merge only after human review
2. deploy to staging with `CIRCUIT_DISPATCH_DRY_RUN=true`
3. validate signed fixture behavior
4. enable Slack posting in staging
5. validate duplicate suppression
6. review logs and debug endpoints
7. get BB approval before any production enablement

## Open Questions for BB

- Should `@circuit-forge` in issue comments dispatch even without `agent-ready`, or should it only notify Circuit for triage?
- Should PR comments ever dispatch to Circuit, or should this remain issues-only for now?
- Which Slack mention should production use: direct Circuit user ID or the Circuit user group mention?
- Should dispatch add or remove GitHub labels such as `agent-working`, or should it remain Slack-only?

## Acceptance Criteria

- The implementation plan is documented at `docs/circuit_issue_dispatcher_2026-06.md`.
- The plan keeps the dispatcher disabled by default.
- The plan routes only approved repos, `agent-ready`, and explicit `@circuit-forge` mentions.
- The plan includes HMAC verification, duplicate suppression, durable dispatch storage, Slack env vars, security risks, and a test plan.
- No runtime code is changed by this docs-first task.
- No production deploy, merge, or PR is performed.

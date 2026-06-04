# BB2 Architect Escalation Dispatcher Plan

Date: 2026-06-03
Repo: `marcus937/riseos-agent-orchestrator`
Target branch: `agent-integration`
Status: docs-first implementation plan only

## Objective

Extend the Circuit dispatcher so BB2 can escalate high-risk reviews to Marcus/BB from GitHub issue and pull request comments.

When BB2 comments on a GitHub issue or pull request with `ARCHITECT_REVIEW_REQUIRED` or another configured high-risk keyword, the orchestrator should:

1. verify the GitHub webhook signature,
2. confirm the comment author is BB2,
3. confirm the repository is approved,
4. add the GitHub label `bb-review-needed`, and
5. post a structured Slack message to `<#C0AR0HMTM28>`.

This is a planning document only. No runtime code, deploy, merge, or PR is part of this task.

## Scope

In scope:

- GitHub `issue_comment` events on issues and pull requests
- BB2-authored review escalation comments
- high-risk keyword detection
- label writeback: `bb-review-needed`
- Slack notification to `<#C0AR0HMTM28>`
- duplicate suppression
- environment variables
- security risks
- test plan

Out of scope:

- auto-merge
- branch mutation
- repository file writes
- production deployment
- PR creation
- closing issues or PRs
- automatic code changes by the dispatcher

## Approved Repositories

Use the same approved repository boundary as the Circuit issue dispatcher unless BB approves an override:

- `marcus937/Project-Jarvis`
- `marcus937/jarvis-mission-control`
- `marcus937/riseos-agent-orchestrator`

Events from any other repository must be ignored or recorded as non-actionable without Slack posting or label mutation.

## Trigger Events

### GitHub Event: `issue_comment`

Supported action:

- `created`

GitHub uses `issue_comment` for both issue comments and pull request conversation comments. The dispatcher should support both because architect escalations can happen on either task issues or PR review threads.

### Future Event: `pull_request_review_comment`

Optional later extension:

- support inline PR review comments if BB2 begins using inline review comments for escalation
- use the same keyword, author, repository, dedupe, label, and Slack rules

Do not include this in the first implementation unless explicitly requested.

## Routing Rules

A comment is eligible only when all routing checks pass:

1. Webhook signature is valid.
2. Event is `issue_comment` with action `created`.
3. Repository is in the approved allowlist.
4. Comment author matches a configured BB2 GitHub login.
5. Comment body contains one of the configured escalation triggers.
6. Issue or PR number is present.
7. The issue or PR is open, unless BB explicitly wants closed-item escalations.
8. Dedupe key has not already been dispatched.

If any check fails, do not label and do not post to Slack.

## Escalation Triggers

Primary explicit trigger:

```text
ARCHITECT_REVIEW_REQUIRED
```

Recommended default high-risk keywords:

```text
ARCHITECT_REVIEW_REQUIRED
SECURITY_RISK
DATA_LOSS_RISK
PRODUCTION_RISK
SECRETS_RISK
AUTH_RISK
MIGRATION_RISK
SCHEMA_CONTRACT_RISK
BACKEND_FRONTEND_CONTRACT_RISK
INFRASTRUCTURE_RISK
NEEDS_ARCHITECT
ESCALATE_TO_BB
ESCALATE_TO_MARCUS
```

Matching policy:

- case-insensitive
- match whole tokens where practical
- preserve the exact matched keyword in the escalation record
- allow configuration override through env vars

Do not trigger on ordinary prose such as `this might be risky` unless BB2 uses one of the configured keywords. The goal is deliberate escalation, not noisy sentiment detection.

## BB2 Identity Verification

The dispatcher must verify the comment author before treating the comment as an escalation.

Recommended env var:

```text
BB2_GITHUB_LOGINS=bb2-login-1,bb2-login-2
```

Routing should compare `comment.user.login` against this allowlist.

If GitHub App bot identity is used for BB2, configure that bot login explicitly. Do not infer BB2 identity from display names or comment text.

## GitHub Label Behavior

Add this label when escalation passes routing:

```text
bb-review-needed
```

Rules:

- add the label to the issue or pull request conversation item
- do not remove existing labels
- do not close the issue or PR
- do not apply approval labels such as `bb2-approved`
- do not apply blocked labels unless a separate BB2 decision path already does that
- skip label writeback when GitHub writeback is disabled, but still record that label writeback was skipped

If the label already exists, treat labeling as successful and continue to Slack dispatch.

## Slack Message Format

Post to `<#C0AR0HMTM28>` using the configured channel ID.

Recommended message:

```text
*BB2 architect review escalation*

Repo: marcus937/jarvis-mission-control
Item: PR #42 - Restore Mission Control debug access
URL: https://github.com/marcus937/jarvis-mission-control/pull/42
Author: bb2-bot
Matched trigger: ARCHITECT_REVIEW_REQUIRED
Labels added: bb-review-needed
Risk area: backend_frontend_contract

BB2 note:
<short sanitized excerpt of the BB2 comment>

Required next action:
Marcus/BB architect review requested before Circuit continues or before human merge review.

Guardrails:
- No merge from orchestrator.
- No branch mutation from orchestrator.
- No production deploy from this escalation.
```

Message fields:

- heading: `BB2 architect review escalation`
- repo full name
- issue or PR number
- title
- GitHub URL
- comment author
- matched trigger keyword
- current labels or label added
- inferred risk area if mapped from keyword
- short BB2 comment excerpt
- required next action
- guardrails

Mentioning Marcus or BB directly should be configurable. The default plan should post to the channel without hardcoding a user mention unless BB requests one.

## Dedupe Behavior

Duplicate suppression is required because GitHub may redeliver webhooks and BB2 may edit/resend similar review comments.

Recommended dedupe key:

```text
repo_full_name#issue_or_pr_number#comment_id#architect_escalation
```

Storage should record:

- dedupe key
- repository full name
- issue or PR number
- item type: `issue` or `pull_request`
- comment ID
- comment URL
- comment author
- matched trigger
- GitHub delivery ID from `X-GitHub-Delivery`
- whether `bb-review-needed` label was added
- Slack channel ID
- Slack message timestamp if posted
- result status: `posted`, `skipped_duplicate`, `label_failed`, `slack_failed`, `dry_run`
- created timestamp
- error text when applicable

Preferred table name:

```text
bb2_architect_escalations
```

Minimum unique constraint:

```text
UNIQUE(dedupe_key)
```

If the same comment contains multiple high-risk keywords, create one escalation record and include all matched keywords in the stored metadata and Slack message.

## Environment Variables

Existing variables to reuse:

| Variable | Required | Purpose |
|---|---:|---|
| `GITHUB_WEBHOOK_SECRET` | Yes | Verify `X-Hub-Signature-256`. |
| `GITHUB_TOKEN` | Required for label writeback | Add `bb-review-needed` to issues/PRs. |
| `ENABLE_GITHUB_WRITEBACK` | Required for label writeback | Existing master flag for GitHub comments/labels. |
| `ORCHESTRATOR_DB_PATH` | Production | Durable dedupe and audit storage. |
| `ORCHESTRATOR_ADMIN_TOKEN` | Debug endpoints | Existing admin token. |
| `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS` | Recommended | Protect debug endpoints when deployed. |

New variables to add:

| Variable | Required | Purpose |
|---|---:|---|
| `ENABLE_BB2_ARCHITECT_ESCALATION` | Yes | Master flag. Defaults to `false`. |
| `BB2_GITHUB_LOGINS` | Yes | Comma-separated GitHub logins allowed to trigger escalation. |
| `BB2_ARCHITECT_ESCALATION_KEYWORDS` | No | Comma-separated trigger override. Defaults to the keyword list in this plan. |
| `BB2_ARCHITECT_ESCALATION_LABEL` | No | Defaults to `bb-review-needed`. |
| `BB2_ARCHITECT_SLACK_CHANNEL_ID` | Yes for posting | Defaults operationally to `C0AR0HMTM28` when configured. |
| `SLACK_BOT_TOKEN` | Yes for posting | Slack bot token with `chat:write` for the channel. |
| `BB2_ARCHITECT_SLACK_MENTION` | No | Optional Marcus/BB mention token to prepend. Empty by default. |
| `BB2_ARCHITECT_ESCALATION_DRY_RUN` | No | Defaults to `true` until staging validation is complete. |
| `BB2_ARCHITECT_COMMENT_EXCERPT_CHARS` | No | Defaults to `500`. Bounds Slack excerpt length. |

## Security Risks

### Spoofed Escalation Comments

Risk: a non-BB2 user writes `ARCHITECT_REVIEW_REQUIRED` to force noisy Slack escalation.

Mitigation:

- require signed GitHub webhook
- require approved repository
- require `comment.user.login` in `BB2_GITHUB_LOGINS`
- never trust text alone as identity

### Slack Spam or Redelivery Floods

Risk: GitHub redelivery or repeated comments post duplicate Slack alerts.

Mitigation:

- durable dedupe table keyed by repo, issue/PR number, comment ID, escalation type
- return successful webhook response after recording duplicate skip
- record Slack timestamp for audit

### Token Leakage

Risk: Slack or GitHub tokens appear in logs or debug output.

Mitigation:

- never log token values
- redact Authorization headers
- keep secrets in deployment secret manager
- avoid dumping full request headers in diagnostics

### Overbroad GitHub Writes

Risk: escalation code mutates more labels or state than intended.

Mitigation:

- limit writeback to adding `bb-review-needed`
- use existing GitHub writeback guardrails
- do not remove labels
- do not close issues or PRs
- do not mutate refs or repository contents

### Comment Content Exposure

Risk: BB2 comments may include sensitive context that should not be broadly reposted.

Mitigation:

- post a bounded excerpt, not the full comment by default
- preserve GitHub URL for full context
- optionally redact obvious secret patterns in Slack excerpt
- keep Slack channel fixed to the approved RiseOS project channel

### False Positives

Risk: high-risk keywords appear in quoted examples or old copied text.

Mitigation:

- prefer explicit `ARCHITECT_REVIEW_REQUIRED`
- keep broader high-risk keywords configurable
- include matched keyword in Slack message so humans can judge quickly

## Implementation Steps

1. Extend the dispatcher planning model with `BB2ArchitectEscalationDecision`.
2. Add settings for BB2 logins, trigger keywords, Slack channel, optional mention, label name, dry-run flag, and excerpt limit.
3. Add pure routing logic for `issue_comment.created` payloads.
4. Add helper to classify item type as issue vs pull request from the GitHub payload.
5. Add durable dedupe storage for architect escalations.
6. Add GitHub label writeback for `bb-review-needed`, gated by `ENABLE_GITHUB_WRITEBACK` and `ENABLE_BB2_ARCHITECT_ESCALATION`.
7. Add Slack posting through the same Slack client planned for the Circuit issue dispatcher.
8. Add debug visibility for recent architect escalation decisions and failures.
9. Keep dry-run default enabled until staging validates signatures, dedupe, label writeback, and Slack formatting.
10. Do not production deploy until BB approves the behavior.

## Suggested Module Boundaries

```text
app/bb2_architect_escalation.py  # pure decision logic and message formatting
app/slack_client.py              # shared Slack post wrapper
app/storage.py                   # escalation dedupe persistence or table migration
app/config.py                    # env vars
app/main.py                      # webhook wiring only
app/github_writeback.py          # add label helper if not already reusable
```

Keep routing logic pure and testable without making GitHub or Slack calls.

## Test Plan

### Unit Tests

- BB2 comment with `ARCHITECT_REVIEW_REQUIRED` dispatches.
- BB2 comment with configured high-risk keyword dispatches.
- Non-BB2 comment with same keyword does not dispatch.
- Comment in unapproved repo does not dispatch.
- Comment without trigger keyword does not dispatch.
- Closed issue/PR does not dispatch unless explicitly configured.
- Pull request conversation comment is identified as `pull_request` item type.
- Issue comment is identified as `issue` item type.
- Multiple keywords in one comment produce one escalation with all matched keywords recorded.
- Slack message includes repo, item number, title, URL, author, matched trigger, label, excerpt, and required next action.
- Comment excerpt is bounded to configured length.
- Dedupe key prevents duplicate Slack posts for the same comment.

### Webhook Tests

- Missing signature is rejected before routing.
- Invalid signature is rejected before routing.
- Valid signed `issue_comment.created` event reaches escalation decision logic.
- Dispatcher disabled returns no label writeback and no Slack post.
- Dry-run mode records intended label and Slack behavior without side effects.
- Duplicate webhook redelivery is recorded as `skipped_duplicate`.

### GitHub Writeback Tests

- Adds `bb-review-needed` label when enabled.
- Treats already-present label as success.
- Records label failure without attempting unsafe fallback mutation.
- Does not remove or replace existing labels.
- Does not comment, close, merge, or mutate branches.

### Slack Tests

- Posts to configured channel `C0AR0HMTM28` when enabled.
- Optional mention is included only when configured.
- Slack token is redacted from errors/logs.
- Slack API failure records `slack_failed`.
- Network timeout records failure and does not retry in a tight loop.

### Storage Tests

- `bb2_architect_escalations` stores dedupe key and Slack timestamp.
- Unique dedupe constraint prevents duplicate records.
- Records survive app restart with SQLite configured.
- In-memory fallback is marked best-effort only.

### Manual Staging Test

1. Configure staging with `ENABLE_BB2_ARCHITECT_ESCALATION=true` and dry run enabled.
2. Send a signed fixture from an approved repo with BB2 as the comment author and `ARCHITECT_REVIEW_REQUIRED` in the body.
3. Confirm decision says label and Slack post would happen, but no side effects occur.
4. Disable dry run in staging.
5. Redeliver the same fixture and confirm dedupe suppresses duplicate Slack posting.
6. Create a controlled test issue or PR comment from the configured BB2 identity.
7. Confirm `bb-review-needed` is applied.
8. Confirm Slack message appears in `<#C0AR0HMTM28>` with the expected fields.
9. Confirm the dispatcher does not merge, mutate branches, deploy, or start code work.

## Operational Response

When Slack receives an escalation, Circuit should not continue implementation blindly. The expected human workflow is:

1. Marcus/BB reviews the linked issue or PR.
2. Marcus/BB comments with direction or resolves the architectural concern.
3. Circuit resumes only if the newest instruction authorizes implementation or requested fixes.

The dispatcher is a notification and labeling mechanism, not an architect substitute.

## Open Questions For BB

- What exact GitHub login or bot login should count as BB2?
- Should the Slack message mention Marcus/BB directly, or only post into `<#C0AR0HMTM28>`?
- Should `bb-review-needed` block task dispatch automatically, or only serve as a visible label?
- Should inline PR review comments be included in the first implementation, or stay as a later extension?
- Should any keyword besides `ARCHITECT_REVIEW_REQUIRED` be enabled initially?

## Acceptance Criteria

- The docs-first plan exists at `docs/bb2_architect_escalation_2026-06.md`.
- The plan watches BB2 issue/PR comments for `ARCHITECT_REVIEW_REQUIRED` and high-risk keywords.
- The plan applies `bb-review-needed` only after routing rules pass.
- The plan posts a structured Slack message to `<#C0AR0HMTM28>`.
- The plan includes routing rules, labels, Slack message format, dedupe behavior, env vars, security risks, and a test plan.
- No runtime code is changed.
- No production deploy, merge, or PR is performed.

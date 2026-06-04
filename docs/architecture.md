# Architecture

## Purpose

`riseos-agent-orchestrator` is an external coordination layer for RiseOS coding agents. It is intentionally planning-first: it receives GitHub events, normalizes them into task context, and prepares comment/label-only actions for human review workflows.

## Components

| Component | Responsibility |
|---|---|
| FastAPI app | Hosts health and GitHub webhook endpoints. |
| Signature verifier | Validates `X-Hub-Signature-256` before parsing payloads. |
| Event parser | Normalizes `issue_comment`, `push`, and `pull_request` payloads. |
| Task state enum | Defines the lifecycle used by agent orchestration. |
| GitHub client | Supports commit fetch, branch compare, comments, and labels. |
| OpenAI reviewer | Feature-flagged BB/Jarvis Architect review decision generation. |

## Task States

- `pending`
- `assigned`
- `working`
- `review_needed`
- `needs_changes`
- `approved_for_human_review`
- `blocked`
- `done`

## Review Decision Flow

After a coding agent finishes work, the orchestrator builds a BB/Jarvis Architect review prompt from task context, changed files, diff, and architecture context. The expected decision contract includes `decision`, `confidence`, `risk_level`, `summary`, `required_changes`, `next_task_prompt`, and `human_review_required`.

Allowed decisions are `APPROVED_FOR_HUMAN_REVIEW`, `NEEDS_CHANGES`, `BLOCKED`, and `ESCALATE_TO_MARCUS`. Human review is always required before merge. The deterministic dry-run decision remains the default. When `ENABLE_OPENAI_REVIEW=true`, `OPENAI_API_KEY` is required and `OPENAI_REVIEW_MODEL` is used to request structured JSON that validates against `ReviewDecision`. Invalid model output becomes a `BLOCKED` decision with `openai_review_error`.

The OpenAI prompt includes the `ReviewWorkItem`, changed files, diff summary, hydrated GitHub context, branch policy, no-auto-merge policy, and the human approval boundary. If `ENABLE_GITHUB_WRITEBACK=true` is also enabled, the GitHub comment and status label use only the validated `ReviewDecision`.

## Write Policy

The orchestrator must not merge PRs, push commits, modify branches, or edit repository files. GitHub writes are disabled by default and remain limited to issue comments and labels when explicitly enabled.

## Request Flow

1. GitHub sends a webhook to `/webhooks/github`.
2. The service verifies the HMAC signature using `GITHUB_WEBHOOK_SECRET`.
3. The event parser validates the event type and extracts routing context.
4. Review-needed events create a `ReviewWorkItem`.
5. Processing the work item creates either a deterministic dry-run decision or a validated OpenAI decision.
6. Optional writeback can post comments or labels only.

## Runtime Caveats

This MVP does not include queue workers or production GitHub App auth. SQLite persistence is lightweight debug/state storage, not a full job system. OpenAI review and GitHub writeback are both disabled by default and must be enabled intentionally through environment flags.

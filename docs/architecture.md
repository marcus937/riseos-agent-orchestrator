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
| GitHub client placeholder | Future wrapper for commit fetch, branch compare, comments, and labels. |
| OpenAI reviewer placeholder | Future prompt and decision interface. |

## Task States

- `pending`
- `assigned`
- `working`
- `review_needed`
- `needs_changes`
- `approved_for_human_review`
- `blocked`
- `done`

## Write Policy

The orchestrator must not merge PRs, push commits, modify branches, or edit repository files. Future GitHub writes are limited to issue comments and labels.

## Request Flow

1. GitHub sends a webhook to `/webhooks/github`.
2. The service verifies the HMAC signature using `GITHUB_WEBHOOK_SECRET`.
3. The event parser validates the event type and extracts routing context.
4. Future orchestration logic can create a task decision.
5. Future write actions can post comments or labels only.

## Runtime Caveats

This MVP does not include durable storage, queue workers, production GitHub App auth, or OpenAI calls. Those belong in later increments after the workflow contract is reviewed.

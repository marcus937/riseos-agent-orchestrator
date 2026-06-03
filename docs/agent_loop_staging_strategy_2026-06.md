# Agent Loop Staging Strategy - June 2026

Status: strategy document only. No runtime code, NGINX config, Vercel config, PR, or merge was changed.

Repo: `marcus937/riseos-agent-orchestrator`
Agent branch: `agent-integration`
Production branch: `main` only, human-approved

## Purpose

The orchestrator coordinates the AI agent development loop: Circuit works queued GitHub Issues on `agent-integration`, BB2 reviews completed commits/issues, and Marcus verifies behavior through separate human and agent staging lanes before any human-approved merge.

The orchestrator remains a separate Vultr service at `https://orchestrator.riseconnect.us`. It is not the Project Jarvis backend and should not be routed through either Jarvis staging domain.

## Deployment Lanes

| Lane | Domain | Owner | Purpose | Branch |
| --- | --- | --- | --- | --- |
| Production | production domains | Marcus + BB | Live production traffic | `main` |
| Human staging | `https://jarvis-staging.riseconnect.us` | Marcus + BB | Manual Jarvis/Rylinn integration testing, OAuth/NGINX validation, active human-led work | `staging` or current human-selected staging branch |
| Agent staging | `https://agent-jarvis-staging.riseconnect.us` | Circuit + BB2 | Test Circuit Project Jarvis work from `agent-integration` before human approval | `agent-integration` |
| Orchestrator | `https://orchestrator.riseconnect.us` | BB2/orchestrator loop | GitHub webhook intake, review queue, BB2 review, task dispatch | `agent-integration` |

`jarvis-staging.riseconnect.us` must not be overwritten by Circuit deployments. Agent staging should use `agent-jarvis-staging.riseconnect.us`.

## Branch-To-Domain Mapping

| Branch | Domain | Notes |
| --- | --- | --- |
| `main` | production domains | Production deploy only after human approval. |
| `staging` or human-selected staging branch | `jarvis-staging.riseconnect.us` | Existing Marcus + BB human staging. |
| `agent-integration` | `agent-jarvis-staging.riseconnect.us` | Recommended Project Jarvis backend lane for Circuit/BB2 work. |
| `agent-integration` | `orchestrator.riseconnect.us` | Existing BB2/orchestrator loop lane. |

## Recommended Domains

| Surface | Recommended domain | Notes |
| --- | --- | --- |
| Human Jarvis staging | `https://jarvis-staging.riseconnect.us` | Existing Marcus + BB staging. Preserve as-is. |
| Agent Jarvis staging API | `https://agent-jarvis-staging.riseconnect.us` | New recommended backend target for Circuit code verification. |
| JMC agent preview/staging | Vercel preview URL or agent staging alias | Should point to agent Jarvis staging API. |
| Orchestrator | `https://orchestrator.riseconnect.us` | Existing GitHub webhook/review/task loop service. |

Use separate secrets and access controls for each lane. The orchestrator should not be used as a reverse proxy for JMC or Project Jarvis.

## Suggested Port Mapping

Exact existing production and human-staging ports should be read from the current host config before any deployment change. Recommended model:

| Service | Bind address | Suggested/current port | Public access |
| --- | --- | ---: | --- |
| Project Jarvis production | `127.0.0.1` | existing production port | Existing production NGINX route. |
| Project Jarvis human staging | `127.0.0.1` | existing/current human staging port | Existing `jarvis-staging.riseconnect.us` OAuth/NGINX route. |
| Project Jarvis agent staging | `127.0.0.1` | `8016` | New `agent-jarvis-staging.riseconnect.us` route. |
| Orchestrator | `127.0.0.1` | `8015` | Current `orchestrator.riseconnect.us` route. |
| JMC staging | Vercel-managed | n/a | Vercel HTTPS. |

Keep orchestrator port `8015` separate from Project Jarvis agent staging. Do not move orchestrator traffic onto a Jarvis backend port.

## Required Environment Variables

Use the existing orchestrator environment file for `orchestrator.riseconnect.us` unless Marcus defines a separate staging copy. Do not commit real values.

Recommended orchestrator settings:

```bash
APP_ENV=staging
GITHUB_WEBHOOK_SECRET=replace-with-webhook-secret
GITHUB_TOKEN=replace-with-fine-grained-token-if-writeback-enabled
OPENAI_API_KEY=replace-with-openai-secret-if-enabled
OPENAI_REVIEW_MODEL=gpt-5.5-thinking
ENABLE_OPENAI_REVIEW=false
ENABLE_BB_CONTEXT_PACK=true
BB_CONTEXT_MAX_CHARS=20000
ENABLE_GITHUB_CONTEXT_HYDRATION=true
ENABLE_GITHUB_WRITEBACK=false
ENABLE_TASK_DISPATCH=false
WORK_BRANCH=agent-integration
BASE_BRANCH=main
ORCHESTRATOR_DB_PATH=/var/lib/riseos-agent-orchestrator/orchestrator.db
ORCHESTRATOR_ADMIN_TOKEN=replace-with-long-random-admin-token
ORCHESTRATOR_MAX_REVIEW_ITEMS=500
REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true
```

Controlled escalation path:

```bash
ENABLE_OPENAI_REVIEW=true
ENABLE_GITHUB_WRITEBACK=true
ENABLE_TASK_DISPATCH=true
```

Only enable the escalation path when Marcus wants BB2 comments/labels and next-task assignment. Even then, writes remain limited to GitHub comments and labels. No branch mutation, repo file writes, issue closing, auto-merge, or production deploys are allowed.

## NGINX Routing Plan

No NGINX edits are made by this document. The orchestrator should remain on `orchestrator.riseconnect.us`, proxying to the current orchestrator port `8015`.

Recommended server block shape for reference only:

```nginx
server {
    server_name orchestrator.riseconnect.us;

    location / {
        proxy_pass http://127.0.0.1:8015;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Recommended public paths:

- `GET /health` for basic health.
- `POST /webhooks/github` for signed GitHub webhook events.
- `GET /debug/health` for operator diagnostics, protected when `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true`.
- `GET /debug/review-queue` for queue visibility, protected if exposed publicly.
- `POST /debug/review-queue/{id}/process` for controlled processing, always admin-token protected.

Do not route JMC, Project Jarvis human staging, or Project Jarvis agent staging through the orchestrator domain.

## OAuth And Security Notes

- Human staging may remain behind the current OAuth and NGINX controls at `jarvis-staging.riseconnect.us`.
- Agent staging should be OAuth-protected, Tailscale-only, or IP-restricted before exposing sensitive routes.
- Orchestrator debug/admin endpoints must not be exposed publicly without admin token protections.
- `POST /debug/review-queue/{id}/process` must remain admin-token protected.
- GitHub webhook secrets, admin tokens, OpenAI keys, and GitHub tokens must stay out of docs and frontend env vars.
- GitHub token scope should remain limited to needed read, issue comment, and label permissions.

## Vercel Relationship

JMC agent preview/staging runs on Vercel and should point to `agent-jarvis-staging.riseconnect.us`, not `jarvis-staging.riseconnect.us`.

The orchestrator can become a future read-only Agent Ops data source, but normal JMC Mission Control rendering should call Project Jarvis agent staging directly.

Responsibilities:

- JMC agent preview verifies frontend UX and backend contracts.
- Project Jarvis agent staging serves backend APIs and websockets for Circuit work.
- Orchestrator receives GitHub events, stores review queue items, runs BB2 review when enabled, and posts comments/labels when enabled.

A future JMC Agent Ops panel may call read-only orchestrator endpoints, but no deploy/merge/write actions should be exposed from the frontend without a separate human-approved design.

## Agent Loop Flow

1. Marcus or BB creates a GitHub Issue labeled `agent-task` and `agent-ready`.
2. Circuit works the task on `agent-integration` only.
3. Circuit comments `Status: Done` with the completed commit SHA and separates VERIFIED, ASSUMED, and UNVERIFIED details.
4. GitHub webhook sends the event to `orchestrator.riseconnect.us`.
5. Orchestrator records a review queue item.
6. BB2 review runs when `ENABLE_OPENAI_REVIEW=true`; otherwise processing remains deterministic/dry-run.
7. If writeback is enabled, the orchestrator posts BB2 review comments and labels only.
8. If task dispatch is enabled and BB2 approves for human review, the orchestrator may assign the next `agent-ready` issue with `agent-next`.
9. Marcus visually verifies the result in JMC agent preview against `agent-jarvis-staging.riseconnect.us`.
10. Human decides whether to merge to `main` and deploy production.

## Deployment Verification Checklist

Orchestrator health:

- `GET /health` returns `{"status":"ok"}` on `orchestrator.riseconnect.us`.
- `GET /debug/health` is protected when staging policy requires it.
- SQLite database path is orchestrator-specific.
- Logs identify the orchestrator environment and do not imply Project Jarvis staging ownership.

Webhook and queue:

- GitHub webhook uses the intended secret.
- Push or `Status: Done` events on `agent-integration` create review-needed items.
- Duplicate pending items are suppressed.
- Review queue can be inspected by an authorized operator.

BB2 review:

- OpenAI calls remain disabled unless explicitly enabled.
- BB context pack is enabled for architecture review.
- BB2 review separates code inspected, code executed, tests executed, assumptions, and verification gaps.
- Human approval boundary is present in review output.

Writeback and dispatch:

- With writeback disabled, no GitHub comments/labels are posted.
- With writeback enabled, only review comments and BB2 labels are posted.
- With dispatch disabled, no next-task assignment is posted.
- With dispatch enabled, only `Circuit Assignment` comments and `agent-next` labels are posted.
- No issue is closed, branch mutated, PR merged, repository file written, NGINX changed, Vercel changed, or production deployed.

Lane isolation:

- `jarvis-staging.riseconnect.us` still belongs to Marcus + BB human staging.
- `agent-jarvis-staging.riseconnect.us` is the backend target for Circuit code verification.
- `orchestrator.riseconnect.us` remains the GitHub webhook/review/task loop service.
- Production remains untouched.

## Rollback Plan

Orchestrator rollback:

1. Identify the last known good orchestrator commit SHA on `agent-integration`.
2. Restore only the orchestrator checkout/deployment to that SHA.
3. Restart only the orchestrator service on port `8015`.
4. Confirm `/health` and debug health.
5. Disable `ENABLE_GITHUB_WRITEBACK` and `ENABLE_TASK_DISPATCH` if loop behavior is noisy or incorrect.
6. Record the failed commit and symptom in the related GitHub Issue.
7. Leave Project Jarvis human staging, Project Jarvis agent staging, JMC staging, and production untouched unless separately implicated.

Emergency safety rollback:

- Set `ENABLE_OPENAI_REVIEW=false` to stop live model calls.
- Set `ENABLE_GITHUB_WRITEBACK=false` to stop GitHub comments/labels.
- Set `ENABLE_TASK_DISPATCH=false` to stop next-task assignment.
- Rotate the webhook secret or disable the webhook if event ingestion must stop.

## Risks And Blockers

- `jarvis-staging.riseconnect.us` is active human/BB staging and must not be overwritten by Circuit deployments.
- Orchestrator comments/labels can still affect real GitHub Issues; keep writeback expected and reversible.
- Agent staging needs OAuth, Tailscale, or IP restriction before exposing sensitive backend routes.
- Debug/admin endpoints must remain token-protected.
- Dispatch depends on issue labels being maintained consistently.
- Visual testing through JMC does not replace backend tests or BB2 source review.
- Agent Ops dashboard write controls are explicitly out of scope for now.

## Future Agent Ops Dashboard Idea

A future read-only Agent Ops dashboard in JMC could show:

- orchestrator health and queue depth at `orchestrator.riseconnect.us`
- current review queue items
- BB2 review decisions and labels
- next `agent-ready` issue candidate
- Circuit completion comments and verification sections
- Project Jarvis agent staging commit/health
- JMC agent preview deployment URL and commit
- last agent staging verification checklist result
- rollback target commit for each service

Start read-only. Any future write action such as process queue, dispatch next task, deploy staging, rollback, merge, or production deploy must require explicit human approval and a separate architecture review.

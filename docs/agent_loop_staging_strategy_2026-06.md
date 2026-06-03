# Agent Loop Staging Strategy - June 2026

Status: strategy document only. No runtime code was changed.

Repo: `marcus937/riseos-agent-orchestrator`
Branch for staging: `agent-integration`
Production branch: `main` only, human-approved

## Purpose

The orchestrator staging service coordinates the AI agent development loop: Circuit works queued GitHub Issues on `agent-integration`, BB2 reviews completed commits/issues, and Marcus visually verifies behavior through JMC and backend staging before any human-approved merge.

This service remains separate from Project Jarvis and JMC. It should run on Vultr as its own FastAPI app behind NGINX.

## Deployment Boundary

- Circuit works only on `agent-integration`.
- BB2 reviews completed commits/issues before human merge.
- The orchestrator may receive GitHub webhooks and, when enabled, post comments/labels only.
- No auto-merge.
- No branch mutation.
- No repository file writes.
- No issue closing.
- No production deploys by agents.
- Human approval remains required before merge and production deployment.

## Recommended Domains

| Surface | Recommended domain | Notes |
| --- | --- | --- |
| Orchestrator staging | `https://orchestrator-staging.riseconnect.us` | GitHub webhook target for staging loop tests. |
| Orchestrator production | `https://orchestrator.riseconnect.us` | Production loop target when approved. |
| Project Jarvis staging API | `https://jarvis-api-staging.riseconnect.us` | Backend visual/API verification target. |
| JMC staging frontend | `https://jmc-staging.riseconnect.us` | Browser surface for Marcus. |

Use separate GitHub webhook configurations for staging and production if both are active. Staging should target only the `agent-integration` workflow and use staging secrets.

## Recommended Ports

| Service | Bind address | Port | Public access |
| --- | --- | ---: | --- |
| Orchestrator staging Uvicorn | `127.0.0.1` | `8012` | Via NGINX only. |
| Orchestrator production Uvicorn | `127.0.0.1` | `8010` | Via NGINX only. |
| Project Jarvis staging Uvicorn | `127.0.0.1` | `8011` | Separate service and domain. |
| JMC staging | Vercel-managed | n/a | Vercel HTTPS. |

Keep the staging orchestrator on a separate port and systemd unit from production so task-loop experiments cannot interrupt the production process.

## Required Environment Variables

Use a staging-specific env file such as `/etc/riseos-agent-orchestrator-staging.env`. Do not commit real values.

Recommended staging settings:

```bash
APP_ENV=staging
GITHUB_WEBHOOK_SECRET=replace-with-staging-webhook-secret
GITHUB_TOKEN=replace-with-staging-fine-grained-token-if-writeback-enabled
OPENAI_API_KEY=replace-with-staging-openai-secret-if-enabled
OPENAI_REVIEW_MODEL=gpt-5.5-thinking
ENABLE_OPENAI_REVIEW=false
ENABLE_BB_CONTEXT_PACK=true
BB_CONTEXT_MAX_CHARS=20000
ENABLE_GITHUB_CONTEXT_HYDRATION=true
ENABLE_GITHUB_WRITEBACK=false
ENABLE_TASK_DISPATCH=false
WORK_BRANCH=agent-integration
BASE_BRANCH=main
ORCHESTRATOR_DB_PATH=/var/lib/riseos-agent-orchestrator-staging/orchestrator.db
ORCHESTRATOR_ADMIN_TOKEN=replace-with-long-random-staging-admin-token
ORCHESTRATOR_MAX_REVIEW_ITEMS=500
REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true
```

Controlled staging escalation path:

```bash
ENABLE_OPENAI_REVIEW=true
ENABLE_GITHUB_WRITEBACK=true
ENABLE_TASK_DISPATCH=true
```

Only enable the escalation path when Marcus wants staging to post BB2 comments/labels and assign next issues. Even then, writes remain limited to GitHub comments and labels. Keep production writeback and dispatch separately reviewed.

## NGINX Routing Plan

Recommended staging server block shape:

```nginx
server {
    server_name orchestrator-staging.riseconnect.us;

    location / {
        proxy_pass http://127.0.0.1:8012;
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
- `POST /webhooks/github` for signed staging webhook events.
- `GET /debug/health` for operator diagnostics, protected when `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=true`.
- `GET /debug/review-queue` for queue visibility, protected in staging if exposed publicly.
- `POST /debug/review-queue/{id}/process` for controlled processing, always admin-token protected.

Do not route JMC or Project Jarvis through the orchestrator domain.

## Vercel Relationship

JMC staging runs on Vercel and should not depend on the orchestrator for normal Mission Control rendering. The orchestrator can become a future read-only Agent Ops data source, but the first staging loop should keep responsibilities separate:

- JMC staging verifies frontend UX and backend contracts.
- Project Jarvis staging serves backend APIs and websockets.
- Orchestrator staging receives GitHub events, stores review queue items, runs BB2 review when enabled, and posts comments/labels when enabled.

A future JMC Agent Ops panel may call a read-only orchestrator endpoint, but no deploy/merge/write actions should be exposed from the frontend without a separate human-approved design.

## Agent Loop Flow

1. Marcus or BB creates a GitHub Issue labeled `agent-task` and `agent-ready`.
2. Circuit works the task on `agent-integration` only.
3. Circuit comments `Status: Done` with the completed commit SHA and separates VERIFIED, ASSUMED, and UNVERIFIED details.
4. GitHub webhook sends the event to orchestrator staging.
5. Orchestrator records a review queue item.
6. BB2 review runs when `ENABLE_OPENAI_REVIEW=true`; otherwise processing remains deterministic/dry-run.
7. If writeback is enabled, the orchestrator posts BB2 review comments and labels only.
8. If task dispatch is enabled and BB2 approves for human review, the orchestrator may assign the next `agent-ready` issue with `agent-next`.
9. Marcus visually verifies the result in JMC staging against Project Jarvis staging.
10. Human decides whether to merge to `main` and deploy production.

## Deployment Verification Checklist

Orchestrator health:

- `GET /health` returns `{"status":"ok"}`.
- `GET /debug/health` is protected when staging policy requires it.
- SQLite database path is staging-specific.
- Logs show `APP_ENV=staging` or staging service identity where available.

Webhook and queue:

- GitHub staging webhook uses the staging secret.
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
- No issue is closed, branch mutated, PR merged, or repository file written.

Visual loop:

- JMC staging loads from Vercel.
- JMC staging points to Project Jarvis staging.
- Marcus can verify the completed task visually without routine SSH/curl/GitHub CLI.
- Verification gaps are recorded in the issue or BB2 review.

## Rollback Plan

Orchestrator staging rollback:

1. Identify the last known good `agent-integration` commit SHA.
2. Restore only the staging orchestrator checkout/deployment to that SHA.
3. Restart only the staging orchestrator service.
4. Confirm `/health` and debug health.
5. Disable `ENABLE_GITHUB_WRITEBACK` and `ENABLE_TASK_DISPATCH` if staging behavior is noisy or incorrect.
6. Record the failed commit and symptom in the related GitHub Issue.
7. Leave Project Jarvis staging, JMC staging, and production untouched unless separately implicated.

Emergency safety rollback:

- Set `ENABLE_OPENAI_REVIEW=false` to stop live model calls.
- Set `ENABLE_GITHUB_WRITEBACK=false` to stop GitHub comments/labels.
- Set `ENABLE_TASK_DISPATCH=false` to stop next-task assignment.
- Rotate staging webhook secret or disable the staging webhook if event ingestion must stop.

## Risks And Blockers

- Staging writeback can still affect real GitHub Issues; keep labels/comments expected and reversible.
- GitHub token scope must stay limited to needed read, issue comment, and label permissions.
- OpenAI review costs and latency should be expected when enabled.
- Dispatch depends on issue labels being maintained consistently.
- Visual testing through JMC does not replace backend tests or BB2 source review.
- If staging and production webhooks point at the same repo events, responses must be clearly separated by domain and secrets.
- Agent Ops dashboard write controls are explicitly out of scope for now.

## Future Agent Ops Dashboard Idea

A future read-only Agent Ops dashboard in JMC could show:

- orchestrator staging health and queue depth
- current review queue items
- BB2 review decisions and labels
- next `agent-ready` issue candidate
- Circuit completion comments and verification sections
- Project Jarvis staging commit/health
- JMC staging deployment URL and commit
- last staging verification checklist result
- rollback target commit for each service

Start read-only. Any future write action such as process queue, dispatch next task, deploy staging, rollback, merge, or production deploy must require explicit human approval and a separate architecture review.

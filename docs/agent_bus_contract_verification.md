# Agent Bus Contract Verification

## VERIFIED

The orchestrator contract was verified against the repository's canonical Agent Bus integration documentation and live connector status.

- `docs/agent_bus_dispatch_flow.md` documents WorkItem creation as `POST /work-items`.
- `docs/agent_bus_dispatch_flow.md` documents required settings:
  - `ENABLE_AGENT_BUS_DISPATCH=true`
  - `AGENT_BUS_BASE_URL=<agent bus base URL>`
  - `AGENT_BUS_TOKEN=<optional bearer token>`
  - `AGENT_BUS_OWNER_AGENT=codex-m2`
  - `AGENT_BUS_REVIEW_AGENT=bb2`
- `docs/agent_bus_dispatch_flow.md` documents request fields:
  - `title`
  - `repository`
  - `issue_number`
  - `priority`
  - `owner_agent`
  - `review_agent`
  - `metadata`
- Existing client implementation `app/clients/agent_bus.py` uses `Authorization: Bearer <AGENT_BUS_TOKEN>` when a token is configured.
- Existing dispatch implementation stores returned `work_item_id` on the orchestration state.
- The live Jarvis Agent Bus status endpoint responded successfully on 2026-06-18, confirming the configured Agent Bus service is reachable through the connector.

## Contract Applied In Code

- Endpoint path: `POST /work-items`.
- Auth header: `Authorization: Bearer <token>` when `AGENT_BUS_TOKEN` is configured.
- Request payload: `title`, `repository`, `issue_number`, `priority`, `owner_agent`, `review_agent`, and `metadata`.
- Callback contract: callback metadata points to `POST /api/v1/agent-tasks/{task_id}/execution-result`.
- Review agent semantics: `review_agent` defaults to `bb2` and is configurable through `AGENT_BUS_REVIEW_AGENT`.
- Response payload: must include `work_item_id`; ambiguous fallback `id` is no longer accepted.

## ASSUMED

The external Agent Bus server's full OpenAPI schema is not stored in this repository. This PR therefore treats the in-repo dispatch flow documentation plus the live status connector as the canonical contract available to the orchestrator repo.

## UNVERIFIED

End-to-end Agent Bus WorkItem creation against production was not executed by this PR. Production dispatch requires deployment credentials and must be verified during rollout.

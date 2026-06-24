# Circuit Wakeup 401 Root Cause

## Scope

This note documents the automatic Circuit wakeup path for workflow-owned Circuit work and the observed HTTP 401 failure emitted after Agent Bus work item creation succeeds.

## Verified Execution Path

1. `POST /api/v1/workflows` creates the workflow and stores its tasks.
2. `app.workflow_routes.create_workflow_endpoint` calls `release_runnable_agent_tasks(...)` when Agent Bus dispatch is enabled.
3. `app.agent_task_release.release_runnable_agent_tasks` dispatches the runnable Circuit task to Agent Bus with `dispatch_agent_task_to_agent_bus(...)`.
4. Agent Bus returns a work item ID.
5. `mark_agent_task_assigned(...)` stores the returned `agent_bus_work_item_id` on the task and persists the task.
6. `_confirm_work_item_visibility(...)` reads the exact Agent Bus work item back with `GET /work-items/{work_item_id}`.
7. `dispatch_circuit_wakeup_for_assigned_task(...)` calls `wake_circuit_agent_for_work(...)`.
8. `app.circuit_agent_trigger.CircuitAgentTriggerHTTPClient.post_wakeup` POSTs to the configured ChatGPT Workspace Agent trigger endpoint.

The observed `task_circuit_wakeup_attempted` event maps to the task lifecycle event named `circuit_wakeup_attempted`, which is appended after `wake_circuit_agent_for_work(...)` returns.

## Request Contract

The canonical trigger request is:

- Method: `POST`
- URL: `CIRCUIT_AGENT_TRIGGER_URL`
- Required path: `/trigger`
- Headers:
  - `Authorization: Bearer <normalized CIRCUIT_AGENT_ACCESS_TOKEN>`
  - `Content-Type: application/json`
  - `Accept: application/json`
- Body: `{ "input": "<wake-up message>" }`
- Successful status codes: any 2xx status, including `200` and `202`

The wake-up message includes the repository, workflow ID, and Agent Bus work item ID when available.

## Root Cause Classification

A `401` response is returned by the ChatGPT Workspace Agent trigger endpoint before Circuit can start work. Agent Bus work item creation and readback are already complete at that point, so the failure is not caused by Agent Bus visibility, task assignment persistence, or Circuit MCP lookup.

The failure class is authentication rejection at the trigger endpoint. In deployment terms this is one of:

- stale token
- token mismatch
- environment mismatch between `CIRCUIT_AGENT_TRIGGER_URL` and `CIRCUIT_AGENT_ACCESS_TOKEN`
- malformed Authorization header caused by storing the token with surrounding whitespace or an included `Bearer ` prefix

The code now classifies HTTP 401 as `token_mismatch_or_stale_token` and normalizes the configured token before sending the Authorization header.

## Fix

`app/circuit_agent_trigger.py` now normalizes `CIRCUIT_AGENT_ACCESS_TOKEN` by:

- trimming surrounding whitespace
- accepting either a raw token or an accidentally stored `Bearer <token>` value
- sending exactly one `Bearer ` prefix in the Authorization header
- redacting both raw and normalized token forms from logs

It also sends explicit JSON headers and logs a redacted failure classification for 401/403 responses.

## Remaining Environment Validation

A live end-to-end wakeup requires the deployed environment to hold a current token for the configured workspace agent trigger URL. If 401 continues after this patch, rotate or correct `CIRCUIT_AGENT_ACCESS_TOKEN` for the same workspace agent ID used in `CIRCUIT_AGENT_TRIGGER_URL`.

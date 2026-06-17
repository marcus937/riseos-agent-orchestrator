# Worker Dispatch Architecture Audit

Date: 2026-06-17

Scope:

1. `marcus937/riseos-agent-orchestrator`
2. `marcus937/jarvis-agent-bus-mcp` / Agent Bus
3. Jarvis MCP surface

Note: a repository named exactly `marcus937/jarvis-mcp` was not available through the installed GitHub app. Repository search for `jarvis mcp` returned `marcus937/jarvis-agent-bus-mcp`. This audit therefore treats the Jarvis MCP surface as the MCP gateway and MCP tool layer implemented inside Agent Bus, plus its documented MCP tool contract.

## Executive Answer

`codex-m2` is intended to receive work through Agent Bus.

The canonical worker dispatch path for autonomous coding is:

```text
Orchestrator
-> Agent Bus WorkItem
-> codex-m2 worker
```

Jarvis MCP does not appear to be a separate durable queue that should sit between Orchestrator and `codex-m2`. It is a tool/access surface over the same Agent Bus capabilities, exposing MCP tools such as `create_work_item`, `get_agent_inbox`, `claim_work_item`, `transition_work_item`, `create_evidence_packet`, and `attach_evidence_to_work_item`.

Therefore the answer is C:

```text
Agent Bus and Jarvis MCP serve different responsibilities.
```

Agent Bus is the durable dispatch, ownership, lifecycle, evidence, and review substrate. Jarvis MCP is an access/tooling facade that lets agents or runtimes call Agent Bus operations through MCP-style tools.

## Findings By System

### 1. RiseOS Orchestrator

Relevant files inspected:

- `app/agent_task_routes.py` on PR #109 branch
- `app/agent_task_dispatch.py` on PR #109 branch
- `app/agent_tasks.py` on PR #109 branch
- `app/workflows.py` on PR #109 branch
- `app/clients/agent_bus.py` on `agent-integration`
- `app/task_dispatch.py` on `agent-integration`
- `docs/autonomous_execution_bridge.md` on PR #109 branch

Observed responsibilities:

- Accept task intake through `POST /api/v1/agent-tasks`.
- Validate the repository is orchestration-enabled.
- Persist canonical AgentTask state.
- Expose AgentTask state through workflow APIs.
- When `ENABLE_AGENT_BUS_DISPATCH=true`, create an Agent Bus WorkItem using `AgentBusClient.create_work_item`.
- Store the returned Agent Bus `work_item_id` on the AgentTask.
- Accept worker completion through `POST /api/v1/agent-tasks/{task_id}/execution-result`.

The Orchestrator does not poll for worker execution and does not own a worker queue. In PR #109, Orchestrator creates one durable Agent Bus WorkItem and waits for callback/state updates.

### 2. Agent Bus

Relevant files inspected:

- `src/agent_bus_mcp/api.py`
- `src/agent_bus_mcp/models.py`
- `src/agent_bus_mcp/mcp.py`
- `docs/rest-api-reference.md`
- `docs/external-agent-contract-v1.md`
- `docs/connector-architecture.md`
- `docs/runtime-worker-setup.md`
- `docs/mcp-tool-reference.md`

Observed responsibilities:

- Durable agent registry.
- Heartbeats and worker liveness.
- Durable WorkItem creation and filtering.
- Role-aware worker inbox.
- Agent queue lookup.
- WorkItem claim and transition lifecycle.
- Evidence packet creation and attachment.
- Review packet creation and attachment.
- Connector and dispatch lifecycle docs for worker-specific wrappers.

Relevant REST endpoints:

```text
POST /work-items
GET /work-items
GET /work-items/{work_item_id}
GET /agents/{agent_id}/inbox
GET /agents/{agent_id}/queue
POST /work-items/{work_item_id}/claim
POST /work-items/{work_item_id}/transition
POST /evidence-packets
POST /work-items/{work_item_id}/evidence
POST /agents
POST /agents/heartbeat
```

Relevant MCP tools from Agent Bus:

```text
create_work_item
get_agent_inbox
get_agent_queue
claim_work_item
transition_work_item
create_evidence_packet
attach_evidence_to_work_item
register_agent
heartbeat_agent
```

Agent Bus is the canonical work dispatch and worker ownership system.

### 3. Jarvis MCP Surface

A standalone `marcus937/jarvis-mcp` repository was not found. The available MCP implementation is in `jarvis-agent-bus-mcp`.

Observed MCP responsibilities:

- Expose Agent Bus operations as MCP tools.
- Let agents call worker lifecycle operations through `POST /mcp/call` and discover tools through `GET /mcp/tools`.
- Provide convenience access to Agent Bus registry, inbox, queue, WorkItems, evidence, and reviews.

This MCP surface is not a separate worker queue. It is an access path into Agent Bus.

## Architecture Classification

### Option A

```text
Orchestrator -> Agent Bus -> codex-m2
```

This is the canonical durable worker dispatch path.

### Option B

```text
Orchestrator -> Jarvis MCP -> codex-m2
```

This is not supported by the inspected architecture as the canonical dispatch path. MCP may be used by a worker as a tool-access mechanism, but the durable work item still lives in Agent Bus.

### Option C

```text
Agent Bus and Jarvis MCP serve different responsibilities.
```

This is the most accurate system-level answer.

## Architecture Diagram

```text
Task Intake
==========

External caller or GitHub issue path
        |
        v
RiseOS Orchestrator
        |
        | POST /api/v1/agent-tasks
        | - validate repository
        | - create AgentTask
        | - persist AgentTask
        | - emit created/queued lifecycle
        v
AgentTask workflow record


Dispatch
========

RiseOS Orchestrator
        |
        | if ENABLE_AGENT_BUS_DISPATCH=true
        | POST {AGENT_BUS_BASE_URL}/work-items
        v
Agent Bus WorkItem
        |
        | metadata:
        | - task_id
        | - workflow_id
        | - repo_full_name
        | - objective
        | - instructions
        | - acceptance_criteria
        | - target_agent=codex-m2
        v
WorkItem owner_agent=codex-m2


Worker Ownership
================

codex-m2 worker
        |
        | GET /agents/codex-m2/inbox
        | or
        | GET /agents/codex-m2/queue
        v
Assigned Agent Bus WorkItem
        |
        | POST /work-items/{work_item_id}/claim
        | POST /work-items/{work_item_id}/transition
        v
claimed / in_progress WorkItem


Execution
=========

codex-m2 worker runtime
        |
        | reads WorkItem metadata
        | executes Codex outside Orchestrator
        | performs Git operations outside Orchestrator
        | captures commit/evidence
        v
execution result


Tool Access
===========

codex-m2 worker may use either:

1. Agent Bus REST endpoints directly
2. Jarvis MCP / Agent Bus MCP tools:
   - get_agent_inbox
   - get_agent_queue
   - claim_work_item
   - transition_work_item
   - create_evidence_packet
   - attach_evidence_to_work_item

Both access paths operate on the same Agent Bus WorkItem model.


Completion Callback
===================

codex-m2 worker
        |
        | POST /api/v1/agent-tasks/{task_id}/execution-result
        | payload includes:
        | - agent_id
        | - status
        | - commit_sha
        | - branch
        | - changed_files
        | - evidence
        v
RiseOS Orchestrator
        |
        | update AgentTask
        | append lifecycle event
        | store execution evidence
        | update workflow projection
        v
Workflow state COMPLETED / BLOCKED
```

## How codex-m2 Should Receive Work

`codex-m2` should receive work by polling Agent Bus, not by polling Orchestrator and not by polling a separate Jarvis MCP queue.

Minimum intended `codex-m2` worker loop:

```text
1. Register with Agent Bus as codex-m2.
2. Heartbeat to Agent Bus.
3. Poll GET /agents/codex-m2/inbox or GET /agents/codex-m2/queue.
4. Select a queued WorkItem where owner_agent=codex-m2.
5. Claim it through POST /work-items/{work_item_id}/claim.
6. Transition it through Agent Bus lifecycle.
7. Execute Codex and collect evidence.
8. Optionally submit evidence to Agent Bus.
9. POST result back to Orchestrator execution-result callback.
```

Jarvis MCP may be used by the worker to call steps 2-8 as tools, but it should not become a separate dispatch source.

## Does PR #109 Introduce A Duplicate Queue?

No.

PR #109 does not introduce a second worker queue. It creates canonical Orchestrator AgentTask intake state and then, when enabled, creates exactly one Agent Bus WorkItem for worker dispatch.

The Orchestrator AgentTask is the intake/workflow record. The Agent Bus WorkItem is the execution queue record. These are separate records with different responsibilities, tied together by:

```text
task_id
workflow_id
agent_bus_work_item_id
```

This is not a duplicate queue because Orchestrator does not expose a worker poll/claim API and does not assign worker execution independently of Agent Bus.

## Does PR #109 Introduce A Duplicate Dispatch Mechanism?

No, with one boundary to preserve.

PR #109 introduces a bridge from AgentTask to Agent Bus WorkItem creation. It does not introduce worker polling, worker claiming, dispatch acknowledgement, or worker lifecycle outside Agent Bus.

The boundary to preserve is:

```text
Orchestrator may create WorkItems.
Agent Bus owns worker dispatch and lifecycle.
Workers must not poll Orchestrator for work.
```

As long as future work keeps `codex-m2` polling Agent Bus inbox/queue and not `/api/v1/agent-tasks`, PR #109 remains aligned with the canonical architecture.

## Recommendation Before Merge

PR #109 is architecturally aligned with the intended split:

- Orchestrator: task intake, workflow state, callback receiver.
- Agent Bus: durable worker dispatch, ownership, lifecycle, evidence/review queue.
- Jarvis MCP: tool-access facade into Agent Bus operations.
- `codex-m2`: worker runtime that consumes Agent Bus WorkItems and calls back with execution results.

Merge risk is acceptable from an architecture perspective if Marcus agrees that direct AgentTask submission should immediately create Agent Bus WorkItems when `ENABLE_AGENT_BUS_DISPATCH=true`.

## Residual Questions

1. Should `codex-m2` use raw Agent Bus REST endpoints or MCP tools in production?

Either is compatible. REST is simpler for a daemon worker. MCP tools are useful when the worker runtime is already tool-driven.

2. Should Orchestrator callback auth be added before live exposure?

The current callback accepts execution results from any caller that can reach the endpoint and knows the task id. If the endpoint is publicly reachable, add API-gateway protection or a first-class Orchestrator callback token before live autonomous execution.

3. Should Agent Bus dispatch records be used in addition to WorkItems?

Not required for the minimum bridge. Existing WorkItem inbox/queue/claim/transition endpoints are enough for the first autonomous coding execution. Dispatch records may be useful later for richer connector-specific telemetry, but they should not replace WorkItems.

# First Autonomous Execution Gap Analysis

Date: 2026-06-17

Scope:

```text
GitHub Issue labeled agent-task + agent-ready
-> RiseOS Orchestrator
-> Agent Bus WorkItem
-> Codex Worker
-> Codex CLI
-> Git Commit on agent-integration
```

This is a documentation-only audit. No runtime code was changed.

## Executive Summary

The current system is partially wired through Agent Bus WorkItem creation, but it does not yet execute an autonomous coding task end-to-end.

Implemented:

- RiseOS Orchestrator can select the oldest unclaimed GitHub issue labeled `agent-task` and `agent-ready`.
- RiseOS Orchestrator can build an Agent Bus WorkItem payload with `owner_agent=codex-m2`, `review_agent=bb2`, target repository, issue number, branch metadata, and issue objective.
- RiseOS Orchestrator has an Agent Bus HTTP client that posts to `POST /work-items`.
- Agent Bus has core REST/MCP primitives to register agents, heartbeat agents, create/list/claim/transition work items, and expose an agent inbox.

Partially implemented:

- The Orchestrator dispatch path is gated behind the approved-review/writeback flow. A newly labeled `agent-ready` issue is queued/visible, but it does not immediately become an Agent Bus WorkItem unless an approved review/writeback event triggers `dispatch_next_agent_task`.
- Agent Bus has queue/inbox primitives, but the documented worker connector files and worker-specific `/codex/*` route stack are not present in the inspected `main` branch.
- Agent Bus documentation describes External Agent Contract v1 and Codex connector behavior, but the executable Codex connector/worker implementation is missing.

Missing:

- A running Codex Worker process for `codex-m2`.
- Worker polling or inbox consumption from Agent Bus.
- WorkItem claim/start/complete lifecycle calls from a worker.
- Repository checkout/update logic for the target issue.
- Codex CLI invocation with the issue objective.
- Commit creation on `agent-integration`.
- Evidence reporting containing commit SHA, changed files, and commands run.

## Hop-by-Hop Trace

| Hop | Status | What Exists | Gap |
| --- | --- | --- | --- |
| GitHub Issue -> RiseOS Orchestrator | Partially implemented | Orchestrator parses GitHub webhooks, creates review work items when events include review context, and can list GitHub issues by labels through `dispatch_next_agent_task`. | An `agent-task` + `agent-ready` issue alone is not enough to start autonomous execution. Dispatch currently runs only after a successful GitHub writeback and `APPROVED_FOR_HUMAN_REVIEW` decision. |
| RiseOS Orchestrator -> Agent Bus WorkItem | Partially implemented | `app/task_dispatch.py` selects the oldest unclaimed `agent-ready` issue and builds a WorkItem payload. `app/clients/agent_bus.py` posts that payload to `/work-items`. `app/main.py` wires this after approved review/writeback when `ENABLE_TASK_DISPATCH` and `ENABLE_AGENT_BUS_DISPATCH` are enabled. | The path is coupled to the review/writeback lifecycle instead of directly triggered from the issue-ready event. It also does not prove live Agent Bus auth or timeout config on the inspected `agent-integration` branch. |
| Agent Bus WorkItem -> Codex Worker | Missing | Agent Bus core API supports `GET /agents/{agent_id}/inbox`, `GET /agents/{agent_id}/queue`, `POST /work-items/{id}/claim`, `POST /work-items/{id}/transition`, and MCP equivalents. | No executable Codex Worker was found. Searches for Codex worker code, `codex-m2`, worker polling, `codex` subprocess invocation, and worker-specific aliases did not find an implementation. |
| Codex Worker -> Codex CLI | Missing | Agent Bus docs describe Codex as a worker type and say Codex workers should advertise repository/test capabilities and submit evidence. | No code was found that shells out to `codex`, builds a prompt from WorkItem metadata, streams output, handles failure, or maps Codex output to Agent Bus lifecycle state. |
| Codex CLI -> Git Commit | Missing | Repository instructions define branch safety expectations and Agent Bus evidence docs describe commit SHA reporting. | No worker code was found that checks out the target repo, ensures `agent-integration`, applies Codex changes, runs validation, commits, and reports the commit SHA. |

## Detailed Findings

### 1. GitHub Issue To Orchestrator

Status: partially implemented.

Relevant Orchestrator behavior:

- `dispatch_next_agent_task` lists open issues with labels `agent-task` and `agent-ready`.
- It filters out pull requests and issues already carrying ownership/blocking labels such as `agent-next`, `agent-working`, or `bb2-blocked`.
- It selects the oldest eligible issue.

Important gap:

- The webhook path does not directly dispatch an `agent-ready` issue to Agent Bus.
- In `app/main.py`, dispatch is called only after `_process_work_item` completes OpenAI/BB2-style review processing, GitHub writeback succeeds, and the decision is `APPROVED_FOR_HUMAN_REVIEW`.

Implication:

An issue labeled `agent-task` and `agent-ready` can be selected by the dispatcher, but another review/writeback event must currently trigger the dispatcher.

### 2. Orchestrator To Agent Bus WorkItem

Status: partially implemented.

Implemented:

- `build_agent_bus_work_item_payload` creates:
  - `title`
  - `repository`
  - `issue_number`
  - `priority`
  - `owner_agent`
  - `review_agent`
  - `metadata.objective`
  - `metadata.branch`
  - `metadata.issue_url`
  - routing metadata
- `AgentBusClient.create_work_item` posts JSON to `${AGENT_BUS_BASE_URL}/work-items`.
- `dispatch_next_agent_task` records success/failure fields including `agent_bus_work_item_id`.
- Orchestrator still applies the existing `agent-next` label and posts the Circuit Assignment comment.

Partial gaps:

- The branch inspected still has `AgentBusClient` timeout hardcoded to `20.0`; the requested `AGENT_BUS_TIMEOUT_SECONDS` work appears to be pending in a separate PR, not present in the base inspected branch.
- The Orchestrator path posts one WorkItem but does not create an Agent Bus dispatch record. Core WorkItem assignment may be enough for a minimal worker, but it does not use the documented External Agent Contract dispatch lifecycle.

### 3. Agent Bus WorkItem To Codex Worker

Status: missing for executable worker loop; partially implemented for queue substrate.

Implemented in Agent Bus:

- `POST /work-items` creates durable WorkItems.
- `GET /work-items` lists/filter work.
- `GET /agents/{agent_id}/queue` lists work assigned to an agent.
- `GET /agents/{agent_id}/inbox` builds a role-aware inbox and puts queued/claimed work in `assigned`.
- `POST /work-items/{work_item_id}/claim` claims queued work.
- `POST /work-items/{work_item_id}/transition` moves work through lifecycle states.
- MCP tools expose equivalent calls such as `get_agent_inbox`, `claim_work_item`, and `transition_work_item`.

Missing:

- No Codex Worker process or daemon was found.
- No polling loop was found for `codex-m2`.
- No code was found that reads Agent Bus inbox/queue and claims a WorkItem.
- Agent Bus docs mention Codex-specific connector files such as `codex_connector.py`, `codex_api.py`, and `codex_mcp.py`, but direct fetches for those files on `main` returned 404.
- The shared `external_agent_adapter.py` and `agent_connectors.py` files named in docs were also not present on the inspected `main` branch.

### 4. Codex Worker To Codex CLI

Status: missing.

Expected minimum behavior:

1. Poll `GET /agents/codex-m2/inbox` or `GET /work-items?owner_agent=codex-m2&status=queued`.
2. Claim the selected WorkItem.
3. Transition it to `in_progress`.
4. Build a Codex prompt from:
   - WorkItem title
   - repository
   - issue number
   - `metadata.objective`
   - `metadata.branch`
   - safety instructions
5. Invoke Codex CLI in a checked-out workspace.
6. Capture command output and failure state.

Missing implementation:

- Codex CLI subprocess invocation.
- Prompt construction.
- Sandbox/workspace management.
- Retry/failure mapping.
- Agent Bus heartbeat updates during execution.

### 5. Codex CLI To Git Commit

Status: missing.

Expected minimum behavior:

1. Clone or update the target repository.
2. Check out `agent-integration`.
3. Apply Codex-generated edits.
4. Run relevant validation.
5. Commit to `agent-integration` or to a configured execution branch, depending on policy.
6. Report commit SHA and changed files to Agent Bus evidence.

Missing implementation:

- Git checkout/fetch logic.
- Branch safety checks.
- Commit creation.
- Push behavior.
- Evidence packet creation with commit SHA.

Policy note:

Current Circuit standing instructions normally require a dedicated `circuit/*` branch and PR, not direct commits to `agent-integration`. The requested ending point says "Codex creates a commit on agent-integration." That is a policy conflict unless Marcus explicitly approves Codex to commit directly to `agent-integration` for this first autonomous execution path.

## Current End-to-End State

The current path can likely reach:

```text
Approved review/writeback event
-> Orchestrator selects agent-ready issue
-> Agent Bus WorkItem is created
-> GitHub issue gets agent-next label and assignment comment
```

The current path does not yet reach:

```text
Agent Bus WorkItem
-> Codex Worker claims work
-> Codex CLI executes
-> Git commit appears on agent-integration
```

## Gap Matrix

| Capability | Implemented | Partially Implemented | Missing |
| --- | --- | --- | --- |
| GitHub issue label recognition | Yes |  |  |
| Oldest eligible issue selection | Yes |  |  |
| Immediate dispatch from `agent-ready` issue event |  |  | Yes |
| Dispatch after approved review/writeback | Yes |  |  |
| Agent Bus WorkItem payload | Yes |  |  |
| Agent Bus WorkItem API | Yes |  |  |
| Agent Bus agent registry/heartbeat API | Yes |  |  |
| Agent Bus inbox/queue API | Yes |  |  |
| Codex-specific connector routes |  | Docs describe them | Executable files missing |
| Codex Worker daemon/loop |  |  | Yes |
| Agent Bus polling by `codex-m2` |  |  | Yes |
| WorkItem claim/start by worker | Agent Bus supports it |  | Worker missing |
| Codex CLI invocation |  |  | Yes |
| Repository checkout/update |  |  | Yes |
| Commit creation |  |  | Yes |
| Evidence packet with commit SHA | Agent Bus supports evidence primitives |  | Worker missing |

## Single Next Code Change Recommendation

Implement the first minimal Codex Worker loop.

Recommended location:

```text
new worker runtime repo or service owned by the Codex worker deployment
```

If it must live in an existing repo first, put it in `jarvis-agent-bus-mcp` only as an operator-run worker script under a clearly separated runtime path, not inside the Agent Bus API service.

Minimum change:

```text
Add a `codex-m2` worker loop that:
1. registers/heartbeats `codex-m2`;
2. polls Agent Bus for queued WorkItems assigned to `codex-m2`;
3. claims one WorkItem;
4. checks out the target repository and `agent-integration`;
5. invokes Codex CLI with the WorkItem objective and branch instructions;
6. commits the resulting changes;
7. transitions the WorkItem and submits evidence with commit SHA, changed files, and commands run.
```

Why this is the single next change:

- Orchestrator already has enough behavior to create the first WorkItem.
- Agent Bus already has enough behavior to store and expose the first WorkItem.
- The first hard break in the autonomous loop is the absence of a running worker that consumes that WorkItem and executes Codex.

Do not start by adding more Orchestrator dispatch logic. That would make the issue-to-WorkItem trigger nicer, but it still would not execute a task. The first end-to-end execution requires a worker that bridges Agent Bus to Codex CLI and Git.

## Validation Plan For The Next Change

After the worker loop exists, validate with a single disposable issue:

1. Register `codex-m2` and confirm it appears in `GET /agents`.
2. Create or dispatch a WorkItem with `owner_agent=codex-m2`.
3. Confirm the worker claims it.
4. Confirm the worker transitions it to `in_progress`.
5. Confirm Codex CLI runs in the target repository.
6. Confirm a commit appears on the approved target branch.
7. Confirm Agent Bus evidence contains:
   - commit SHA
   - branch
   - changed files
   - commands run
   - VERIFIED / ASSUMED / UNVERIFIED

## Evidence Inspected

RiseOS Orchestrator:

- `app/task_dispatch.py` on `agent-integration`
- `app/main.py` on `agent-integration`
- `app/clients/agent_bus.py` on `agent-integration`
- `app/github_writeback.py` on `agent-integration`
- `app/review_queue.py` on `agent-integration`
- `app/review_worker.py` on `agent-integration`

Agent Bus:

- `src/agent_bus_mcp/api.py` on `main`
- `src/agent_bus_mcp/mcp.py` on `main`
- `src/agent_bus_mcp/inbox.py` on `main`
- `src/agent_bus_mcp/models.py` on `main`
- `docs/runtime-worker-setup.md` on `main`
- `docs/external-agent-contract-v1.md` on `main`
- `docs/connector-architecture.md` on `main`
- `docs/repository-navigation.md` on `main`

Searches performed:

- `codex-m2`
- `Codex Worker`
- `AGENT_BUS_BASE_URL`
- `subprocess codex git commit`
- `get_agent_queue claim_work_item Codex`
- `external_agent_adapter`
- `codex_api.py`
- `codex_connector.py`
- worker-specific aliases

Result:

The Orchestrator and Agent Bus queue substrate are present; the worker execution bridge is not.

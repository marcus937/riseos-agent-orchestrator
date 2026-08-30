# First Live Task Test

## Preconditions

1. Deploy RiseOS Orchestrator from the `agent-integration` branch.
2. Deploy Agent Bus with the `POST /work-items` endpoint available.
3. Run the Codex Worker with an Agent Bus registration identity matching `AGENT_BUS_OWNER_AGENT`.
4. Configure Orchestrator on Vultr:

```text
ENABLE_GITHUB_WRITEBACK=true
ENABLE_TASK_DISPATCH=true
ENABLE_AGENT_BUS_DISPATCH=true
AGENT_BUS_BASE_URL=https://agent-bus.riseconnect.us
AGENT_BUS_TOKEN=<existing-agent-bus-token>
AGENT_BUS_TIMEOUT_SECONDS=30
AGENT_BUS_OWNER_AGENT=codex-m2
AGENT_BUS_REVIEW_AGENT=bb2
WORK_BRANCH=agent-integration
```

Do not create a new Agent Bus token for this test. Use the existing deployment secret value for `AGENT_BUS_TOKEN`.

## Agent Bus Reachability Check

After deployment, run this from the Orchestrator host to prove it can reach Agent Bus with the configured token:

```bash
curl -fsS \
  -H "Authorization: Bearer ${AGENT_BUS_TOKEN}" \
  -H "Accept: application/json" \
  "${AGENT_BUS_BASE_URL}/agents"
```

Expected result: JSON containing the current Agent Bus agent registry.

## Test Steps

### 1. Create Issue

Create a GitHub issue in an approved repository with:

- A clear title.
- A task objective in the issue body.
- Labels: `agent-task`, `agent-ready`.

### 2. Mark Agent Ready

If the issue is created without labels, add `agent-task` and `agent-ready`.

Expected Orchestrator behavior:

- Webhook is accepted.
- Slack notification still posts to the configured orchestrator channel.
- The issue remains queued until dispatch runs after an approved review/writeback event.

### 3. Observe Agent Bus WorkItem Creation

Trigger the existing approved-review dispatch path.

Expected Orchestrator behavior:

- Selects the oldest open issue with `agent-task` and `agent-ready`.
- Sends `POST /work-items` to Agent Bus.
- Stores the returned `work_item_id` on the review work item.
- Records `agent_bus_dispatch_started_at` and `agent_bus_dispatch_completed_at`.
- Applies `agent-next` to the GitHub issue.
- Posts the existing `## Circuit Assignment` comment.

Confirm through:

- Agent Bus work item list or database.
- Orchestrator debug review queue item.
- GitHub issue labels and comments.

### 4. Observe Codex Worker Claim

Start or monitor the Codex Worker.

Expected Codex Worker behavior:

- Polls Agent Bus inbox or queue.
- Sees the new WorkItem assigned to `codex-m2`.
- Claims the WorkItem.
- Transitions the WorkItem to claimed or working.

Confirm through:

- Agent Bus WorkItem state.
- Codex Worker logs.

### 5. Observe Execution

Run the Codex Worker in no-PR automation mode until autonomous execution is explicitly enabled.

Expected first live behavior before full autonomy:

- Worker can claim the task.
- Worker can build a task execution plan from metadata.
- Worker should not create a GitHub PR until PR automation is enabled.

Expected later autonomous behavior:

- Worker checks out the target repository and branch.
- Worker creates a dedicated working branch.
- Worker executes Codex.
- Worker reports completion with branch, commit SHA, summary, and result metadata.

## Success Criteria

- One Agent Bus WorkItem exists for the selected GitHub issue.
- The WorkItem has the expected repository, issue number, owner agent, review agent, priority, and metadata.
- The Orchestrator review work item stores the Agent Bus `work_item_id`.
- The GitHub issue still receives the existing `agent-next` label and Circuit Assignment comment.
- The Codex Worker can see and claim the WorkItem.

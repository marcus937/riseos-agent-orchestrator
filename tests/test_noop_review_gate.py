from __future__ import annotations

import anyio

from app.agent_tasks import (
    AgentTaskCreateRequest,
    AgentTaskExecutionResult,
    AgentTaskStatus,
    InMemoryAgentTaskStore,
    create_agent_task,
    mark_agent_task_review_pending,
)
from app.noop_review_gate import finalize_verified_noop_review, is_verified_noop_execution


class FakeAgentBusClient:
    def __init__(self) -> None:
        self.claimed: list[str] = []
        self.reviews: list[dict] = []

    async def claim_review_request(self, work_item_id: str, *, reviewer: str, actor: str) -> dict:
        self.claimed.append(work_item_id)
        return {"work_item_id": work_item_id, "status": "claimed"}

    async def submit_bb2_review(self, payload: dict) -> dict:
        self.reviews.append(payload)
        return {"review_packet_id": "packet-noop"}

    async def get_work_item(self, work_item_id: str) -> dict:
        return {
            "work_item": {
                "work_item_id": work_item_id,
                "status": "completed",
                "metadata": {
                    "source_review_decision": "approved",
                    "source_review_packet_id": "packet-noop",
                },
            }
        }


def _payload(**overrides) -> AgentTaskExecutionResult:
    values = {
        "agent_id": "codex-m2",
        "status": "completed",
        "commit_sha": None,
        "changed_files": [],
        "evidence": {
            "execution_type": "no_op",
            "no_op": True,
            "success": True,
            "codex_exit_code": 0,
            "codex_timed_out": False,
            "push_success": False,
            "pr_number": None,
            "pull_request": None,
            "review_dispatch": {"evidence_packet_id": "evidence-noop"},
        },
    }
    values.update(overrides)
    return AgentTaskExecutionResult(**values)


def test_verified_noop_requires_complete_fail_closed_evidence() -> None:
    assert is_verified_noop_execution(_payload()) is True
    assert is_verified_noop_execution(_payload(commit_sha="abc123")) is False
    assert is_verified_noop_execution(_payload(changed_files=["src/app.ts"])) is False
    assert is_verified_noop_execution(_payload(evidence={"no_op": True})) is False


def test_verified_noop_accepts_unchanged_existing_pull_request() -> None:
    payload = _payload(
        evidence={
            "execution_type": "no_op",
            "no_op": True,
            "success": True,
            "codex_exit_code": 0,
            "codex_timed_out": False,
            "push_success": False,
            "pr_number": 209,
            "pull_request": {"status": "existing", "number": 209},
            "review_dispatch": {"evidence_packet_id": "evidence-existing-pr"},
        }
    )

    assert is_verified_noop_execution(payload) is True


def test_verified_noop_rejects_created_or_mismatched_pull_request() -> None:
    base_evidence = _payload().evidence

    created = {**base_evidence, "pr_number": 209, "pull_request": {"status": "created", "number": 209}}
    mismatched = {**base_evidence, "pr_number": 209, "pull_request": {"status": "existing", "number": 210}}

    assert is_verified_noop_execution(_payload(evidence=created)) is False
    assert is_verified_noop_execution(_payload(evidence=mismatched)) is False


def test_verified_noop_completes_review_envelope_and_task() -> None:
    task = create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/jarvis-codex-worker",
            title="No-op verification",
            objective="Verify the no-op path.",
            target_agent="codex-m2",
        )
    )
    task.agent_bus_work_item_id = "implementation-noop"
    task.execution_evidence = {"agent_bus_review_work_item_id": "review-noop"}
    mark_agent_task_review_pending(task, review_work_item_id="review-noop")
    store = InMemoryAgentTaskStore()
    store.save_agent_task(task)
    bus = FakeAgentBusClient()

    async def run() -> bool:
        return await finalize_verified_noop_review(
            task,
            _payload(),
            bus,
            reviewer="bb2",
            store=store,
        )

    assert anyio.run(run) is True
    saved = store.get_agent_task(task.task_id)
    assert saved is not None
    assert saved.status == AgentTaskStatus.COMPLETED
    assert bus.claimed == ["review-noop"]
    assert bus.reviews[0]["decision"] == "approved"
    assert bus.reviews[0]["metadata"]["review_mode"] == "verified_noop"
    assert bus.reviews[0]["evidence_packet_ids_reviewed"] == ["evidence-noop"]


def test_non_noop_never_claims_or_submits_review() -> None:
    task = create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/jarvis-codex-worker",
            title="Real change",
            objective="Implement a real change.",
            target_agent="codex-m2",
        )
    )
    store = InMemoryAgentTaskStore()
    bus = FakeAgentBusClient()

    async def run() -> bool:
        return await finalize_verified_noop_review(
            task,
            _payload(commit_sha="abc123", changed_files=["src/app.ts"]),
            bus,
            reviewer="bb2",
            store=store,
        )

    assert anyio.run(run) is False
    assert bus.claimed == []
    assert bus.reviews == []

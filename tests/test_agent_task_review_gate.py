from __future__ import annotations

import anyio

from app.agent_task_review_gate import finalize_review_gated_agent_task
from app.agent_tasks import (
    AgentTaskCreateRequest,
    AgentTaskStatus,
    InMemoryAgentTaskStore,
    create_agent_task,
    mark_agent_task_review_pending,
    refresh_agent_task_dependency_states,
)
from app.config import Settings
from app.github_events import GitHubEventType
from app.review_queue import ReviewProcessResponse, ReviewWorkItem
from app.reviewer.decision import ReviewDecision, ReviewDecisionType, RiskLevel


class FakeAgentBusClient:
    def __init__(self, decision: str = "approved") -> None:
        self.decision = decision
        self.claimed: list[str] = []
        self.reviews: list[dict] = []
        self.created: list[dict] = []

    async def claim_review_request(
        self, work_item_id: str, *, reviewer: str, actor: str
    ) -> dict:
        self.claimed.append(work_item_id)
        return {"work_item_id": work_item_id, "status": "claimed"}

    async def submit_bb2_review(self, payload: dict) -> dict:
        self.reviews.append(payload)
        return {"review_packet_id": "packet-1"}

    async def get_work_item(self, work_item_id: str) -> dict:
        if work_item_id == "review-1":
            return {
                "work_item": {
                    "work_item_id": work_item_id,
                    "status": "completed",
                    "metadata": {
                        "source_review_decision": self.decision,
                        "source_review_packet_id": "packet-1",
                    },
                }
            }
        return {"work_item_id": work_item_id, "status": "queued"}

    async def create_work_item(self, payload: dict) -> dict:
        self.created.append(payload)
        return {"work_item_id": "implementation-2", "status": "queued"}


def _response(decision: ReviewDecisionType) -> ReviewProcessResponse:
    item = ReviewWorkItem(
        id="orchestrator-review-1",
        created_at="2026-08-31T00:00:00Z",
        repo_full_name="marcus937/jarvis-mission-control",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="codex-m2/contract-alignment",
        commit_sha="abc123",
        pr_number=202,
        runtime_validation_id="hermes-1",
        runtime_validation_status="completed",
    )
    return ReviewProcessResponse(
        work_item=item,
        decision=ReviewDecision(
            decision=decision,
            confidence=0.99,
            risk_level=RiskLevel.LOW,
            summary="Review complete.",
            required_changes=[]
            if decision == ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW
            else ["Fix the contract."],
            next_task_prompt=None,
            human_review_required=True,
        ),
        intended_next_actions=[],
        changed_files=["src/contracts.ts"],
        github_context_available=True,
    )


def _store() -> tuple[InMemoryAgentTaskStore, str, str]:
    store = InMemoryAgentTaskStore()
    first = create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/jarvis-mission-control",
            title="Step one",
            objective="Align the first contract.",
            target_agent="codex-m2",
            correlation_id="wf-review-gate",
        )
    )
    first.branch = "codex-m2/contract-alignment"
    first.commit_sha = "abc123"
    first.execution_evidence = {
        "agent_bus_review_work_item_id": "review-1",
        "evidence_packet_id": "evidence-1",
    }
    mark_agent_task_review_pending(first, review_work_item_id="review-1")

    second = create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/jarvis-mission-control",
            title="Step two",
            objective="Align the second contract.",
            target_agent="codex-m2",
            correlation_id="wf-review-gate",
            dependency_task_ids=[first.task_id],
        )
    )
    for task in refresh_agent_task_dependency_states([first, second]):
        store.save_agent_task(task)
    return store, first.task_id, second.task_id


def test_approved_review_completes_current_task_and_releases_successor() -> None:
    store, first_id, second_id = _store()
    bus = FakeAgentBusClient()

    async def run() -> bool:
        return await finalize_review_gated_agent_task(
            _response(ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW),
            Settings(),
            store=store,
            agent_bus_client=bus,
        )

    assert anyio.run(run) is True
    assert store.get_agent_task(first_id).status == AgentTaskStatus.COMPLETED
    assert store.get_agent_task(second_id).status == AgentTaskStatus.ASSIGNED
    assert bus.claimed == ["review-1"]
    assert bus.reviews[0]["decision"] == "approved"
    assert len(bus.created) == 1


def test_changes_requested_keeps_successor_blocked() -> None:
    store, first_id, second_id = _store()
    bus = FakeAgentBusClient(decision="needs_changes")

    async def run() -> bool:
        return await finalize_review_gated_agent_task(
            _response(ReviewDecisionType.NEEDS_CHANGES),
            Settings(),
            store=store,
            agent_bus_client=bus,
        )

    assert anyio.run(run) is True
    assert store.get_agent_task(first_id).status == AgentTaskStatus.READY_FOR_REVIEW
    assert store.get_agent_task(second_id).blocked is True
    assert bus.reviews[0]["decision"] == "needs_changes"
    assert bus.created == []

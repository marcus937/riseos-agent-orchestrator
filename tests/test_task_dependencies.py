import asyncio
from typing import Any

from app.task_dependencies import (
    dependencies_satisfied,
    dependency_complete,
    dependency_state_for_issue,
    parse_issue_dependencies,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeDependencyClient:
    def __init__(self, issues: dict[int, dict[str, Any]]) -> None:
        self.issues = issues

    async def fetch_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        return self.issues[issue_number]


def issue(*, state: str = "open", labels: list[str] | None = None) -> dict[str, Any]:
    return {"state": state, "labels": [{"name": label} for label in (labels or [])]}


def test_no_dependencies_are_satisfied() -> None:
    client = FakeDependencyClient({})

    state = run(dependency_state_for_issue("riseos/example", 12, "Do the thing.", client))

    assert state.dependency_count == 0
    assert state.dependencies_satisfied is True
    assert state.blocked_by == []


def test_single_predecessor_complete() -> None:
    client = FakeDependencyClient({72: issue(labels=["bb2-approved"])})

    satisfied = run(dependencies_satisfied("riseos/example", 12, "predecessor_issue: 72", client))

    assert satisfied is True


def test_single_predecessor_incomplete() -> None:
    client = FakeDependencyClient({72: issue()})

    state = run(dependency_state_for_issue("riseos/example", 12, "predecessor_issue: 72", client))

    assert state.dependency_count == 1
    assert state.dependencies_satisfied is False
    assert state.blocked_by == [72]


def test_multiple_predecessors_require_all_complete() -> None:
    client = FakeDependencyClient(
        {
            70: issue(state="closed"),
            71: issue(labels=["ready-to-merge"]),
            72: issue(),
        }
    )

    state = run(
        dependency_state_for_issue(
            "riseos/example",
            12,
            "depends_on:\n  - issue:70\n  - issue:71\n  - issue:72\npredecessor_issue: 99",
            client,
        )
    )

    assert state.dependency_count == 3
    assert state.dependencies_satisfied is False
    assert state.blocked_by == [72]


def test_malformed_metadata_never_throws() -> None:
    dependencies = parse_issue_dependencies("depends_on:\n  - issue: nope\npredecessor_issue: 72")

    assert dependencies.predecessor_issue_ids == []


def test_closed_predecessor_is_complete() -> None:
    assert dependency_complete(issue(state="closed")) is True


def test_bb2_approved_predecessor_is_complete() -> None:
    assert dependency_complete(issue(labels=["bb2-approved"])) is True


def test_ready_to_merge_predecessor_is_complete() -> None:
    assert dependency_complete(issue(labels=["ready-to-merge"])) is True

from __future__ import annotations

import re
from typing import Any, Protocol

from pydantic import BaseModel, Field


LABEL_BB2_APPROVED = "bb2-approved"
LABEL_READY_TO_MERGE = "ready-to-merge"


class IssueDependencies(BaseModel):
    predecessor_issue_ids: list[int] = Field(default_factory=list)


class DependencyState(BaseModel):
    dependency_count: int = 0
    dependencies_satisfied: bool = True
    blocked_by: list[int] = Field(default_factory=list)


class DependencyIssueClient(Protocol):
    async def fetch_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        ...


def parse_issue_dependencies(body: str | None) -> IssueDependencies:
    if not body:
        return IssueDependencies()
    try:
        if _contains_depends_on(body):
            return IssueDependencies(predecessor_issue_ids=_unique_issue_ids(_parse_depends_on(body)))
        return IssueDependencies(predecessor_issue_ids=_unique_issue_ids(_parse_predecessor_issue(body)))
    except Exception:
        return IssueDependencies()


def dependency_complete(issue: dict[str, Any]) -> bool:
    labels = _label_names(issue.get("labels"))
    if {LABEL_BB2_APPROVED, LABEL_READY_TO_MERGE}.issubset(labels):
        return True
    return _linked_pr_ready_to_merge(issue)


async def dependency_state_for_issue(
    repo_full_name: str,
    issue_number: int,
    body: str | None,
    client: DependencyIssueClient,
) -> DependencyState:
    return await _dependency_state_for_issue(
        repo_full_name,
        issue_number,
        body,
        client,
        visiting={issue_number},
    )


async def _dependency_state_for_issue(
    repo_full_name: str,
    issue_number: int,
    body: str | None,
    client: DependencyIssueClient,
    *,
    visiting: set[int],
) -> DependencyState:
    predecessor_ids = parse_issue_dependencies(body).predecessor_issue_ids
    if not predecessor_ids:
        return DependencyState()

    blocked_by: list[int] = []
    for predecessor_id in predecessor_ids:
        if predecessor_id in visiting:
            blocked_by.append(predecessor_id)
            continue
        try:
            predecessor = await client.fetch_issue(repo_full_name, predecessor_id)
        except Exception:
            blocked_by.append(predecessor_id)
            continue
        predecessor_state = await _dependency_state_for_issue(
            repo_full_name,
            predecessor_id,
            predecessor.get("body") if isinstance(predecessor.get("body"), str) else None,
            client,
            visiting={*visiting, predecessor_id},
        )
        if predecessor_state.blocked_by:
            blocked_by.extend(predecessor_state.blocked_by)
        if not predecessor_state.dependencies_satisfied or not dependency_complete(predecessor):
            blocked_by.append(predecessor_id)

    blocked_by = _unique_issue_ids(blocked_by)
    return DependencyState(
        dependency_count=len(predecessor_ids),
        dependencies_satisfied=not blocked_by,
        blocked_by=blocked_by,
    )


async def dependencies_satisfied(
    repo_full_name: str,
    issue_number: int,
    body: str | None,
    client: DependencyIssueClient,
) -> bool:
    state = await dependency_state_for_issue(repo_full_name, issue_number, body, client)
    return state.dependencies_satisfied


def _contains_depends_on(body: str) -> bool:
    return re.search(r"(?im)^\s*depends_on\s*:", body) is not None


def _parse_depends_on(body: str) -> list[int]:
    issue_ids: list[int] = []
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^\s*depends_on\s*:", line):
            continue
        base_indent = len(line) - len(line.lstrip())
        for nested in lines[index + 1 :]:
            if not nested.strip():
                continue
            nested_indent = len(nested) - len(nested.lstrip())
            if nested_indent <= base_indent and not nested.lstrip().startswith("-"):
                break
            issue_ids.extend(int(match) for match in re.findall(r"\bissue\s*:\s*(\d+)\b", nested))
        break
    return issue_ids


def _parse_predecessor_issue(body: str) -> list[int]:
    return [int(match) for match in re.findall(r"(?im)^\s*predecessor_issue\s*:\s*(\d+)\b", body)]


def _unique_issue_ids(issue_ids: list[int]) -> list[int]:
    unique: list[int] = []
    seen: set[int] = set()
    for issue_id in issue_ids:
        if issue_id < 1 or issue_id in seen:
            continue
        seen.add(issue_id)
        unique.append(issue_id)
    return unique


def _label_names(raw_labels: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(raw_labels, list):
        return names
    for label in raw_labels:
        if isinstance(label, str):
            names.add(label)
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            names.add(label["name"])
    return names


def _linked_pr_ready_to_merge(issue: dict[str, Any]) -> bool:
    linked_prs = issue.get("linked_pull_requests") or issue.get("linked_prs") or issue.get("pull_requests")
    if not isinstance(linked_prs, list):
        return False
    for linked_pr in linked_prs:
        if isinstance(linked_pr, dict) and LABEL_READY_TO_MERGE in _label_names(linked_pr.get("labels")):
            return True
    return False

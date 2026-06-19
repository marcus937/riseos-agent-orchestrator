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
    if str(issue.get("state") or "").lower() == "closed":
        return True
    labels = _label_names(issue.get("labels"))
    return bool({LABEL_BB2_APPROVED, LABEL_READY_TO_MERGE} & labels)


async def dependency_state_for_issue(
    repo_full_name: str,
    issue_number: int,
    body: str | None,
    client: DependencyIssueClient,
) -> DependencyState:
    dependencies = parse_issue_dependencies(body)
    predecessor_ids = dependencies.predecessor_issue_ids
    if not predecessor_ids:
        return DependencyState()

    blocked_by: list[int] = []
    for predecessor_id in predecessor_ids:
        try:
            predecessor = await client.fetch_issue(repo_full_name, predecessor_id)
        except Exception:
            blocked_by.append(predecessor_id)
            continue
        if not dependency_complete(predecessor):
            blocked_by.append(predecessor_id)

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

from enum import StrEnum


class TaskState(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    WORKING = "working"
    REVIEW_NEEDED = "review_needed"
    NEEDS_CHANGES = "needs_changes"
    APPROVED_FOR_HUMAN_REVIEW = "approved_for_human_review"
    BLOCKED = "blocked"
    DONE = "done"


def transition_task_state(current_state: TaskState, event: str | None) -> TaskState:
    if event == "review_needed":
        return TaskState.REVIEW_NEEDED
    if event == "needs_changes":
        return TaskState.NEEDS_CHANGES
    if event == "approved_for_human_review":
        return TaskState.APPROVED_FOR_HUMAN_REVIEW
    if event == "blocked":
        return TaskState.BLOCKED
    if event == "done":
        return TaskState.DONE
    return current_state

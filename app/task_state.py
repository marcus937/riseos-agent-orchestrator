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

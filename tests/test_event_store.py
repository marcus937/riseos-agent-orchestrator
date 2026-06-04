from app.event_store import event_record_from_parsed
from app.github_events import parse_github_event


def test_event_record_from_push_event() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
            "sender": {"login": "agent"},
        },
    )

    record = event_record_from_parsed(parsed)

    assert record.event_id
    assert record.github_event == "push"
    assert record.repo_full_name == "riseos/example"
    assert record.branch == "agent-integration"
    assert record.commit_sha == "abc123"
    assert record.issue_number is None
    assert record.pr_number is None
    assert record.raw_action is None


def test_event_record_from_issue_comment_event() -> None:
    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "riseos/example"},
            "issue": {"number": 12},
            "comment": {"body": "Status: Done"},
        },
    )

    record = event_record_from_parsed(parsed)

    assert record.github_event == "issue_comment"
    assert record.repo_full_name == "riseos/example"
    assert record.issue_number == 12
    assert record.pr_number is None
    assert record.raw_action == "created"


def test_event_record_from_pull_request_event() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "synchronize",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {
                "number": 7,
                "head": {"ref": "agent-integration", "sha": "def456"},
            },
        },
    )

    record = event_record_from_parsed(parsed)

    assert record.github_event == "pull_request"
    assert record.branch == "agent-integration"
    assert record.commit_sha == "def456"
    assert record.pr_number == 7
    assert record.raw_action == "synchronize"

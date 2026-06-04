from app.github_events import extract_commit_sha_from_comment, parse_github_event


def test_extract_commit_sha_from_comment_supports_expected_labels() -> None:
    formats = [
        ("Commit", "11ff7f7fad1b5c563e42f143eb9523c3126974cf"),
        ("commit", "abc1234"),
        ("SHA", "deadbee"),
        ("sha", "feed123"),
        ("Commit SHA", "c0ffee1"),
        ("commit_sha", "badcafe"),
    ]

    for label, expected_sha in formats:
        body = f"Status: Done\n{label}: {expected_sha}\nSummary: Ready."
        assert extract_commit_sha_from_comment(body) == expected_sha


def test_extract_commit_sha_from_comment_returns_first_valid_match() -> None:
    body = "Status: Done\nCommit: abc1234\nSHA: deadbee"

    assert extract_commit_sha_from_comment(body) == "abc1234"


def test_extract_commit_sha_from_comment_ignores_non_hex_values() -> None:
    body = "Status: Done\nCommit: not-a-sha"

    assert extract_commit_sha_from_comment(body) is None


def test_extract_commit_sha_from_comment_accepts_first_40_hex_characters() -> None:
    body = "Status: Done\nCommit: 11ff7f7fad1b5c563e42f143eb9523c3126974cf5"

    assert extract_commit_sha_from_comment(body) == "11ff7f7fad1b5c563e42f143eb9523c3126974cf"


def test_issue_comment_parser_sets_head_sha_from_commit_full_sha() -> None:
    commit_sha = "11ff7f7fad1b5c563e42f143eb9523c3126974cf"
    body = f"Status: Done\nCommit: {commit_sha}\nSummary: Testing commit SHA extraction."

    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "riseos/example"},
            "sender": {"login": "agent"},
            "issue": {"number": 12},
            "comment": {"body": body},
        },
    )

    assert parsed.comment_body == body
    assert parsed.head_sha == commit_sha


def test_issue_comment_parser_sets_head_sha_from_lowercase_commit_short_sha() -> None:
    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "riseos/example"},
            "sender": {"login": "agent"},
            "issue": {"number": 12},
            "comment": {"body": "Status: Done\ncommit: abc1234"},
        },
    )

    assert parsed.head_sha == "abc1234"


def test_issue_comment_parser_sets_head_sha_from_commit_sha_label() -> None:
    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "riseos/example"},
            "sender": {"login": "agent"},
            "issue": {"number": 12},
            "comment": {"body": "Status: Done\ncommit_sha: deadbee"},
        },
    )

    assert parsed.head_sha == "deadbee"


def test_issue_comment_parser_ignores_invalid_commit_sha_but_preserves_body() -> None:
    body = "Status: Done\nCommit: not-a-sha"

    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "riseos/example"},
            "sender": {"login": "agent"},
            "issue": {"number": 12},
            "comment": {"body": body},
        },
    )

    assert parsed.comment_body == body
    assert parsed.head_sha is None

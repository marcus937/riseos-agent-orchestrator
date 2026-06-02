from pathlib import Path

from app.reviewer.context_loader import load_bb_architect_context


def test_context_loader_includes_global_context() -> None:
    context = load_bb_architect_context("marcus937/unknown-repo")

    assert "BB is the Project Jarvis architect and reviewer" in context
    assert "Review Rubric" in context
    assert "Branch Policy" in context


def test_context_loader_selects_correct_repo_profile() -> None:
    context = load_bb_architect_context("marcus937/jarvis-mission-control")

    assert "Jarvis Mission Control Repo Profile" in context
    assert "frontend operational dashboard" in context


def test_unknown_repo_still_gets_global_context() -> None:
    context = load_bb_architect_context("marcus937/not-a-known-repo")

    assert "BB Architect System Prompt" in context
    assert "Repo Profile" not in context


def test_max_char_limit_truncates_safely() -> None:
    context = load_bb_architect_context("marcus937/Project-Jarvis", max_chars=160)

    assert len(context) <= 160
    assert "truncated" in context


def test_missing_context_files_fail_soft(tmp_path: Path) -> None:
    context = load_bb_architect_context(
        "marcus937/Project-Jarvis",
        context_dir=tmp_path,
    )

    assert "Missing context file: bb_architect_system_prompt.md" in context
    assert "Missing context file: repo_profiles/project_jarvis.md" in context

from pathlib import Path


GLOBAL_CONTEXT_FILES = (
    "bb_architect_system_prompt.md",
    "review_rubric.md",
    "branch_policy.md",
)

REPO_PROFILE_FILES = {
    "project-jarvis": "project_jarvis.md",
    "jarvis": "project_jarvis.md",
    "jarvis-mission-control": "jarvis_mission_control.md",
    "marketing-leadership-dashboard": "marketing_leadership_dashboard.md",
    "rylinn-field-app": "rylinn_field_app.md",
    "riseos-ui-system": "riseos_ui_system.md",
}

DEFAULT_CONTEXT_DIR = Path(__file__).resolve().parents[2] / "context"
TRUNCATION_NOTICE = "\n\n[BB context pack truncated to configured BB_CONTEXT_MAX_CHARS limit.]"


def load_bb_architect_context(
    repo_full_name: str | None,
    *,
    max_chars: int = 20000,
    context_dir: Path | None = None,
) -> str:
    context_root = context_dir or DEFAULT_CONTEXT_DIR
    sections: list[str] = []

    for filename in GLOBAL_CONTEXT_FILES:
        sections.append(_load_section(context_root / filename, filename))

    profile_file = _profile_file_for_repo(repo_full_name)
    if profile_file:
        sections.append(_load_section(context_root / "repo_profiles" / profile_file, f"repo_profiles/{profile_file}"))

    context = "\n\n".join(section for section in sections if section.strip())
    return _truncate_context(context, max_chars)


def _profile_file_for_repo(repo_full_name: str | None) -> str | None:
    if not repo_full_name:
        return None

    repo_name = repo_full_name.rsplit("/", 1)[-1].lower().replace("_", "-")
    return REPO_PROFILE_FILES.get(repo_name)


def _load_section(path: Path, label: str) -> str:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return f"## Missing context file: {label}\nThis context file was not available; continue using remaining context."
    if not content:
        return f"## Empty context file: {label}\nThis context file was empty; continue using remaining context."
    return content


def _truncate_context(context: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(context) <= max_chars:
        return context

    notice = TRUNCATION_NOTICE
    if max_chars <= len(notice):
        return notice[:max_chars]
    return context[: max_chars - len(notice)].rstrip() + notice

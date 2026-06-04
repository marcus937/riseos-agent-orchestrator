from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


_FAILED_TEST_RE = re.compile(r"^FAILED\s+([^\s]+)", re.MULTILINE)
_ERROR_TYPE_RE = re.compile(r"\b([A-Z][A-Za-z_]*(?:Error|Exception))\b")


def parse_pytest_output(output: str) -> tuple[list[str], str | None]:
    failed_tests = []
    for match in _FAILED_TEST_RE.finditer(output):
        failed_tests.append(match.group(1).split("::")[-1])

    error_type = None
    for line in output.splitlines():
        if line.startswith("E   ") or line.startswith(">"):
            type_match = _ERROR_TYPE_RE.search(line)
            if type_match:
                error_type = type_match.group(1)
                break
    if error_type is None:
        type_match = _ERROR_TYPE_RE.search(output)
        if type_match:
            error_type = type_match.group(1)

    return failed_tests, error_type


def build_failure_summary(
    *,
    workflow: str,
    status: str,
    pytest_output: str = "",
    exception_text: str | None = None,
    diagnostics_output: str | None = None,
) -> dict[str, Any]:
    failed_tests, error_type = parse_pytest_output(pytest_output)
    if error_type is None and exception_text:
        match = _ERROR_TYPE_RE.search(exception_text)
        error_type = match.group(1) if match else None

    return {
        "workflow": workflow,
        "status": status,
        "failed_tests": failed_tests,
        "error_type": error_type,
        "has_pytest_output": bool(pytest_output.strip()),
        "has_exception_text": bool((exception_text or "").strip()),
        "has_diagnostics_output": bool((diagnostics_output or "").strip()),
    }


def build_lifecycle_failure_artifact(
    *,
    review_queue_state: Any,
    worker_state: Any,
    lifecycle_state: Any,
    exception_text: str | None,
    diagnostics_output: str | None,
) -> dict[str, Any]:
    return {
        "review_queue_state": review_queue_state,
        "worker_state": worker_state,
        "lifecycle_state": lifecycle_state,
        "exception_text": exception_text or "",
        "diagnostics_output": diagnostics_output or "",
    }


def write_actions_repair_artifacts(
    *,
    output_dir: Path,
    workflow: str,
    status: str,
    pytest_output: str = "",
    review_queue_state: Any = None,
    worker_state: Any = None,
    lifecycle_state: Any = None,
    exception_text: str | None = None,
    diagnostics_output: str | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = build_failure_summary(
        workflow=workflow,
        status=status,
        pytest_output=pytest_output,
        exception_text=exception_text,
        diagnostics_output=diagnostics_output,
    )
    summary_json = output_dir / "failure-summary.json"
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary_md = output_dir / "failure-summary.md"
    summary_md.write_text(_render_summary_markdown(summary), encoding="utf-8")

    written = {"failure_summary_json": summary_json, "failure_summary_markdown": summary_md}
    if workflow == "bb2-lifecycle-validation":
        lifecycle_json = output_dir / "bb2-lifecycle-failure.json"
        lifecycle_json.write_text(
            json.dumps(
                build_lifecycle_failure_artifact(
                    review_queue_state=review_queue_state,
                    worker_state=worker_state,
                    lifecycle_state=lifecycle_state,
                    exception_text=exception_text,
                    diagnostics_output=diagnostics_output,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        written["bb2_lifecycle_failure_json"] = lifecycle_json

    return written


def _render_summary_markdown(summary: dict[str, Any]) -> str:
    failed_tests = summary["failed_tests"] or ["None detected"]
    lines = [
        "# Actions Repair Failure Summary",
        "",
        f"- Workflow: {summary['workflow']}",
        f"- Status: {summary['status']}",
        f"- Error type: {summary['error_type'] or 'Unknown'}",
        f"- Pytest output captured: {summary['has_pytest_output']}",
        f"- Exception text captured: {summary['has_exception_text']}",
        f"- Diagnostics output captured: {summary['has_diagnostics_output']}",
        "",
        "## Failed Tests",
        "",
    ]
    lines.extend(f"- {test}" for test in failed_tests)
    return "\n".join(lines) + "\n"


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.exists():
        return ""
    return file_path.read_text(encoding="utf-8", errors="replace")


def _read_json(path: str | None) -> Any:
    text = _read_text(path)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic GitHub Actions repair artifacts.")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--status", default="failed")
    parser.add_argument("--output-dir", default="actions-repair-artifacts")
    parser.add_argument("--pytest-output")
    parser.add_argument("--review-queue-state")
    parser.add_argument("--worker-state")
    parser.add_argument("--lifecycle-state")
    parser.add_argument("--exception-text")
    parser.add_argument("--diagnostics-output")
    args = parser.parse_args()

    write_actions_repair_artifacts(
        output_dir=Path(args.output_dir),
        workflow=args.workflow,
        status=args.status,
        pytest_output=_read_text(args.pytest_output),
        review_queue_state=_read_json(args.review_queue_state),
        worker_state=_read_json(args.worker_state),
        lifecycle_state=_read_json(args.lifecycle_state),
        exception_text=_read_text(args.exception_text),
        diagnostics_output=_read_text(args.diagnostics_output),
    )


if __name__ == "__main__":
    main()

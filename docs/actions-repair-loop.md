# Actions Repair Loop

The Actions repair loop gives Circuit and BB2 deterministic failure artifacts whenever CI or lifecycle validation fails. The intent is to make the first repair pass possible from artifacts alone, without manually scanning raw Actions logs.

## Artifact Locations

GitHub Actions uploads artifacts from the failed workflow run:

- `ci-repair-artifacts` from the `CI` workflow.
- `bb2-lifecycle-repair-artifacts` from the `BB2 Lifecycle Validation` workflow.

Each artifact bundle is stored under `actions-repair-artifacts/` during the job before upload.

## Workflow Outputs

The CI workflow captures:

- `pytest-output.txt`
- `failure-summary.json`
- `failure-summary.md`

The BB2 lifecycle validation workflow captures:

- `pytest-output.txt`
- `failure-summary.json`
- `failure-summary.md`
- `bb2-lifecycle-failure.json`
- `review-queue-state.json`
- `worker-state.json`
- `lifecycle-state.json`
- `exception.txt`
- `diagnostics-output.txt`

`failure-summary.json` is the primary machine-readable entry point. It contains the workflow name, failed status, detected failed tests, detected error type, and booleans showing which supporting diagnostics were captured.

`bb2-lifecycle-failure.json` preserves the lifecycle-specific repair packet: review queue state, worker state, lifecycle state, exception text, and diagnostics output.

## Circuit Troubleshooting Process

1. Open the failed workflow run and download the repair artifact bundle.
2. Read `failure-summary.json` first.
3. Use `failed_tests` to scope the initial repair and `error_type` to classify the failure.
4. For BB2 lifecycle failures, read `bb2-lifecycle-failure.json` before reading raw logs.
5. Use `pytest-output.txt` only when the structured summary does not provide enough context.
6. Make the smallest coherent repair on a `circuit/*` branch, rerun available tests, and keep merge/deploy restrictions intact.

## Expected Repair Signals

The repair loop should provide enough detail for a failed run to identify:

- Which workflow failed.
- Which tests failed when pytest emitted a failed-test list.
- The likely exception or assertion class.
- Whether lifecycle state and diagnostics were captured.
- Where to find the complete pytest output and lifecycle repair packet.

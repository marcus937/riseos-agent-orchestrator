# Runtime Validation

The runtime smoke validation workflow boots the RiseOS Agent Orchestrator inside GitHub Actions and verifies real HTTP behavior without deploying, touching production, using SSH, modifying infrastructure, or requiring Hermes, Vultr, or DGX access.

## Workflow Purpose

`.github/workflows/runtime-smoke-validation.yml` is an isolated staging-style runtime check for pull requests and manual dispatches. It complements unit tests and BB2 lifecycle validation by confirming that the FastAPI application can start, become ready, respond to diagnostics routes, expose the review queue, and shut down cleanly inside an Actions runner.

The workflow is read-only with `contents: read` permissions. It does not merge, deploy, close issues, mutate branches, write repository files, or call external production systems.

## Execution Flow

1. Check out the repository with persisted credentials disabled.
2. Set up Python 3.11 and install the project with development dependencies.
3. Create the `runtime-validation-artifacts/` directory tree.
4. Run deterministic helper tests in `tests/test_runtime_validation.py`.
5. Start `uvicorn app.main:app` on `127.0.0.1:8000` with CI-safe feature flags:
   - OpenAI review disabled.
   - GitHub context hydration disabled.
   - GitHub writeback disabled.
   - task dispatch disabled.
   - debug read token enforcement disabled.
6. Wait for `/health` readiness through `python -m app.runtime_validation`.
7. Execute HTTP smoke requests against:
   - `/health`
   - `/debug/health`
   - `/debug/review-queue`
8. Write every HTTP response to an artifact file.
9. Send `SIGTERM` to the local app process and verify it exits before completing the job.
10. Upload artifacts on every run, including failures.

## Artifact Locations

The workflow uploads one artifact bundle named `runtime-smoke-validation-artifacts`.

Inside the bundle:

- `startup-readiness.json` records the readiness probe result.
- `http-responses/health.json` records the `/health` response.
- `http-responses/diagnostics.json` records the `/debug/health` response.
- `http-responses/review_queue.json` records the `/debug/review-queue` response.
- `failure-summary.md` summarizes failed HTTP checks, or confirms that all smoke checks passed.
- `graceful-shutdown.txt` records whether the app exited after `SIGTERM`.
- `logs/application.log` captures application stdout.
- `logs/startup.log` captures uvicorn startup and stderr output.
- `runtime-validation-helper-results.xml` records the deterministic helper test results.

## Circuit Runtime Validation API

Circuit can trigger and retrieve compact runtime validation results through Orchestrator-owned API routes. All routes require `X-Orchestrator-Admin-Token` matching `ORCHESTRATOR_ADMIN_TOKEN`.

Routes:

```text
POST /api/v1/runtime-validations
GET  /api/v1/runtime-validations/{validation_id}
GET  /api/v1/runtime-validations/{validation_id}/evidence
GET  /api/v1/runtime-validations/{validation_id}/bb2-packet
```

Minimum trigger request:

```json
{
  "repo": "marcus937/jarvis-mission-control",
  "issue_number": 43,
  "pr_number": 38,
  "branch": "agent-integration",
  "target_url": "https://jarvis-mission-control-gules.vercel.app",
  "validation_type": "playwright",
  "requested_by": "circuit"
}
```

The trigger path uses Hermes M2 settings: `HERMES_M2_BASE_URL`, `HERMES_M2_TOKEN`, and `HERMES_M2_ENABLE_DISPATCH=true`.

Allowed runtime targets are restricted to trusted Vercel preview hosts (`vercel.app` or `*.vercel.app`) or the exact configured `HERMES_DEFAULT_TARGET` host. The API rejects missing targets, non-HTTP(S) URLs, credential-bearing URLs, localhost/local-only hosts, private IP literals, and hostnames whose DNS results include private, loopback, link-local, reserved, or multicast addresses.

The response stores a compact result in process memory under `validation_id`. It includes:

- `hermes`: job ID, target URL, target source, Hermes status, manifest fetch status, bundle fetch status, and redacted errors.
- `evidence`: page title, final URL, HTTP status, viewport, user agent, load duration, console counts, network counts, screenshot presence, and artifact filename/size/SHA metadata.
- `bb2`: packet visibility with `packet_created`, `review_requested`, `review_status`, and a field propagation matrix showing which evidence fields were hydrated.

The API returns summaries only. It does not expose raw secrets, raw headers, unbounded console or network bodies, or raw artifact content. Evidence values and errors reuse the public Circuit Hermes adapter redaction path before returning to Circuit.

## Troubleshooting Guidance

If startup fails before HTTP checks run, inspect `logs/startup.log` first. Import errors, port binding failures, dependency installation issues, and FastAPI startup exceptions usually appear there.

If readiness times out, inspect `startup-readiness.json` and `logs/startup.log`. A missing `200` from `/health` means the application either did not bind to `127.0.0.1:8000`, crashed during startup, or returned an unexpected error from the health route.

If diagnostics or review queue checks fail, inspect the matching file under `http-responses/` and then review the FastAPI route implementation in `app/main.py`. The workflow intentionally keeps `REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS=false` so these read-only debug routes are available in isolated CI without secrets.

If Circuit runtime validation returns `blocked`, check the response `error` field first. Common causes are a missing admin token, Hermes dispatch disabled, missing Hermes M2 configuration, a target host outside the allowed policy, credential-bearing target URLs, or unsafe DNS resolution.

If graceful shutdown fails, inspect `graceful-shutdown.txt` and `logs/startup.log`. The workflow sends `SIGTERM`, waits up to 10 seconds, and fails if the process must be killed.

If helper tests fail, inspect `runtime-validation-helper-results.xml`. These tests use fake request functions and temporary directories so they are deterministic and do not require network services.

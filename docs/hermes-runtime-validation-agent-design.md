# Hermes Runtime Validation Agent Design

Issue: #48 - ORCH-005 Hermes Runtime Validation Agent Design

Documentation-only task.

ARCHITECT_REVIEW_REQUIRED

## Objective

Hermes v1 is the Coding Crew runtime validation layer. It closes the gap between static agent completion and human browser or simulator validation by producing structured runtime evidence before BB2 and human review.

Hermes does not merge, deploy, mutate branches, write production data, or approve production changes. Its job is to observe approved preview or simulator targets, capture evidence, summarize runtime health, and return a canonical validation packet.

## Coding Crew Lifecycle

The target lifecycle is:

```text
Marcus -> Orchestrator -> Circuit -> Hermes -> BB2 -> Human Review -> Merge
```

### Marcus

Marcus owns roadmap intent, priority, final acceptance, merge authority, and production release decisions. Marcus may create issues, approve human review, request BB2 escalation, or stop a task.

### Orchestrator

The Orchestrator owns task intake, approved repository filtering, issue and PR event processing, queue persistence, label and comment writeback, Slack notification, and dispatch boundaries. It should decide when Hermes is eligible to run, but it should not execute browser or simulator validation itself.

### Circuit

Circuit owns implementation or specification work on the allowed task branch. Circuit produces a completion packet with `VERIFIED`, `ASSUMED`, and `UNVERIFIED`, then requests downstream validation when runtime behavior matters.

### Hermes

Hermes owns runtime validation evidence. It receives a target, a validation profile, and immutable task context from the Orchestrator. It runs read-only validation against preview URLs, local apps, simulator builds, or future mobile targets. It returns evidence and health findings without changing the application state beyond safe navigation and inspection.

### BB2

BB2 owns architecture review, risk review, and sufficiency decisions. BB2 consumes Circuit and Hermes packets together and decides whether the work is approved for human review, needs changes, is blocked, or requires Marcus.

### Human Review

Humans own final experiential review and merge readiness. Hermes should reduce repetitive validation work, not remove human review.

### Merge

Only Marcus may merge. Hermes never merges, deploys, force pushes, deletes branches, bypasses protection, or approves production writes.

## Ownership Boundaries

| Area | Owner | Boundary |
|---|---|---|
| Task priority and merge | Marcus | Human authority only. |
| Queue and dispatch | Orchestrator | May notify, label, comment, and persist state when enabled. |
| Code or spec change | Circuit | May work only inside the task branch rules. |
| Runtime observation | Hermes | Read-only validation and evidence capture. |
| Architecture approval | BB2 | Review and escalation decisions. |
| Production behavior | Marcus and protected systems | No agent may bypass controls. |

## Escalation Paths

Hermes must escalate instead of continuing when it detects any of the following:

- Authentication or authorization ambiguity.
- A target appears to be production rather than preview, staging, local, or simulator.
- A validation step would write customer, billing, or production data.
- Secrets or tokens appear in screenshots, console logs, network bodies, or artifacts.
- Runtime behavior suggests a security risk, contract change, roadmap conflict, router change, memory change, or merge blocker.
- Repeated validation failures are caused by environment availability rather than the change under review.

Escalations should preserve `bb-review-needed` and include `ARCHITECT_REVIEW_REQUIRED` when architecture judgment is needed.

## Retry Behavior

Hermes should use bounded retries because preview and simulator environments often have cold starts.

Recommended retry policy:

| Failure class | Retry policy | Escalation threshold |
|---|---:|---|
| Preview cold start | 3 attempts with 10 second backoff | Still unavailable after 3 attempts. |
| Navigation timeout | 2 attempts with fresh browser context | Repeated timeout on same route. |
| Flaky selector or hydration delay | 2 attempts after network idle or app-ready signal | Selector still unavailable. |
| Simulator boot | 2 attempts after simulator reset is confirmed safe | Boot or install still fails. |
| Crash | No silent retry after captured crash evidence | Escalate with crash logs. |

Retries must be reported in the output packet so BB2 can distinguish flaky infrastructure from deterministic product failure.

## Failure Handling

Hermes should classify outcomes as:

- `PASS`: Required runtime checks completed and evidence supports readiness.
- `WARN`: Checks completed with non-blocking anomalies.
- `FAIL`: A user-visible runtime issue, crash, broken flow, or severe console/network failure was observed.
- `BLOCKED`: Hermes could not validate because of missing target, auth, secrets, environment, or policy constraints.
- `ESCALATE`: Runtime evidence indicates BB2 or Marcus must decide before work continues.

A blocked run is not a failed implementation by itself. The packet must explain what prevented validation and what input would unblock it.

## Artifact Storage

Hermes artifacts should be stored outside the repository by default. Recommended storage locations, in priority order:

1. GitHub Actions artifacts for CI-triggered validation.
2. Orchestrator-managed object storage for Slack or queue-triggered validation.
3. PR or issue comments containing links to artifacts, not large inline blobs.
4. Local workspace artifacts only for temporary debugging.

Artifact records should include repo, issue or PR number, branch, commit SHA, run ID, timestamp, validation profile, target URL or simulator app identifier, and retention expiration.

Recommended retention:

- Passing routine validation: 14 days.
- Failed or escalated validation: 30 days.
- Security-sensitive evidence: redact immediately, retain only sanitized summary unless Marcus approves retention.

## Evidence Requirements

Every Hermes run should produce the following minimum evidence:

- Runtime target and environment classification.
- Commit SHA or immutable build identifier.
- Validation profile name and version.
- Routes, screens, or flows tested.
- Screenshots for each required checkpoint.
- Console error summary.
- Network failure summary.
- WebSocket summary when applicable.
- Runtime health summary.
- `VERIFIED`, `ASSUMED`, and `UNVERIFIED` sections.

## Hermes-Web Architecture

Hermes-Web validates browser-based targets:

- Vercel previews.
- Next.js apps.
- React dashboards.
- Mission Control.
- Other RiseOS web applications.

### Runtime Stack

Recommended stack for v1:

- Playwright as the primary execution engine.
- Chromium or Chrome with controlled browser contexts.
- Chrome DevTools Protocol sessions for console, network, performance, and WebSocket capture.
- Trace, screenshot, and video capture enabled per validation profile.
- DOM extraction snapshots for key assertions and BB2 review context.

### Web Validation Flow

1. Receive repo, issue or PR, branch, commit SHA, target URL, and validation profile.
2. Confirm the target is preview, staging, local, or otherwise explicitly approved.
3. Start isolated browser context with no shared cookies unless a test auth fixture is provided.
4. Capture initial health: HTTP status, redirects, TLS status, document title, major console errors.
5. Execute profile routes and flows.
6. Capture screenshots and DOM summaries at checkpoints.
7. Capture console, network, and WebSocket evidence.
8. Produce the canonical Hermes packet.
9. Attach artifacts and comment back through Orchestrator.

### Web Capabilities

Hermes-Web v1 should support:

- Page navigation and route health checks.
- Screenshot capture at desktop and mobile viewports.
- Console error and warning capture.
- Failed request capture and status summaries.
- WebSocket connection lifecycle and message metadata summaries.
- DOM extraction for headings, forms, buttons, tables, navigation, and visible error states.
- Basic accessibility smoke checks for missing labels and obvious focus traps.
- Network idle and app-ready waiting strategies.

Hermes-Web should avoid destructive actions by default. Form submission, mutation flows, uploads, billing actions, or database-affecting clicks require an explicit read-only test fixture or BB2-approved validation script.

## Hermes-Mac Architecture

Hermes-Mac validates Apple platform targets:

- Xcode projects.
- SwiftUI apps.
- iOS Simulator builds.
- XCTest suites.
- Screenshot and crash evidence.

Target apps include:

- Rylinn Field App.
- Rise Field Flow.
- Rise Customer App.
- ChargeUp.

### Runtime Stack

Recommended stack for v1 planning:

- A dedicated macOS runner or hosted Mac agent.
- Xcode command line tools.
- `xcodebuild` for build, test, and result bundle generation.
- iOS Simulator control through `simctl`.
- XCTest for deterministic smoke and regression flows.
- Screenshot capture through XCTest attachments or `simctl io screenshot`.
- Crash log collection from simulator diagnostics and result bundles.

### Mac Validation Flow

1. Receive repo, branch, commit SHA, scheme, simulator device, and validation profile.
2. Confirm the app target and simulator are approved for validation.
3. Install dependencies and select the requested Xcode version.
4. Build the app or test target.
5. Boot a clean simulator when the profile requires isolation.
6. Run smoke XCTest or launch validation.
7. Capture screenshots, result bundle, logs, and crash reports.
8. Summarize build, launch, test, crash, and screenshot evidence.
9. Return the canonical Hermes packet.

### Mac Capabilities

Hermes-Mac v1 should support:

- Build verification by scheme and destination.
- Simulator launch health.
- XCTest smoke suite execution.
- Screenshot capture for required screens.
- Crash log collection.
- Result bundle artifact links.
- Environment metadata: Xcode version, simulator device, runtime version, app bundle ID.

Hermes-Mac should not access production Apple services, production customer data, or device-level secrets. It should run with test fixtures and sandbox accounts only.

## Future Hermes-Android Architecture

Hermes-Android should mirror Hermes-Mac once Android work becomes active.

Recommended future stack:

- Linux runner with Android SDK and emulator support.
- Gradle build and test execution.
- Android Emulator snapshots for faster smoke validation.
- Espresso or UIAutomator for runtime flows.
- Logcat capture and crash parsing.
- Screenshot capture per checkpoint.
- Optional Firebase Test Lab integration for device matrix validation.

Hermes-Android should initially focus on build, launch, smoke navigation, screenshot, and crash evidence before adding deeper device matrix coverage.

## Trigger Design

Hermes should wake up only when the Orchestrator or a human-approved workflow supplies enough context to validate safely.

Recommended triggers:

| Trigger | Source | Hermes action |
|---|---|---|
| `HERMES_RUNTIME_VALIDATION_REQUIRED` label | Issue or PR | Queue validation if target is known. |
| `agent-ready` plus runtime profile | Issue | Wait for Circuit completion unless issue is spec-only. |
| Circuit `Status: Done` comment | Issue or PR | Validate the completed branch or preview. |
| PR opened or synchronized | Pull request | Validate preview when URL and profile are available. |
| BB2 review request | PR | Run targeted validation requested by BB2. |
| Orchestrator task creation | Queue | Attach validation requirements to task metadata. |
| Issue state change to ready | Issue | Queue only if labels and repo are approved. |

Hermes should not infer targets from arbitrary comments unless the Orchestrator normalizes and approves them.

## Canonical Hermes Completion Packet

Hermes output should use this format:

```markdown
## Hermes Runtime Validation

Repo: <owner/repo>
Issue/PR: <number and URL>
Branch: <branch>
Commit: <sha>
Target: <preview URL, app ID, or simulator target>
Profile: <profile name and version>
Run ID: <id>
Status: PASS | WARN | FAIL | BLOCKED | ESCALATE

VERIFIED

- <facts verified by runtime evidence>

ASSUMED

- <assumptions required to run or interpret validation>

UNVERIFIED

- <items Hermes did not validate>

ARTIFACTS

- Screenshots: <links>
- Console logs: <link or summary>
- Network summary: <link or summary>
- WebSocket summary: <link or summary>
- Runtime health summary: <link or summary>

NOTES

- <retry behavior, environment notes, or escalation reason>
```

## Security Model

Hermes must preserve these constraints:

- Preview-only, staging-only, local-only, simulator-only, or explicitly approved test target access.
- Read-only browser actions by default.
- No production writes.
- No merge, deploy, force push, branch deletion, or branch protection bypass.
- No production credentials in validation profiles.
- Secrets supplied only through managed secret stores, never in issue bodies, PR descriptions, screenshots, or repository files.
- Artifact redaction before posting links to logs that may contain tokens, cookies, private URLs, or user data.
- Least-privilege tokens for GitHub, Slack, browser auth fixtures, and artifact storage.
- Run isolation between repositories and validation targets.

Hermes should treat cookies, authorization headers, signed preview URLs, local storage, and crash logs as sensitive. Console and network evidence should summarize payloads rather than store full bodies unless a profile explicitly allows safe test data capture.

## Tooling Comparison

| Option | Strengths | Weaknesses | Recommended role |
|---|---|---|---|
| Playwright MCP | Strong browser automation, screenshots, traces, deterministic flows. | Needs clear profiles to avoid unsafe actions. | Primary Hermes-Web execution layer. |
| Chrome DevTools MCP | Deep console, network, WebSocket, DOM, and performance inspection. | Lower-level orchestration than Playwright. | Evidence capture companion to Playwright. |
| Preview Test Runner service | Centralizes validation, queueing, profiles, artifacts, and retention. | Requires service build and hosting. | Best long-term Hermes service shape. |
| GitHub Actions Playwright validation | Easy artifact storage and PR integration. | Less flexible for Slack-triggered ad hoc validation and Mac/iOS work. | MVP weekend implementation path for web previews. |
| Hybrid approach | Combines fast MVP with future dedicated Hermes service. | Requires clear ownership to avoid duplicated logic. | Recommended approach. |

## Recommended MVP For This Weekend

Smallest viable implementation:

1. Add Orchestrator support for a `HERMES_RUNTIME_VALIDATION_REQUIRED` label and a normalized validation request object.
2. Add a GitHub Actions Playwright workflow that can run against a provided preview URL and commit SHA.
3. Define one web validation profile: health check, desktop screenshot, mobile screenshot, console summary, network failure summary, and DOM heading/button extraction.
4. Store screenshots and logs as GitHub Actions artifacts.
5. Post a Hermes packet back to the PR or issue through existing Orchestrator writeback boundaries.
6. Add BB2 review consumption rules: BB2 reviews Circuit and Hermes packets together before approving human review.

This delivers value quickly for Vercel previews and web dashboards while leaving Hermes-Mac and Hermes-Android as planned platform extensions.

## Integration Plan With Orchestrator

The Orchestrator should add a `RuntimeValidationRequest` model with:

- Repository full name.
- Issue number and optional PR number.
- Branch.
- Commit SHA.
- Target type: `web`, `mac`, or `android`.
- Target locator: preview URL, app scheme, simulator target, or build artifact.
- Validation profile.
- Requested by.
- Reason.
- Created timestamp.

The Orchestrator should persist request state separately from BB2 review queue items. Suggested states:

- `queued`
- `running`
- `passed`
- `warned`
- `failed`
- `blocked`
- `escalated`

The Orchestrator should then expose Hermes results as read-only context for BB2 review prompts.

## MVP Build Plan

Phase 1: Specification and profile contract.

- Land this architecture spec.
- Define the canonical packet.
- Define the first web validation profile.

Phase 2: Web preview validation.

- Add a Playwright workflow or runner.
- Capture screenshots, console, network, and DOM summaries.
- Store artifacts.
- Post Hermes packet.

Phase 3: Orchestrator integration.

- Add validation request queue state.
- Add label/comment triggers.
- Add BB2 context pack integration for Hermes results.

Phase 4: Mac validation planning.

- Provision macOS runner.
- Define app-specific schemes and simulator profiles.
- Add XCTest smoke profile and artifact capture.

Phase 5: Android roadmap.

- Define emulator build validation profile.
- Add Android only after web and Mac evidence contracts are stable.

## Future Roadmap

- Validation profile registry per repo and app.
- BB2-authored targeted validation requests.
- Automatic preview URL discovery from Vercel deployment events.
- Rich visual diffing for stable UI screens.
- Accessibility smoke reports.
- Performance budget checks for key routes.
- Cross-browser validation where needed.
- iOS simulator matrix validation.
- Android emulator and Firebase Test Lab integration.
- Central artifact viewer in Mission Control.

## BB2 Question

Is this Hermes architecture sufficient to close the runtime validation gap currently observed between Circuit completion and BB2 approval?

Circuit recommendation: yes for a v1 architecture, if the first implementation uses the hybrid path: GitHub Actions Playwright validation for immediate web preview evidence, Orchestrator-managed request and packet state, and future dedicated Hermes-Web and Hermes-Mac runners once the evidence contract is stable.

## VERIFIED

- Issue #48 requires a design/specification-only deliverable.
- The approved repository is `marcus937/riseos-agent-orchestrator`.
- The allowed work branch is `agent-integration`.
- The spec covers the requested lifecycle, Hermes-Web, Hermes-Mac, future Android architecture, triggers, completion packet, security model, tooling recommendations, MVP build plan, integration plan, and roadmap.

## ASSUMED

- Hermes v1 should begin with web preview validation because it is the smallest operational path for this weekend.
- Hermes-Mac requires a dedicated macOS runner and should follow after the evidence contract is proven.
- Artifact storage should use GitHub Actions artifacts first for MVP simplicity.

## UNVERIFIED

- No runtime code was changed or executed.
- No Playwright, Xcode, simulator, or Android validation was run because this task is documentation-only.
- Exact hosting and artifact retention infrastructure remains to be selected during implementation.

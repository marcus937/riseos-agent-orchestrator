# Runtime Validation Review Bridge Rollout

## Default State

`ENABLE_RUNTIME_VALIDATION_REVIEW_BRIDGE=false` by default.

With the flag disabled, GitHub webhook processing preserves the legacy path:

```text
GitHub webhook -> ReviewWorkItem -> BB2/OpenAI review
GitHub webhook -> Hermes dispatch side channel
```

No runtime validation gating occurs unless the flag is explicitly enabled.

## Opt-In Rollout

1. Confirm Hermes M2 dispatch settings are present:
   - `HERMES_M2_ENABLE_DISPATCH=true`
   - `HERMES_M2_BASE_URL=<Hermes M2 URL>`
   - `HERMES_M2_TOKEN=<Hermes token>`
2. Confirm BB2 review processing is healthy with the bridge disabled.
3. Enable `ENABLE_RUNTIME_VALIDATION_REVIEW_BRIDGE=true` in a non-production environment.
4. Send a runtime-dependent Circuit PR webhook and verify orchestrator snapshot shows:
   - `runtime_validation_pending`
   - `runtime_validation_completed` or `runtime_validation_failed`
   - `bb2_review_requested_from_runtime_validation`
5. Confirm BB2 review input contains `runtime_evidence_context` with Hermes status, target URL, screenshot availability, console errors, network failures, evidence artifacts, and validation result status.
6. Enable the flag for production only after the non-production validation is complete.

## Rollback

Set `ENABLE_RUNTIME_VALIDATION_REVIEW_BRIDGE=false` and restart the orchestrator.

The rollback is config-only. The SQLite migration is additive and leaves existing rows readable. Runtime validation metadata remains stored on rows that already received it, but new GitHub webhooks return to the legacy BB2 review path.

## Lifecycle

```text
GitHub PR
  -> Circuit review context
  -> runtime_validation_pending
  -> Hermes runtime validation
  -> evidence collection
  -> RuntimeValidationBB2Packet
  -> runtime_validation_completed | runtime_validation_failed
  -> bb2_review_requested_from_runtime_validation
  -> ReviewWorkItem processed by existing worker
  -> OpenAI/BB2 review
  -> GitHub writeback
```

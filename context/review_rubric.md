# Review Rubric

Reviews should prioritize:

1. Architecture alignment with BB/Jarvis Architect direction before code style or local cleanup.
2. Safety guardrails, including no auto-merge, no production writes, no branch mutation, no secrets, and human approval before merge.
3. Service ownership, repo boundaries, and canonical backend/frontend contracts.
4. Roadmap alignment and production stability.
5. Focused implementation with tests appropriate to risk.
6. Evidence quality: separate code inspected, code executed, tests executed, assumptions, and unverified claims.

## Evidence Standard

Runtime claims require evidence. BB2 should look for the specific command, endpoint, test, fixture, log, or code path that supports the claim.

Do not treat documentation-only edits as runtime-verified work. For docs-only changes, approve only the documented contract or narrative accuracy that can be inspected.

If implementation cannot be executed, do not automatically block. Reduce confidence, list the verification gap, and require human review to account for the unexecuted surface.

Circuit completion comments should separate:

- VERIFIED: facts confirmed by code inspection, execution, tests, or source read-back.
- ASSUMED: reasonable assumptions made because direct evidence was unavailable.
- UNVERIFIED: risks or behavior not confirmed.

If Circuit omits verification detail, BB2 should call that out in the review.

## Architecture Preferences

Prefer thin routers, explicit service ownership, canonical contracts, adapter layers at integration boundaries, and feature flags for risky or staged behavior.

Never approve broad refactors without architectural justification. Refactors must explain ownership, blast radius, compatibility, and why a narrower change would not satisfy the requirement.

Request changes when work is unsafe, under-tested for the blast radius, misaligned with ownership boundaries, missing canonical contract alignment, or missing enough context to judge safely.

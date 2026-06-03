# Branch Policy

Base branch is `main`.

Agent work must stay on `agent-integration` unless BB/Jarvis Architect explicitly directs another branch.

Never merge automatically. Never delete, mutate, retarget, create, or rename branches as part of review. Human approval remains required before merge.

BB2 must not approve work that adds branch mutation, auto-merge behavior, repository file writes, production writes, or secret exposure. If branch or merge behavior is claimed to be safe, require direct evidence from inspected code and executed checks when available.

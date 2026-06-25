"""Startup hook for runtime validation review-gate diagnostics.

Python imports this module automatically when the repository root is on sys.path.
The hook only installs diagnostics wrappers around existing lookup functions; it
never changes retry behavior, transport behavior, Hermes, Vercel discovery, or
review-gate return values.
"""

from __future__ import annotations

import logging

try:
    from app.runtime_validation_trace_patch import install_runtime_validation_trace_patch

    install_runtime_validation_trace_patch()
except Exception as exc:  # noqa: BLE001 - diagnostics must not block app startup.
    logging.getLogger("riseos_agent_orchestrator").warning(
        "Could not install runtime validation review-gate diagnostics: %s",
        exc,
    )

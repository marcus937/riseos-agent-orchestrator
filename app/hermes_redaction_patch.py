from __future__ import annotations

import re
from typing import Any

SECRET_REDACTION = "[REDACTED]"
AUTH_BEARER_PATTERN = re.compile(r"(?i)(authorization\s*:\s*)bearer\s+([^'\"\s,;}]+)")
AUTH_REDACTED_BEARER_PATTERN = re.compile(r"(?i)(authorization\s*:\s*\[REDACTED\]\s+)([^'\"\s,;}]+)")


def install_hermes_redaction_patch(module: Any) -> None:
    if getattr(module, "_wf20_redaction_patch_installed", False):
        return

    previous_redact = module._redact_sensitive_text

    def redact_sensitive_text(value: str | None, settings: Any) -> str | None:
        redacted = previous_redact(value, settings)
        if redacted is None:
            return None
        redacted = AUTH_BEARER_PATTERN.sub(lambda match: f"{match.group(1)}{SECRET_REDACTION} {SECRET_REDACTION}", redacted)
        return AUTH_REDACTED_BEARER_PATTERN.sub(lambda match: f"{match.group(1)}{SECRET_REDACTION}", redacted)

    module._redact_sensitive_text = redact_sensitive_text
    module._wf20_redaction_patch_installed = True

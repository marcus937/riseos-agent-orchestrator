from __future__ import annotations

from app import hermes_dispatch_impl as _impl
from app.hermes_evidence_patch import install_hermes_evidence_patch as _install_hermes_evidence_patch
from app.hermes_preview_guard import install_preview_guard as _install_preview_guard
from app.hermes_redaction_patch import install_hermes_redaction_patch as _install_hermes_redaction_patch

_install_preview_guard(_impl, globals())
_install_hermes_evidence_patch(_impl)
_install_hermes_redaction_patch(_impl)

for _name in dir(_impl):
    if not _name.startswith("__") and _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = [_name for _name in globals() if not _name.startswith("_")]

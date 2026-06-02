import hmac
from hashlib import sha256


GITHUB_SIGNATURE_HEADER = "x-hub-signature-256"


def build_signature(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
    return f"sha256={digest}"


def verify_github_signature(secret: str, payload: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header:
        return False
    expected = build_signature(secret, payload)
    return hmac.compare_digest(expected, signature_header)

"""Safe projections for internal Bridge request identities."""

from __future__ import annotations

import hashlib
import re


def bridge_request_fingerprint(request_id: str) -> str:
    """Return the stable operator-facing fingerprint for a Bridge request key."""
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def safe_bridge_request_fingerprint(request_id: str) -> str:
    """Keep an existing valid fingerprint stable; hash every other value."""
    if request_id.startswith("sha256:"):
        digest = request_id.removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{16,64}", digest):
            return f"sha256:{digest[:16]}"
    return bridge_request_fingerprint(request_id)

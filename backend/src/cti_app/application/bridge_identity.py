"""Safe projections for internal Bridge request identities."""

from __future__ import annotations

import hashlib


def bridge_request_fingerprint(request_id: str) -> str:
    """Return the stable operator-facing fingerprint for a Bridge request key."""
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"

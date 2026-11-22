"""Closed provider-profile registrations for the host session supervisor (H4/H5).

The supervisor launches only profiles registered here; a request cannot name a
binary, argv or host path. H4 registers Codex; H5 adds Claude. Importing this
package imports the supervisor module, so the supervisor registers profiles
through a local import inside ``default_profiles()`` to keep the cycle closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .codex import CodexProfile

if TYPE_CHECKING:
    from ..supervisor import ProfileRegistry


def register_profiles(registry: ProfileRegistry) -> None:
    """Register every provider profile this build may launch."""
    registry.register(CodexProfile.from_environment())

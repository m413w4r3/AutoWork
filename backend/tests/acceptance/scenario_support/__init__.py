"""Shared support for the M24 synthetic acceptance scenario (lot 10-S).

The package holds only test-side wiring: synthetic seeds, fake providers, the
real host sidecar runner and the real FastAPI application assembly.  It never
imports production modules for their side effects and never contacts a live
provider, network endpoint or CLI.
"""

from __future__ import annotations

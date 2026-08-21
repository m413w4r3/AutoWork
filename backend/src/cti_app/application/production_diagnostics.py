"""Backward-compatible imports for ProductionDiagnosticsLog.

The diagnostic logging functionality has been moved to diagnostics.py.
This module re-exports the class and constants for backward compatibility.
"""

from __future__ import annotations

from cti_app.application.diagnostics import (
    MAX_PAYLOAD_BYTES,
    DiagnosticsLog as ProductionDiagnosticsLog,
)

__all__ = ["ProductionDiagnosticsLog", "MAX_PAYLOAD_BYTES"]

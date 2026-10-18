"""Read-only, versioned Ghidra headless service."""

from cti_app.ghidra_service.contract import CONTRACT_VERSION, OPERATIONS
from cti_app.ghidra_service.service import GhidraReadOnlyService

__all__ = ["CONTRACT_VERSION", "OPERATIONS", "GhidraReadOnlyService"]

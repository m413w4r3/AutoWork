from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from cti_app.ghidra_service.selftest import run_real_headless_selftest


def test_real_headless_ghidra_inspects_a_generated_minimal_elf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run in the image with:

    docker compose --profile ghidra run --rm --entrypoint python3 ghidra \
      -m cti_app.ghidra_service.selftest
    """
    install_dir = Path(os.environ.get("GHIDRA_INSTALL_DIR", "/opt/ghidra"))
    image_test_requested = os.environ.get("GHIDRA_SERVICE_IMAGE_TEST") == "1"
    if not install_dir.is_dir():
        if image_test_requested:
            pytest.fail("GHIDRA_SERVICE_IMAGE_TEST=1 but GHIDRA_INSTALL_DIR is absent")
        pytest.skip("Ghidra headless distribution is not installed")

    with tempfile.TemporaryDirectory(prefix="ghidra-real-test-") as workspace:
        monkeypatch.setenv("GHIDRA_WORKSPACE", workspace)
        run_real_headless_selftest()

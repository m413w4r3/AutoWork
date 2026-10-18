"""Image verification after building with `docker compose build analysis-worker`:

    docker compose build analysis-worker
    cd backend
    ANALYSIS_WORKER_IMAGE_TEST=1 \
        uv run --python 3.12 pytest tests/test_confined_analysis_image.py -q

The opt-in test starts only a one-off verification process, never the service stack.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.environ.get("ANALYSIS_WORKER_IMAGE_TEST") != "1",
    reason="run after building analysis-worker with ANALYSIS_WORKER_IMAGE_TEST=1",
)
def test_built_analysis_worker_image_has_attested_analysis_binaries() -> None:
    root = Path(__file__).resolve().parents[2]
    code = "\n".join(
        (
            "import importlib.metadata, re, shutil, subprocess",
            "from pathlib import Path",
            "import capa.main",
            "from cti_app.infrastructure.capa import ruleset_manifest",
            "assert importlib.metadata.version('flare-capa') == '9.4.0'",
            "assert importlib.metadata.version('smda') == '4.5.0'",
            "assert importlib.metadata.version('flare-floss') == '3.1.1'",
            "assert importlib.metadata.version('mwcp') == '3.16.1'",
            "assert importlib.metadata.version('yara-python') == '4.5.0'",
            "import yara; assert yara.__version__ == '4.5.0'",
            "assert shutil.which('tshark') and shutil.which('capinfos')",
            "assert shutil.which('floss') and shutil.which('mwcp')",
            "assert '3.1.1' in subprocess.check_output(['floss', '--version'], text=True)",
            "assert subprocess.run(['mwcp', 'list', '--json'], "
            "capture_output=True, check=True).stdout",
            "assert '4.0.17' in subprocess.check_output(['tshark', '--version'], text=True)",
            "assert '4.0.17' in subprocess.check_output(['capinfos', '-v'], text=True)",
            "signatures = capa.main.get_default_root() / 'sigs'",
            "assert signatures.is_dir() and any(signatures.glob('*.sig'))",
            "rules = Path('/app/rules/capa')",
            "marker = (rules / 'AUTOWORK_RULESET_SHA256').read_text(encoding='ascii').strip()",
            "assert re.fullmatch(r'[0-9a-f]{64}', marker)",
            "assert ruleset_manifest(rules) == marker",
        )
    )
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "analysis-worker",
            "-c",
            code,
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

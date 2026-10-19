"""Image verification after building with `docker compose build analysis-worker`:

    docker compose build analysis-worker
    cd backend
    ANALYSIS_WORKER_IMAGE_TEST=1 \
        uv run --python 3.12 pytest tests/test_confined_analysis_image.py -q

The opt-in test starts only a one-off verification process, never the service stack.
"""

from __future__ import annotations

import base64
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
    from tests.test_confined_local_commands import _MINIMAL_DOTNET_ASSEMBLY

    fixture_b64 = base64.b64encode(_MINIMAL_DOTNET_ASSEMBLY).decode("ascii")
    code = "\n".join(
        (
            "import asyncio, base64, hashlib, importlib.metadata, re, shutil, subprocess",
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
            "assert shutil.which('file') and shutil.which('strings')",
            "assert shutil.which('readelf') and shutil.which('objdump')",
            "assert shutil.which('xxd') and shutil.which('sha256sum')",
            "assert '5.44' in subprocess.check_output(['file', '--version'], text=True)",
            "assert '2.40' in subprocess.check_output(['strings', '--version'], text=True)",
            "assert '2.40' in subprocess.check_output(['readelf', '--version'], text=True)",
            "assert '2.40' in subprocess.check_output(['objdump', '--version'], text=True)",
            "xxd = subprocess.run(['xxd', '-v'], capture_output=True, text=True)",
            "assert '2022-01-14' in xxd.stdout + xxd.stderr",
            "assert '9.1' in subprocess.check_output(['sha256sum', '--version'], text=True)",
            "assert shutil.which('monodis')",
            "assert subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'mono-utils'], ",
            "text=True).strip() == '6.8.0.105+dfsg-3.3+deb12u1'",
            "monodis_help = subprocess.run(['monodis', '--help'], capture_output=True, ",
            "text=True, check=False)",
            "monodis_help_output = monodis_help.stdout + monodis_help.stderr",
            "assert 'Mono Common Intermediate Language Disassembler' in monodis_help_output",
            "assert all(option in monodis_help_output for option in ",
            "('--userstrings', '--typedef', '--method', '--show-method-tokens'))",
            "assert shutil.which('floss') and shutil.which('mwcp')",
            "assert '3.1.1' in subprocess.check_output(['floss', '--version'], text=True)",
            "assert subprocess.run(['mwcp', 'list', '--json'], "
            "capture_output=True, check=True).stdout",
            "assert '4.0.17' in subprocess.check_output(['tshark', '--version'], text=True)",
            "assert '4.0.17' in subprocess.check_output(['capinfos', '-v'], text=True)",
            "assert '7.0.10' in subprocess.check_output(['suricata', '-V'], text=True)",
            "rules = Path('/app/rules/suricata')",
            "rule_file = rules / 'autowork.rules'",
            "assert rule_file.is_file()",
            "assert (rules / 'AUTOWORK_RULESET_REVISION').read_text().strip() "
            "== 'autowork-suricata-v1'",
            "assert (rules / 'AUTOWORK_RULESET_SHA256').read_text().strip() "
            "== '66f69f0cf586a87f739d4f63c6bb9d23a7d303f8adf23c2773a31839ea7f9f59'",
            "import tempfile",
            "from cti_app.workers.confined_analysis import _minimal_suricata_config",
            "from cti_app.workers.confined_analysis import _validate_local_command_arguments",
            "from cti_app.workers.confined_analysis import build_analysis_worker_probe_payload",
            "from cti_app.workers.confined_analysis import _run_monodis",
            "from cti_app.config import Settings",
            f"fixture = base64.b64decode({fixture_b64!r})",
            "from uuid import uuid4",
            "from cti_app.application.confined_analysis import (",
            " ConfinedAnalysisJobParameters, ConfinedAnalysisTool)",
            "fixture_sha256 = hashlib.sha256(fixture).hexdigest()",
            "monodis_parameters = ConfinedAnalysisJobParameters(",
            " investigation_id=uuid4(), artifact_id=uuid4(), artifact_sha256=fixture_sha256,",
            " operation_key='confined-analysis:' + 'a' * 64,",
            " tool=ConfinedAnalysisTool.LOCAL_COMMAND,",
            " timeout_seconds=30, max_output_bytes=16384, arguments=('monodis',),",
            " analysis_parameters={'operation': 'cil', 'method_name': 'Main'})",
            "monodis_result = asyncio.run(",
            " _run_monodis(monodis_parameters, fixture, Settings(_env_file=None)))",
            "assert monodis_result['status'] == 'SUCCEEDED', monodis_result",
            "assert monodis_result['method'] == 'Fixture.Main'",
            "assert monodis_result['offset'] == 0 and monodis_result['length'] <= 8192",
            "assert 'IL_0001:  ret' in monodis_result['stdout']",
            "try:",
            " _validate_local_command_arguments(('file', '/etc/passwd'), {'mime_type': 'false'}, "
            "max_range_bytes=1048576)",
            " raise AssertionError('host paths must not be accepted as command arguments')",
            "except ValueError:",
            " pass",
            "try:",
            " Path('/app/autowork-write-probe').write_text('blocked')",
            " raise AssertionError('analysis worker rootfs must remain read-only')",
            "except OSError:",
            " pass",
            "scratch = Path('/tmp/autowork-local-command-image-test')",
            "scratch.write_text('tmpfs-only')",
            "assert scratch.read_text() == 'tmpfs-only'; scratch.unlink()",
            "import socket",
            "try:",
            " socket.create_connection(('192.0.2.1', 9), timeout=0.25)",
            " raise AssertionError('analysis worker must not have network access')",
            "except OSError:",
            " pass",
            "with tempfile.TemporaryDirectory(prefix='suricata-image-test-') as root:",
            " classification = Path(root) / 'classification.config'",
            " classification.write_text('config classification: autowork, "
            "AutoWork Offline Alert,3\\n')",
            " reference = Path(root) / 'reference.config'; reference.write_text('')",
            " config = Path(root) / 'suricata.yaml'",
            " config.write_text(",
            "  _minimal_suricata_config(classification_file=classification,",
            "   reference_file=reference))",
            " output = Path(root) / 'out'; output.mkdir()",
            " checked = subprocess.run(['suricata', '-T', '-c', str(config), '-S', str(rule_file), "
            "'-l', str(output), '--runmode', 'single'], capture_output=True, text=True)",
            " assert checked.returncode == 0, checked.stdout + checked.stderr",
            "signatures = capa.main.get_default_root() / 'sigs'",
            "assert signatures.is_dir() and any(signatures.glob('*.sig'))",
            "probe = build_analysis_worker_probe_payload(Settings(_env_file=None))",
            "assert probe['versions']['local_monodis'] == '6.8.0.105+dfsg-3.3+deb12u1'",
            "assert 'local_command' in probe['supported_families']",
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
            "-e",
            "HOME=/tmp/home",
            "-e",
            "XDG_CONFIG_HOME=/tmp/home/.config",
            "-e",
            "XDG_CACHE_HOME=/tmp/home/.cache",
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

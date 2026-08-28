from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.capabilities import CapabilitiesService
from cti_app.domain.capabilities import Capability, CapabilitySet, CapabilitySetStatus
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessResult,
    AnalysisSubprocessStatus,
)
from cti_app.infrastructure.capa import CapaRunner, parse_capa_output, ruleset_manifest


class FakeSubprocessRunner:
    def __init__(self, result: AnalysisSubprocessResult) -> None:
        self.result = result
        self.argv = []

    async def __call__(self, argv, **kwargs):
        self.argv.append(list(argv))
        return self.result


def test_capability_set_is_immutable_and_serializable() -> None:
    result = CapabilitySet(
        sample_id=uuid4(), tool_name="capa", tool_version="9.4.0",
        ruleset_sha256="a" * 64, parameters_sha256="b" * 64,
        status=CapabilitySetStatus.SUCCEEDED,
        capabilities=(
            Capability(
                rule_id="a", name="A", namespace="n", attack=(), mbc=(),
                function_addresses=(),
            ),
        ),
        errors=(),
    )
    assert result.as_json()["capabilities"][0]["rule_id"] == "a"
    with pytest.raises(AttributeError):
        result.status = CapabilitySetStatus.UNAVAILABLE  # type: ignore[misc]


def test_manifest_is_deterministic_and_has_no_mtime(tmp_path: Path) -> None:
    (tmp_path / "b.yml").write_text("b")
    (tmp_path / "a.yml").write_text("a")
    first = ruleset_manifest(tmp_path)
    (tmp_path / "a.yml").touch()
    assert ruleset_manifest(tmp_path) == first


def test_parser_normalizes_and_ignores_upstream_extra_fields() -> None:
    payload = (
        b'{"rules":{"z":{"meta":{"name":"Z","namespace":"n",'
        b'"att&ck":[{"id":"T2"},{"id":"T1"},{"id":"T1"}],'
        b'"mbc":[{"id":"B"}]},"source":"fixture",'
        b'"matches":[[{"type":"absolute","value":2},{"statement":{},"extra":true}],'
        b'[{"type":"relative","value":1},{"statement":{}}]],"extra":1}}}'
    )
    values, errors = parse_capa_output(payload)
    assert not errors
    assert values[0]["attack"] == ("T1", "T2")
    assert values[0]["function_addresses"] == ("0x1", "0x2")


@pytest.mark.asyncio
async def test_runner_uses_fake_subprocess_and_explicit_rules(tmp_path: Path) -> None:
    fake = FakeSubprocessRunner(
        AnalysisSubprocessResult(
            AnalysisSubprocessStatus.SUCCEEDED, 0, b'{"rules":{}}', b""
        )
    )
    runner = CapaRunner(fake)
    version, result = await runner.run(
        sample=b"sample", rules_path=tmp_path, timeout_seconds=1,
        output_limit=1000, memory_limit_bytes=1024 * 1024,
    )
    assert version == "9.4.0"
    assert result.status is AnalysisSubprocessStatus.SUCCEEDED
    assert fake.argv[0][0:2] == ["capa", "-r"]
    assert str(tmp_path) in fake.argv[0]


class _CapabilitySets:
    def __init__(self) -> None:
        self.values = {}

    async def get(self, sample_id, tool_version, ruleset_sha256, parameters_sha256):
        return self.values.get((sample_id, tool_version, ruleset_sha256, parameters_sha256))

    async def add_if_absent(self, result, blob_id):
        key = (
            result.sample_id,
            result.tool_version,
            result.ruleset_sha256,
            result.parameters_sha256,
        )
        if key in self.values:
            return False
        self.values[key] = result
        return True

    async def index(self, result):
        return None


class _CapabilitiesUow:
    def __init__(self, sample, capability_sets) -> None:
        self.samples = SimpleNamespace(get=self._get_sample)
        self.capability_sets = capability_sets
        self._sample = sample

    async def _get_sample(self, sample_id):
        return self._sample if sample_id == self._sample.id else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def commit(self):
        return None


class _CapabilityBlobs:
    async def read(self, blob_id, *, max_bytes):
        return b"sample"

    async def ingest(self, source, *, logical_bucket, mime_type):
        return SimpleNamespace(id=uuid4())


@pytest.mark.asyncio
async def test_unavailable_empty_rules_result_does_not_collide_with_real_rules(
    tmp_path: Path,
) -> None:
    sample = SimpleNamespace(id=uuid4(), blob_id=uuid4())
    capability_sets = _CapabilitySets()
    uow = _CapabilitiesUow(sample, capability_sets)

    class _Runner:
        async def __call__(self, argv, **kwargs):
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.SUCCEEDED, 0, b'{"rules":{}}', b""
            )

    rules_path = tmp_path / "missing"
    service = CapabilitiesService(
        _CapabilityBlobs(), lambda: uow, rules_path=rules_path,
        timeout_seconds=1, max_output_bytes=1000, max_memory_bytes=1024,
        runner=CapaRunner(_Runner()),
    )
    unavailable = await service.analyze(sample.id)

    rules_path.mkdir()
    (rules_path / "rule.yml").write_text("rule")
    available = await service.analyze(sample.id)

    assert unavailable.ruleset_sha256 == ""
    assert available.ruleset_sha256 == ruleset_manifest(rules_path)
    assert available.ruleset_sha256
    assert len(capability_sets.values) == 2

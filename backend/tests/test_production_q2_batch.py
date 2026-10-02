"""Archive-backed IOC_RULES batches: deterministic handles, no cross-capture moves."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cti_app.application import production_q2_batch
from cti_app.application.production_parsers import Q2ArtifactProposal, Q2SourceOutput


def _archive_entry(index: int) -> production_q2_batch.ArchiveQ2BatchSource:
    return production_q2_batch.ArchiveQ2BatchSource(
        source_document_id=uuid4(),
        content_sha256=f"{index:064x}",
    )


def test_archive_batch_partition_is_char_bounded_and_never_drops_a_capture() -> None:
    entries = tuple(_archive_entry(index) for index in range(1, 7))

    full = production_q2_batch.partition_archive_q2_batch_sources(entries)
    assert [len(batch) for batch in full] == [
        production_q2_batch.MAX_Q2_BATCH_SOURCES,
        2,
    ]
    assert tuple(entry for batch in full for entry in batch) == entries

    lengths = {entry.source_document_id: 4 for entry in entries}
    bounded = production_q2_batch.partition_archive_q2_batch_sources(
        entries, text_lengths=lengths, max_total_chars=8
    )
    assert [len(batch) for batch in bounded] == [2, 2, 2]
    assert tuple(entry for batch in bounded for entry in batch) == entries


def test_archive_batch_handles_and_identity_are_deterministic() -> None:
    entries = tuple(_archive_entry(index) for index in range(1, 4))

    batch = production_q2_batch.make_archive_q2_batch(entries)

    assert [entry.batch_id for entry in batch] == ["B1", "B2", "B3"]
    assert production_q2_batch.archive_q2_batch_identity(batch) == (
        production_q2_batch.archive_q2_batch_identity(
            production_q2_batch.make_archive_q2_batch(entries)
        )
    )
    reordered = production_q2_batch.make_archive_q2_batch(tuple(reversed(entries)))
    assert production_q2_batch.archive_q2_batch_identity(reordered) != (
        production_q2_batch.archive_q2_batch_identity(batch)
    )
    with pytest.raises(ValueError):
        production_q2_batch.make_archive_q2_batch(entries[:1])


def test_archive_batch_attribution_never_moves_content_between_captures() -> None:
    entries = production_q2_batch.make_archive_q2_batch(
        tuple(_archive_entry(index) for index in range(1, 4))
    )
    expected = {entry.batch_id: entry for entry in entries}
    output = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="evil.security-lab.io",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            )
        ]
    )
    response = production_q2_batch.Q2BatchResponse(
        sources=[
            production_q2_batch.Q2BatchSourceOutput(batch_id="B1", output=output),
            production_q2_batch.Q2BatchSourceOutput(batch_id="B1", output=output),
            production_q2_batch.Q2BatchSourceOutput(batch_id="B9", output=output),
            production_q2_batch.Q2BatchSourceOutput(batch_id="B3", output=output),
        ]
    )

    attribution = production_q2_batch.attribute_q2_batch_response(response, expected)

    assert [result.batch_id for result in attribution.results] == ["B1", "B2", "B3"]
    assert attribution.result_for("B1").error_code == "batch_source_duplicate"
    assert attribution.result_for("B2").error_code == "batch_source_missing"
    assert attribution.result_for("B3").output is output
    assert "batch_source_unknown" in attribution.warnings


def test_line_oriented_batch_parser_recovers_each_source_and_keeps_raw_text() -> None:
    raw = (
        "@@Q2:B1@@\nIOC contextual domain\n"
        '- clearview.ai :: Reconnaissance :chatgpt-content-reference{index="0"}\n'
        "@@Q2:B2@@\nUNAVAILABLE\n"
        "@@Q2:B3@@\nEMPTY\n"
        "@@Q2:B4@@\nIOC contextual domain\n"
        "- indicator.example :: infrastructure secondaire\n"
        "UNCERTAINTIES\n- L'attribution reste incertaine.\n"
    )

    parsed = production_q2_batch.parse_q2_batch_response(raw, ("B1", "B2", "B3", "B4"))

    first, second, third, fourth = parsed.sources
    assert parsed.usable
    assert sum(line.startswith("- ") for line in raw.splitlines()) == 3
    retained_items = sum(
        len(source.output.artifacts)
        + len(source.output.facts)
        + len(source.output.events)
        + len(source.output.rules)
        + len(source.output.uncertainties)
        for source in parsed.sources
        if source.output is not None
    )
    assert retained_items == 3
    assert sum(source.error_code == "batch_source_unavailable" for source in parsed.sources) == 1
    assert first.usable
    assert first.output is not None
    assert first.output.artifacts[0].value == "clearview.ai"
    assert first.output.artifacts[0].context == "Reconnaissance"
    assert ':chatgpt-content-reference{index="0"}' in first.raw_block
    assert second.error_code == "batch_source_unavailable"
    assert third.usable
    assert third.output == Q2SourceOutput()
    assert fourth.usable
    assert fourth.output is not None
    assert fourth.output.uncertainties == ["L'attribution reste incertaine."]

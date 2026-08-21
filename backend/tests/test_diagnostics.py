"""The local diagnostic trail.

It exists to answer, after the fact, "what did the model actually say and what
did the parser do with it?". Two properties matter: it records enough to answer
that, and it never breaks a production run.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from cti_app.application.diagnostics import (
    MAX_PAYLOAD_BYTES,
    DiagnosticsLog,
)


def _events(root: Path) -> list[dict[str, object]]:
    lines = (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_disabled_by_default_and_silent(tmp_path: Path) -> None:
    """No configured root means no trail, and no error either."""
    log = DiagnosticsLog.from_env(None)

    assert log.enabled is False
    log.record(event="anything", run_id=uuid4())  # must not raise


def test_model_answer_is_recorded_verbatim(tmp_path: Path) -> None:
    log = DiagnosticsLog.from_env(tmp_path)
    run_id, subject_id = uuid4(), uuid4()

    log.record_model_answer(
        run_id=run_id,
        subject_id=subject_id,
        stage="references",
        correlation_id="corr-1",
        prompt="Cherche des références.",
        answer="# REFERENCES\n\n## SOURCE S1\n",
        idempotency_key=f"references-{run_id}-v1",
    )

    entry = _events(tmp_path)[0]
    assert entry["event"] == "model.answer"
    assert entry["stage"] == "references"
    assert entry["correlation_id"] == "corr-1"
    assert entry["idempotency_key"] == f"references-{run_id}-v1"

    stored = (tmp_path / str(entry["payload_file"])).read_text(encoding="utf-8")
    assert "Cherche des références." in stored
    assert "## SOURCE S1" in stored


def test_parse_result_keeps_the_dropped_blocks(tmp_path: Path) -> None:
    """The dropped blocks are the whole point: they say what was unreadable."""
    log = DiagnosticsLog.from_env(tmp_path)
    run_id = uuid4()

    log.record_parse(
        run_id=run_id,
        subject_id=uuid4(),
        stage="references",
        correlation_id="corr-1",
        usable=True,
        warnings=["duplicate_source_merged"],
        errors=[],
        repair_actions=[],
        dropped_blocks=["## SOURCE\n\nplus d'url ici"],
        source_count=3,
    )

    entry = _events(tmp_path)[0]
    assert entry["usable"] is True
    assert entry["warnings"] == ["duplicate_source_merged"]
    assert entry["dropped_block_count"] == 1
    assert entry["source_count"] == 3
    stored = (tmp_path / str(entry["payload_file"])).read_text(encoding="utf-8")
    assert "plus d'url ici" in stored


def test_payloads_are_numbered_in_stage_order(tmp_path: Path) -> None:
    log = DiagnosticsLog.from_env(tmp_path)
    run_id, subject_id = uuid4(), uuid4()

    for stage in ("references", "extraction", "synthesis"):
        log.record_model_answer(
            run_id=run_id,
            subject_id=subject_id,
            stage=stage,
            correlation_id="c",
            prompt="p",
            answer="a",
            idempotency_key=f"{stage}-{run_id}-v1",
        )

    files = sorted(p.name for p in (tmp_path / "runs" / str(run_id)).glob("*.txt"))
    assert files == [
        "01-references-model.txt",
        "02-extraction-model.txt",
        "03-synthesis-model.txt",
    ]


def test_runaway_payload_is_truncated(tmp_path: Path) -> None:
    log = DiagnosticsLog.from_env(tmp_path)
    run_id = uuid4()

    log.record(
        event="model.answer",
        run_id=run_id,
        payload="x" * (MAX_PAYLOAD_BYTES + 5000),
        payload_name="huge",
    )

    entry = _events(tmp_path)[0]
    stored = (tmp_path / str(entry["payload_file"])).read_text(encoding="utf-8")
    assert len(stored) < MAX_PAYLOAD_BYTES + 200
    assert "tronqué" in stored


def test_an_unwritable_root_disables_the_trail_instead_of_failing(
    tmp_path: Path,
) -> None:
    """A diagnostics problem must never take a production run down."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")

    log = DiagnosticsLog.from_env(blocker)

    assert log.enabled is False
    log.record(event="ignored", run_id=uuid4())


def test_a_write_failure_is_swallowed(tmp_path: Path) -> None:
    log = DiagnosticsLog.from_env(tmp_path)
    # Replace the index with a directory so appending to it fails.
    (tmp_path / "events.jsonl").mkdir()

    log.record(event="still_fine", run_id=uuid4())  # must not raise

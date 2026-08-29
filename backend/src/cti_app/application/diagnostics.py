"""Diagnostic trail for production pipelines.

Debugging a run means answering "what exactly did the model send back, and what
did the parser make of it?". The artifacts hold the payloads, but reading them
means going through blob ids — too slow when the pipeline is still settling.

This writes a plain-file trail next to the repository instead: one JSONL index
of events, and the raw payloads as ordinary files. It is a development aid, so
it never raises: a failure to log must never fail a production run.

The trail contains raw model output and source URLs. It is local, gitignored,
and must not be shipped anywhere.
"""

from __future__ import annotations

import json
import os
import re
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

# A single answer is a few tens of KB; this only guards against a runaway blob.
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    return _UNSAFE.sub("-", value).strip("-")[:60] or "item"


class DiagnosticsLog:
    """Append-only trail of what the pipeline saw and decided."""

    def __init__(self, root: Path | None) -> None:
        self._root = root

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @classmethod
    def from_env(cls, root: Path | None) -> DiagnosticsLog:
        """Build from configuration, disabling itself if the root is unusable."""
        if root is None:
            return cls(None)
        try:
            root.mkdir(parents=True, exist_ok=True)
            readme = root / "README.md"
            if not readme.exists():
                readme.write_text(
                    "# Journal de diagnostic (local)\n\n"
                    "Écrit par la production CTI. Contient les réponses brutes du modèle\n"
                    "et les décisions du parser. Local et ignoré par git — ne pas diffuser.\n\n"
                    "- `events.jsonl` : un événement par ligne (index)\n"
                    "- `runs/<run_id>/` : payloads bruts, dans l'ordre des étapes\n\n"
                    "Lecture : `make diagnostics`, par exemple\n"
                    '`make diagnostics ARGS="--failures -v"` ou\n'
                    '`make diagnostics ARGS="merge. -n 100"`.\n\n'
                    "Familles d'événements :\n\n"
                    "- `model.*`, `parse.*`, `stage.*` : ce que le modèle a répondu\n"
                    "  et ce que le parser en a fait\n"
                    "- `merge.*` : consolidation cumulative. `merge.needs_review` et\n"
                    "  `merge.plan_invalid` s'arrêtent avant d'appliquer ;\n"
                    "  `merge.resolve_*` couvre la décision humaine (`_applied`,\n"
                    "  `_deferred`, `_stale`, `_already_applied`, `_failed`)\n"
                    "- `http.request_failed` : toute erreur non rattrapée d'une requête,\n"
                    "  avec sa trace — le message rendu au navigateur est volontairement\n"
                    "  vague, c'est ici que se trouve la cause\n",
                    encoding="utf-8",
                )
        except OSError:
            return cls(None)
        return cls(root)

    def _write_payload(self, run_id: UUID, name: str, content: str) -> str | None:
        if self._root is None:
            return None
        try:
            directory = self._root / "runs" / str(run_id)
            directory.mkdir(parents=True, exist_ok=True)
            # Sequence numbers keep the stage order readable in a file listing.
            sequence = len(list(directory.glob("*.txt"))) + 1
            path = directory / f"{sequence:02d}-{_slug(name)}.txt"
            payload = content[:MAX_PAYLOAD_BYTES]
            if len(content) > MAX_PAYLOAD_BYTES:
                payload += "\n\n[... tronqué par le journal de diagnostic ...]"
            path.write_text(payload, encoding="utf-8")
            return str(path.relative_to(self._root))
        except OSError:
            return None

    def record(
        self,
        *,
        event: str,
        run_id: UUID,
        subject_id: UUID | None = None,
        stage: str | None = None,
        correlation_id: str | None = None,
        payload: str | None = None,
        payload_name: str | None = None,
        **fields: Any,
    ) -> None:
        """Append one event, storing `payload` beside it when given."""
        if self._root is None:
            return
        entry: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            "event": event,
            "run_id": str(run_id),
            "pid": os.getpid(),
        }
        if subject_id is not None:
            entry["subject_id"] = str(subject_id)
        if stage is not None:
            entry["stage"] = stage
        if correlation_id is not None:
            entry["correlation_id"] = correlation_id
        entry.update(fields)

        if payload:
            stored = self._write_payload(run_id, payload_name or f"{stage or event}", payload)
            if stored:
                entry["payload_file"] = stored
                entry["payload_bytes"] = len(payload)

        try:
            with (self._root / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            return

    def record_model_answer(
        self,
        *,
        run_id: UUID,
        subject_id: UUID,
        stage: str,
        correlation_id: str,
        prompt: str,
        answer: str,
        idempotency_key: str,
    ) -> None:
        """The exact question asked and the exact answer received."""
        self.record(
            event="model.answer",
            run_id=run_id,
            subject_id=subject_id,
            stage=stage,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            prompt_chars=len(prompt),
            answer_chars=len(answer),
            payload=f"--- PROMPT ---\n{prompt}\n\n--- ANSWER ---\n{answer}",
            payload_name=f"{stage}-model",
        )

    def record_parse(
        self,
        *,
        run_id: UUID,
        subject_id: UUID,
        stage: str,
        correlation_id: str,
        usable: bool,
        warnings: list[str],
        errors: list[str],
        repair_actions: list[str],
        dropped_blocks: list[str],
        **counts: Any,
    ) -> None:
        """What the parser managed to read, and what it had to throw away."""
        payload = None
        if dropped_blocks:
            payload = "\n\n--- BLOC IGNORÉ ---\n\n".join(dropped_blocks)
        self.record(
            event="parse.result",
            run_id=run_id,
            subject_id=subject_id,
            stage=stage,
            correlation_id=correlation_id,
            usable=usable,
            warnings=warnings,
            errors=errors,
            repair_actions=repair_actions,
            dropped_block_count=len(dropped_blocks),
            payload=payload,
            payload_name=f"{stage}-dropped",
            **counts,
        )

    def record_stage_outcome(
        self,
        *,
        run_id: UUID,
        subject_id: UUID,
        stage: str,
        correlation_id: str,
        result: dict[str, Any],
    ) -> None:
        """The decision the stage returned, verbatim minus the bulky fields."""
        # `stage` and the bulky lists are already carried by the entry itself.
        redundant = {"stage", "warnings", "repair_actions"}
        summary = {key: value for key, value in result.items() if key not in redundant}
        self.record(
            event="stage.outcome",
            run_id=run_id,
            subject_id=subject_id,
            stage=stage,
            correlation_id=correlation_id,
            **summary,
        )

    def record_failure(
        self,
        *,
        event: str,
        run_id: UUID,
        stage: str | None = None,
        correlation_id: str | None = None,
        error: BaseException | None = None,
        error_code: str | None = None,
        **fields: Any,
    ) -> None:
        """Why something in the chain stopped, with the traceback kept verbatim.

        Public error messages are deliberately vague ("une erreur interne est
        survenue"); without the traceback stored here, the only remaining copy
        is the container log, which is gone on the next rebuild.
        """
        payload = None
        if error is not None:
            fields.setdefault("error_type", type(error).__name__)
            fields.setdefault("error", str(error))
            payload = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        if error_code is not None:
            fields["error_code"] = error_code
        self.record(
            event=event,
            run_id=run_id,
            stage=stage,
            correlation_id=correlation_id,
            payload=payload,
            payload_name=f"{stage or event}-traceback",
            **fields,
        )

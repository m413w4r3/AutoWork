"""Bounded, sanitized session log and event rings with cursor semantics (§5-§6).

The text ring is bounded to 2 MiB (and a configurable entry cap); the event
ring to 10 000 entries. Both persist as append-only JSONL inside the session
runtime directory, compacted to the retained window when they overflow. A
cursor that fell out of the retained window, or an event cursor from another
generation, raises ``cursor_expired`` with ``resync_required``.

Supervisor-generated content is never logged: only the child's stdout/stderr
and closed lifecycle event payloads enter these rings. Sanitization strips
ANSI/control characters, caps lines at 8 KiB and redacts URLs, credential
assignments, bearer/JWT/long tokens and private-key blocks (the
``_normalize_output`` rule set), and replaces host paths with labels.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import uuid
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .state import CursorExpiredError

LOG_RING_FILE_NAME = "logs.jsonl"
EVENT_RING_FILE_NAME = "events.jsonl"
RING_FILE_MODE = 0o600
TEMP_FILE_SUFFIX = ".tmp"
LOG_RING_MAX_BYTES = 2 * 1024 * 1024
LOG_RING_MAX_ENTRIES = 10_000
EVENT_RING_MAX_ENTRIES = 10_000
EVENT_RING_MAX_BYTES = 16 * 1024 * 1024
LINE_MAX_BYTES = 8 * 1024
MAX_PAGE_LIMIT = 1000
DEFAULT_PAGE_LIMIT = 100

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)[\"']?\b(?:"
    r"api[_ -]?key|access[_ -]?token|auth[_ -]?token|secret|password|passwd|"
    r"authorization|private[_ -]?key)\b[\"']?\s*[:=]\s*[\"']?[^\"'\s,;}]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_TOKEN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_+/=-]{40,}\b")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)


class EventKind(StrEnum):
    """Closed session event-kind set (§6)."""

    STATE_CHANGED = "state_changed"
    INSTRUCTION_ACCEPTED = "instruction_accepted"
    INSTRUCTION_DELIVERED = "instruction_delivered"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_DENIED = "tool_call_denied"
    OUTPUT_CHUNK = "output_chunk"
    PROCESS_STARTED = "process_started"
    PROCESS_EXITED = "process_exited"
    BUDGET_LIMIT_REACHED = "budget_limit_reached"
    CAPTURE_COMPLETED = "capture_completed"
    RECONCILE_COMPLETED = "reconcile_completed"


@dataclass(frozen=True)
class LogLine:
    seq: int
    generation: int
    stream: str
    text: str
    truncated: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "stream": self.stream,
            "text": self.text,
            "truncated": self.truncated,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "generation": self.generation,
            "stream": self.stream,
            "text": self.text,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class SessionEvent:
    seq: int
    generation: int
    kind: EventKind
    at: str
    payload: dict[str, object]

    def to_wire(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "kind": str(self.kind),
            "at": self.at,
            "payload": dict(self.payload),
        }

    def to_record(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "generation": self.generation,
            "kind": str(self.kind),
            "at": self.at,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class LogPage:
    lines: tuple[LogLine, ...]
    next_cursor: int
    oldest_retained: int


@dataclass(frozen=True)
class EventPage:
    events: tuple[SessionEvent, ...]
    next_cursor: int
    oldest_retained: int
    generation: int
    resync_required: bool


def sanitize_line(
    raw: bytes,
    *,
    line_max_bytes: int = LINE_MAX_BYTES,
    replacements: Sequence[tuple[str, str]] = (),
) -> tuple[str, bool]:
    """Normalize one log line and return ``(text, truncated)``."""
    text = raw.decode("utf-8", errors="replace")
    for value, label in replacements:
        if value:
            text = text.replace(value, label)
    text = _CONTROL_CHARACTERS.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _URL_TEXT.sub("<url-redacted>", text)
    text = _CREDENTIAL_ASSIGNMENT.sub("<credential-redacted>", text)
    text = _BEARER_TOKEN.sub("Bearer <credential-redacted>", text)
    text = _JWT_TOKEN.sub("<token-redacted>", text)
    text = _LONG_TOKEN.sub("<token-redacted>", text)
    text = _PRIVATE_KEY_BLOCK.sub("<private-key-redacted>", text)
    encoded = text.rstrip("\n").encode("utf-8")
    truncated = False
    if len(encoded) > line_max_bytes:
        encoded = encoded[:line_max_bytes]
        truncated = True
    return encoded.decode("utf-8", errors="ignore"), truncated


class _PersistentRing:
    """Append-only JSONL ring shared by the log and session event logs."""

    def __init__(
        self,
        path: Path,
        *,
        generation: int,
        max_bytes: int,
        max_entries: int,
        records: Iterable[Mapping[str, object]] = (),
        last_seq: int = 0,
    ) -> None:
        if max_bytes < 1 or max_entries < 1:
            raise ValueError("ring limits must be positive")
        self.path = path
        self._generation = generation
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._records: deque[dict[str, object]] = deque()
        self._sizes: deque[int] = deque()
        self._total_bytes = 0
        self._last_seq = last_seq
        for record in records:
            self._retain(dict(record))

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def oldest_retained(self) -> int:
        if not self._records:
            return self._last_seq + 1
        value = self._records[0].get("seq")
        return value if isinstance(value, int) else self._last_seq + 1

    def records(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(record) for record in self._records)

    def append(self, record: Mapping[str, object]) -> int:
        with self._lock:
            seq = self._last_seq + 1
            entry = {"seq": seq, **{key: value for key, value in record.items() if key != "seq"}}
            entry["generation"] = self._generation
            pruned = self._retain(entry)
            self._last_seq = seq
            self._append_disk(entry)
            if pruned:
                self._rewrite_disk()
            return seq

    def advance_generation(self, generation: int, *, drop_older: bool) -> None:
        with self._lock:
            self._generation = generation
            if drop_older:
                retained = [
                    record for record in self._records if record.get("generation") == generation
                ]
                self._reset(retained)
                self._rewrite_disk()

    def _retain(self, record: dict[str, object]) -> bool:
        size = len(json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode())
        self._records.append(record)
        self._sizes.append(size)
        self._total_bytes += size
        pruned = False
        while self._records and (
            self._total_bytes > self._max_bytes or len(self._records) > self._max_entries
        ):
            self._records.popleft()
            self._total_bytes -= self._sizes.popleft()
            pruned = True
        return pruned

    def _reset(self, records: Iterable[dict[str, object]]) -> None:
        self._records.clear()
        self._sizes.clear()
        self._total_bytes = 0
        for record in records:
            self._retain(record)

    def _append_disk(self, record: Mapping[str, object]) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        parent_fd = os.open(
            self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            fd = os.open(
                self.path.name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                RING_FILE_MODE,
                dir_fd=parent_fd,
            )
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def _rewrite_disk(self) -> None:
        payload = b"".join(
            json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
            for record in self._records
        )
        parent_fd = os.open(
            self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        temporary = f".{self.path.name}.{uuid.uuid4().hex}{TEMP_FILE_SUFFIX}"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                RING_FILE_MODE,
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(fd, RING_FILE_MODE)
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(temporary, self.path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(parent_fd)

    def _require_cursor(self, after: int) -> None:
        if after < 0 or after > self._last_seq or after < self.oldest_retained - 1:
            raise CursorExpiredError(
                generation=self._generation,
                oldest_retained=self.oldest_retained,
                last_seq=self._last_seq,
                after=after,
            )


class LogRing(_PersistentRing):
    """The bounded, sanitized child output ring (2 MiB default)."""

    def __init__(
        self,
        path: Path,
        *,
        generation: int,
        max_bytes: int = LOG_RING_MAX_BYTES,
        max_entries: int = LOG_RING_MAX_ENTRIES,
        records: Iterable[Mapping[str, object]] = (),
        last_seq: int = 0,
    ) -> None:
        super().__init__(
            path,
            generation=generation,
            max_bytes=max_bytes,
            max_entries=max_entries,
            records=records,
            last_seq=last_seq,
        )

    def append_line(self, *, stream: str, text: str, truncated: bool) -> LogLine:
        seq = self.append({"stream": stream, "text": text, "truncated": truncated})
        return LogLine(
            seq=seq,
            generation=self.generation,
            stream=stream,
            text=text,
            truncated=truncated,
        )

    def page(self, after: int, limit: int) -> LogPage:
        if limit < 1 or limit > MAX_PAGE_LIMIT:
            raise ValueError("the page limit is out of range")
        with self._lock:
            self._require_cursor(after)
            selected: list[LogLine] = []
            for record in self._records:
                seq = record.get("seq")
                if not isinstance(seq, int) or seq <= after:
                    continue
                selected.append(_log_line(record))
                if len(selected) == limit:
                    break
            next_cursor = selected[-1].seq if selected else after
            return LogPage(
                lines=tuple(selected),
                next_cursor=next_cursor,
                oldest_retained=self.oldest_retained,
            )


class EventRing(_PersistentRing):
    """The bounded session event ring (10 000 entries default)."""

    def __init__(
        self,
        path: Path,
        *,
        generation: int,
        max_entries: int = EVENT_RING_MAX_ENTRIES,
        max_bytes: int = EVENT_RING_MAX_BYTES,
        records: Iterable[Mapping[str, object]] = (),
        last_seq: int = 0,
    ) -> None:
        super().__init__(
            path,
            generation=generation,
            max_bytes=max_bytes,
            max_entries=max_entries,
            records=records,
            last_seq=last_seq,
        )

    def append_event(
        self, *, kind: EventKind, at: str, payload: Mapping[str, object]
    ) -> SessionEvent:
        seq = self.append({"kind": str(kind), "at": at, "payload": dict(payload)})
        return SessionEvent(
            seq=seq, generation=self.generation, kind=kind, at=at, payload=dict(payload)
        )

    def page(self, after: int, limit: int, generation: int | None) -> EventPage:
        if limit < 1 or limit > MAX_PAGE_LIMIT:
            raise ValueError("the page limit is out of range")
        with self._lock:
            if generation is not None and generation != self._generation:
                raise CursorExpiredError(
                    generation=self._generation,
                    oldest_retained=self.oldest_retained,
                    last_seq=self._last_seq,
                    after=after,
                )
            self._require_cursor(after)
            selected: list[SessionEvent] = []
            for record in self._records:
                seq = record.get("seq")
                if not isinstance(seq, int) or seq <= after:
                    continue
                selected.append(_session_event(record))
                if len(selected) == limit:
                    break
            next_cursor = selected[-1].seq if selected else after
            return EventPage(
                events=tuple(selected),
                next_cursor=next_cursor,
                oldest_retained=self.oldest_retained,
                generation=self._generation,
                resync_required=False,
            )


def load_ring_records(
    path: Path, *, max_bytes: int, max_entries: int
) -> tuple[tuple[dict[str, object], ...], int]:
    """Read the retained tail of a JSONL ring; malformed lines are ignored."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return (), 0
    except OSError:
        return (), 0
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return (), 0
        read_limit = min(info.st_size, max(4 * max_bytes, 2 * 1024 * 1024))
        start = info.st_size - read_limit
        os.lseek(fd, start, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = read_limit
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    body = b"".join(chunks)
    if start > 0:
        _, _, body = body.partition(b"\n")
    records: list[dict[str, object]] = []
    last_seq = 0
    for line in body.split(b"\n"):
        if not line:
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        seq = value.get("seq")
        generation = value.get("generation")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            continue
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            continue
        records.append({str(key): item for key, item in value.items()})
        last_seq = max(last_seq, seq)
    return tuple(records[-max_entries:]), last_seq


def _log_line(record: Mapping[str, object]) -> LogLine:
    return LogLine(
        seq=_int(record.get("seq")),
        generation=_int(record.get("generation")),
        stream=str(record.get("stream", "stdout")),
        text=str(record.get("text", "")),
        truncated=bool(record.get("truncated", False)),
    )


def _session_event(record: Mapping[str, object]) -> SessionEvent:
    raw_kind = record.get("kind")
    try:
        kind = EventKind(str(raw_kind))
    except ValueError:
        kind = EventKind.STATE_CHANGED
    raw_payload = record.get("payload")
    payload = (
        {str(key): value for key, value in raw_payload.items()}
        if isinstance(raw_payload, dict)
        else {}
    )
    return SessionEvent(
        seq=_int(record.get("seq")),
        generation=_int(record.get("generation")),
        kind=kind,
        at=str(record.get("at", "")),
        payload=payload,
    )


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0

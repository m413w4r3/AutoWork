#!/usr/bin/env python3
"""Mechanical file-to-API relay. It holds no token; the backend container does."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any


RELAY_VERSION = "2.0"
ROOT = pathlib.Path(__file__).parent
DEFAULT_EXECUTOR = [
    "docker",
    "exec",
    "-i",
    "cti-bulletin-backend-1",
    "python",
    "-c",
]
GET_OK = re.compile(r"^/[A-Za-z0-9_\-/.?=&%:,+]*$")
TOOL_OK = re.compile(
    r"^/operator/(tools/[a-z_.]+|invocations/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/reconcile)$"
)
INVESTIGATION_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

SCRIPT = r'''
import os,sys,json,urllib.request,urllib.error
q=json.load(sys.stdin)
h={"Authorization":"Bearer "+os.environ["OPERATOR_CLIENT_API_TOKEN"],"Content-Type":"application/json"}
if q.get("idem"): h["Idempotency-Key"]=q["idem"]
d=json.dumps(q["body"]).encode() if q.get("body") is not None else None
r=urllib.request.Request("http://127.0.0.1:8000"+q["path"],data=d,method=q["method"],headers=h)
try:
    x=urllib.request.urlopen(r,timeout=q["timeout"]); out={"http":x.status,"body":x.read().decode()}
except urllib.error.HTTPError as e: out={"http":e.code,"body":e.read().decode()}
except Exception as e: out={"http":None,"body":"","transport_error":type(e).__name__,"ambiguous":True}
sys.stdout.write(json.dumps(out))
'''


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temp:
            temp_name = temp.name
            json.dump(value, temp, ensure_ascii=False)
            temp.write("\n")
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


class Request:
    def __init__(self, path: pathlib.Path, payload: Mapping[str, Any]):
        self.file = path
        raw_id = str(payload.get("id", path.stem))
        self.id = re.sub(r"[^A-Za-z0-9_\-]", "", raw_id)[:80] or re.sub(
            r"[^A-Za-z0-9_\-]", "", path.stem
        )[:80]
        self.payload = dict(payload)
        self.method = str(payload.get("method", "")).upper()
        self.relative_path = str(payload.get("path", ""))
        self.api_path = ""
        self.received_at = utc_now()
        self.started_at: str | None = None
        self.state = "received"
        self.allowed = bool(
            self.id
            and GET_OK.fullmatch(self.relative_path)
            and ".." not in self.relative_path
            and (
                self.method == "GET"
                or (self.method == "POST" and TOOL_OK.fullmatch(self.relative_path))
            )
        )

    def status_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "method": self.method,
            "path": self.api_path or self.relative_path,
            "received_at": self.received_at,
            "started_at": self.started_at,
            "status": self.state,
        }


Executor = Callable[[dict[str, Any]], dict[str, Any]]


class Relay:
    def __init__(
        self,
        workdir: pathlib.Path | str,
        investigation_id: str,
        *,
        executor: Executor | None = None,
        executor_command: list[str] | None = None,
        get_workers: int = 4,
        poll_interval: float = 0.1,
    ):
        if not INVESTIGATION_ID_OK.fullmatch(investigation_id):
            raise ValueError("investigation ID must contain 1-80 letters, digits, '_' or '-'")
        if get_workers < 1:
            raise ValueError("get_workers must be at least 1")
        self.root = pathlib.Path(workdir).expanduser().resolve()
        self.inv = investigation_id
        self.executor_command = executor_command
        self.executor = executor or self._execute_container
        self.get_workers = get_workers
        self.poll_interval = poll_interval
        self.pid = os.getpid()
        self.stop_event = threading.Event()
        self.pending: list[Request] = []
        self.active: dict[str, tuple[Request, concurrent.futures.Future[dict[str, Any]] | None]] = {}
        self.seen_files: set[pathlib.Path] = set()
        self.last_completed_id: str | None = None
        self._last_heartbeat = 0.0
        self._get_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._post_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        for name in ("inbox", "outbox", "inflight", "processed"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def _execute_container(self, query: dict[str, Any]) -> dict[str, Any]:
        command = self.executor_command
        if command is None:
            command = [*DEFAULT_EXECUTOR, SCRIPT]
        completed = subprocess.run(
            command,
            input=json.dumps(query),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return {
                "http": None,
                "body": "",
                "transport_error": "ExecutorExit",
                "ambiguous": True,
            }
        try:
            output = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return {
                "http": None,
                "body": "",
                "relay_error": "bad_container_output",
                "ambiguous": query["method"] == "POST",
            }
        if not isinstance(output, dict):
            return {
                "http": None,
                "body": "",
                "relay_error": "bad_container_output",
                "ambiguous": query["method"] == "POST",
            }
        if output.get("transport_error"):
            output["ambiguous"] = True
        return output

    def request_stop(self) -> None:
        self.stop_event.set()

    def _inflight_path(self, request: Request) -> pathlib.Path:
        return self.root / "inflight" / f"{request.id}.json"

    def _persist_request(self, request: Request) -> None:
        atomic_json(self._inflight_path(request), request.status_record())

    def _status(self) -> dict[str, Any]:
        active = [
            request.status_record()
            for request, _future in self.active.values()
            if request.state == "running"
        ]
        queued = [request.status_record() for request in self.pending]
        return {
            "pid": self.pid,
            "heartbeat_ts": utc_now(),
            "relay_version": RELAY_VERSION,
            "investigation_id": self.inv,
            "in_flight": active,
            "queue": queued,
            "last_completed_id": self.last_completed_id,
        }

    def _write_status(self, *, heartbeat: bool = False) -> None:
        now = time.monotonic()
        if heartbeat and now - self._last_heartbeat < 5:
            return
        atomic_json(self.root / "relay-status.json", self._status())
        self._last_heartbeat = now

    def _receive_files(self) -> None:
        for path in sorted((self.root / "inbox").glob("*.json")):
            if path in self.seen_files:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # Writers may still be finishing the request file; retry next pass.
                continue
            if not isinstance(payload, dict):
                payload = {}
            request = Request(path, payload)
            request.api_path = f"/api/investigations/{self.inv}{request.relative_path}"
            self.seen_files.add(path)
            self._persist_request(request)
            if request.allowed:
                self.pending.append(request)
                self._write_status()
            else:
                self._write_status()
                self._finish(request, {"http": None, "rejected": "path_or_method_not_allowed"})

    def _query(self, request: Request) -> dict[str, Any]:
        return {
            "id": request.id,
            "method": request.method,
            "path": request.api_path,
            "body": request.payload.get("body"),
            "idem": f"m07-{request.id}" if request.method == "POST" else None,
            "timeout": 7400 if request.method == "POST" else 300,
        }

    def _start(self, request: Request) -> None:
        request.state = "running"
        request.started_at = utc_now()
        self.pending.remove(request)
        self.active[request.id] = (request, None)
        self._persist_request(request)
        self._write_status()
        pool = self._post_pool if request.method == "POST" else self._get_pool
        assert pool is not None
        future = pool.submit(self.executor, self._query(request))
        self.active[request.id] = (request, future)

    def _dispatch(self) -> None:
        running_gets = sum(req.method == "GET" for req, _future in self.active.values())
        for request in list(self.pending):
            if request.method == "GET" and running_gets < self.get_workers:
                self._start(request)
                running_gets += 1

        if any(req.method == "POST" for req, _future in self.active.values()):
            return
        for request in self.pending:
            if request.method == "POST":
                self._start(request)
                break

    def _base_log_entry(self, request: Request) -> dict[str, Any]:
        payload = request.payload
        body = payload.get("body") or {}
        return {
            "ts": utc_now(),
            "id": request.id,
            "agent": payload.get("agent", "CTI-ANALYST"),
            "method": request.method,
            "path": request.relative_path,
            "purpose": body.get("purpose") if request.method == "POST" else payload.get("purpose"),
            "arguments_sha256": hashlib.sha256(
                json.dumps(body.get("arguments"), sort_keys=True).encode()
            ).hexdigest()
            if request.method == "POST"
            else None,
            "context": payload.get("context"),
        }

    def _write_journal(self, entry: dict[str, Any]) -> None:
        with (self.root / "journal.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _finish(self, request: Request, output: dict[str, Any]) -> None:
        output.setdefault("body", "")
        body = output.get("body", "")
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False)
            output["body"] = body
        if output.get("transport_error"):
            output["ambiguous"] = True

        entry = self._base_log_entry(request)
        entry.update(
            {
                "http": output.get("http"),
                "response_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "response_bytes": len(body),
                "ambiguous": output.get("ambiguous", False),
            }
        )
        try:
            parsed = json.loads(body)
            invocation = parsed.get("invocation") or {}
            if invocation:
                entry["invocation_id"] = invocation.get("id")
                entry["invocation_status"] = invocation.get("status")
            entry["working_state_version"] = parsed.get("working_state_version")
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        atomic_json(self.root / "outbox" / f"{request.id}.json", output)
        self._write_journal(entry)
        self._inflight_path(request).unlink(missing_ok=True)
        if request.file.exists():
            destination = self.root / "processed" / request.file.name
            if destination.exists():
                destination = destination.with_name(f"{destination.name}.{time.time_ns()}")
            request.file.replace(destination)
        self.active.pop(request.id, None)
        if request in self.pending:
            self.pending.remove(request)
        self.seen_files.discard(request.file)
        self.last_completed_id = request.id
        self._write_status()

    def _collect_completed(self) -> None:
        for request_id, (request, future) in list(self.active.items()):
            if future is None or not future.done():
                continue
            try:
                output = future.result()
                if not isinstance(output, dict):
                    output = {
                        "http": None,
                        "relay_error": "bad_executor_output",
                        "ambiguous": request.method == "POST",
                    }
            except Exception as error:  # The request may have reached the API.
                output = {
                    "http": None,
                    "body": "",
                    "transport_error": type(error).__name__,
                    "ambiguous": True,
                }
            self._finish(request, output)

    def run(self) -> None:
        self._get_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.get_workers, thread_name_prefix="relay-get"
        )
        self._post_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="relay-post"
        )
        self._write_status()
        while True:
            if not self.stop_event.is_set():
                self._receive_files()
            self._collect_completed()
            self._dispatch()
            self._write_status(heartbeat=True)
            if self.stop_event.is_set() and not self.pending and not self.active:
                break
            if self.stop_event.is_set():
                time.sleep(self.poll_interval)
            else:
                self.stop_event.wait(self.poll_interval)
        assert self._get_pool is not None and self._post_pool is not None
        self._get_pool.shutdown(wait=True)
        self._post_pool.shutdown(wait=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="file-to-API relay without stored credentials")
    parser.add_argument(
        "--investigation-id",
        default=os.environ.get("RELAY_INVESTIGATION_ID"),
        help="investigation ID (or set RELAY_INVESTIGATION_ID)",
    )
    parser.add_argument(
        "--workdir",
        default=os.environ.get("RELAY_WORKDIR", str(ROOT)),
        help="directory containing inbox/, outbox/, processed/ and relay-status.json",
    )
    parser.add_argument(
        "--executor-command",
        default=os.environ.get("RELAY_EXECUTOR_COMMAND"),
        help="optional executor command for offline harnesses; receives one JSON request on stdin",
    )
    args = parser.parse_args(argv)
    if not args.investigation_id:
        parser.error("missing investigation ID: pass --investigation-id or set RELAY_INVESTIGATION_ID")
    if not INVESTIGATION_ID_OK.fullmatch(args.investigation_id):
        parser.error("invalid investigation ID: use 1-80 letters, digits, '_' or '-'")

    command = shlex.split(args.executor_command) if args.executor_command else None
    relay = Relay(args.workdir, args.investigation_id, executor_command=command)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: relay.request_stop())
    signal.signal(signal.SIGINT, lambda _signum, _frame: relay.request_stop())
    try:
        relay.run()
    except KeyboardInterrupt:
        relay.request_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

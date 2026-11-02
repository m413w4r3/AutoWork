import json
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timezone


DEFAULT_ROOT = pathlib.Path("/home/nill/perso/work/AutoWork/.autowork-runs/m07")
R = pathlib.Path(os.environ.get("RELAY_WORKDIR", str(DEFAULT_ROOT)))


def _workdir(workdir=None):
    return pathlib.Path(workdir) if workdir is not None else R


def _write_request(request_id, request, workdir=None):
    root = _workdir(workdir)
    inbox = root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{request_id}.json"
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=inbox, prefix=f".{request_id}.", suffix=".tmp", delete=False
        ) as temp:
            temp_name = temp.name
            json.dump(request, temp, ensure_ascii=False)
            temp.write("\n")
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


def _journal_record(root, request_id):
    journal = root / "journal.jsonl"
    if not journal.exists():
        return {}
    try:
        for line in reversed(journal.read_text(encoding="utf-8").splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("request_id", record.get("id")) == request_id:
                return record
    except OSError:
        return {}
    return {}


def _elapsed_seconds(started_at):
    if not isinstance(started_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return round(max(0.0, (datetime.now(timezone.utc) - started).total_seconds()), 3)


def request_status(request_id, workdir=None):
    """Return the current relay state and timing metadata for one request."""
    root = _workdir(workdir)
    outbox = root / "outbox" / f"{request_id}.json"
    if outbox.exists():
        try:
            output = json.loads(outbox.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            output = {}
        state = "ambiguous" if output.get("ambiguous") else "completed"
        record = _journal_record(root, request_id)
    else:
        record = {}
        state = "not_seen"
    status = root / "relay-status.json"
    if state == "not_seen" and status.exists():
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        for item in data.get("in_flight", []):
            if item.get("id") == request_id:
                record = item
                state = "running"
                break
        if state == "not_seen":
            for item in data.get("queue", []):
                if item.get("id") == request_id:
                    record = item
                    state = "queued"
                    break
    if state == "not_seen":
        inflight = root / "inflight" / f"{request_id}.json"
        if inflight.exists():
            try:
                record = json.loads(inflight.read_text(encoding="utf-8"))
                state = record.get("status", "received")
            except (json.JSONDecodeError, OSError):
                record = {}

    lane = record.get("lane")
    position = record.get("queue_position")
    if state == "received":
        message = "Received by the relay; waiting for dispatch."
    elif state == "queued":
        lane_name = (lane or "request").upper()
        position_text = (
            f"queue position {position}" if position is not None else "queue position unknown"
        )
        message = f"Queued; waiting for a {lane_name} slot ({position_text})."
    elif state == "running":
        message = f"Running in the {(lane or 'request').upper()} lane."
    elif state == "completed":
        message = "Completed."
    elif state == "ambiguous":
        message = (
            "Ambiguous transport outcome; the relay did not resend it. "
            "Do not resubmit blindly."
        )
    else:
        message = "No relay record found yet."

    fields = (
        "received_at",
        "started_at",
        "completed_at",
        "queue_wait_seconds",
        "run_seconds",
        "lane",
        "request_id",
        "idempotency_key_sha256",
        "queue_position",
    )
    result = {key: record.get(key) for key in fields}
    if state in {"received", "queued"} and record.get("started_at") is None:
        elapsed_queue = _elapsed_seconds(record.get("received_at"))
        if elapsed_queue is not None:
            result["queue_wait_seconds"] = elapsed_queue
    if state == "running":
        elapsed_run = _elapsed_seconds(record.get("started_at"))
        if elapsed_run is not None:
            result["run_seconds"] = elapsed_run
    result.update({"status": state, "message": message})
    return result


def request_state(request_id, workdir=None):
    """Return not_seen, received, queued, running, completed, or ambiguous."""
    return request_status(request_id, workdir).get("status", "not_seen")


def _print_status(request_id, status):
    def value(name):
        item = status.get(name)
        return "-" if item is None else item

    details = " ".join(
        f"{name}={value(name)}"
        for name in (
            "received_at",
            "started_at",
            "completed_at",
            "queue_wait_seconds",
            "run_seconds",
            "lane",
            "request_id",
            "idempotency_key_sha256",
            "queue_position",
        )
    )
    print(
        f"relay {request_id}: {status['message']} {details}",
        file=sys.stdout,
        flush=True,
    )


def _wait(request_id, seconds, workdir=None):
    root = _workdir(workdir)
    last_signature = None
    for _ in range(seconds):
        time.sleep(1)
        status = request_status(request_id, workdir)
        signature = (status["status"], status.get("queue_position"))
        if status["status"] != "not_seen" and signature != last_signature:
            _print_status(request_id, status)
            last_signature = signature
        result = root / "outbox" / f"{request_id}.json"
        if result.exists():
            output = json.loads(result.read_text(encoding="utf-8"))
            return output.get("http"), output.get("body", "")
    raise TimeoutError(
        f"relay request {request_id} timed out; "
        f"{request_status(request_id, workdir)['message']} do not resubmit blindly"
    )


def get(i, path, purpose="tool health smoke", workdir=None):
    _write_request(
        i,
        {"id": i, "agent": "ORCHESTRATOR", "method": "GET", "path": path, "purpose": purpose},
        workdir,
    )
    return _wait(i, 120, workdir)


def post(i, tool, body, workdir=None):
    _write_request(
        i,
        {
            "id": i,
            "agent": "ORCHESTRATOR",
            "method": "POST",
            "path": f"/operator/tools/{tool}",
            "body": body,
        },
        workdir,
    )
    return _wait(i, 3600, workdir)

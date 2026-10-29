import json
import os
import pathlib
import tempfile
import time


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


def request_state(request_id, workdir=None):
    """Return not_seen, received, running, queued, or completed for one relay ID."""
    root = _workdir(workdir)
    if (root / "outbox" / f"{request_id}.json").exists():
        return "completed"
    status = root / "relay-status.json"
    if status.exists():
        data = json.loads(status.read_text(encoding="utf-8"))
        if any(item.get("id") == request_id for item in data.get("in_flight", [])):
            return "running"
        if any(item.get("id") == request_id for item in data.get("queue", [])):
            return "queued"
    inflight = root / "inflight" / f"{request_id}.json"
    if inflight.exists():
        return json.loads(inflight.read_text(encoding="utf-8")).get("status", "received")
    return "not_seen"


def _wait(request_id, seconds, workdir=None):
    root = _workdir(workdir)
    for _ in range(seconds):
        time.sleep(1)
        result = root / "outbox" / f"{request_id}.json"
        if result.exists():
            output = json.loads(result.read_text(encoding="utf-8"))
            return output.get("http"), output.get("body", "")
    raise TimeoutError(
        f"relay request {request_id} timed out; state={request_state(request_id, workdir)}; do not resubmit blindly"
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

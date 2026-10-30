"""Tiny client for the file relay (no credentials; the relay holds none).

    import opclient as oc
    oc.AGENT = "CTI-OPERATOR-A"            # your label, written to the journal
    h, body = oc.get("a001-status", "/operator-status", purpose="read current state", context={...})
    h, body = oc.post("a002-x", "tool.name", {"schema_version": "1", "arguments": {...},
                      "purpose": "...", "expected_version": N})

* Every request id must be unique. A POST can last many minutes (Bridge: 10-20 min).
* GETs run in parallel with a POST; POSTs are serialized. Never resend a POST that timed out:
  call state(id) and read relay-status.json (do NOT use pgrep/ps; the sandbox cannot see host processes).
* Paths are relative to /api/investigations/<INV>.
"""
import json, os, pathlib, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent
AGENT = "CTI-OPERATOR-D"


def _write(i, req):
    inbox = ROOT / "inbox"; inbox.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=inbox, prefix=f".{i}.", suffix=".tmp", delete=False) as t:
        json.dump(req, t, ensure_ascii=False); name = t.name
    os.replace(name, inbox / f"{i}.json")


def state(i):
    if (ROOT / "outbox" / f"{i}.json").exists():
        return "completed"
    try:
        d = json.loads((ROOT / "relay-status.json").read_text())
        if any(x.get("id") == i for x in d.get("in_flight", [])): return "running"
        if any(x.get("id") == i for x in d.get("queue", [])): return "queued"
    except Exception:
        pass
    p = ROOT / "inflight" / f"{i}.json"
    return json.loads(p.read_text()).get("status", "received") if p.exists() else "not_seen"


def _wait(i, seconds):
    for _ in range(seconds):
        time.sleep(1)
        f = ROOT / "outbox" / f"{i}.json"
        if f.exists():
            o = json.loads(f.read_text()); return o.get("http"), o.get("body", "")
    raise TimeoutError(f"{i} not finished; state={state(i)}; do not resubmit blindly")


def get(i, path, purpose=None, context=None, wait=300):
    _write(i, {"id": i, "agent": AGENT, "method": "GET", "path": path, "purpose": purpose, "context": context})
    return _wait(i, wait)


def post(i, tool, body, context=None, wait=3600):
    _write(i, {"id": i, "agent": AGENT, "method": "POST", "path": f"/operator/tools/{tool}", "body": body, "context": context})
    return _wait(i, wait)

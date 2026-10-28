#!/usr/bin/env python3
"""Mechanical file->API relay for the CTI-ANALYST. Holds no token: the call runs inside the
backend container, which reads OPERATOR_CLIENT_API_TOKEN from its own environment.
Whitelist: GET /api/investigations/<INV>/..., POST .../operator/tools/<tool>. Never retries."""
import hashlib, json, re, subprocess, sys, time, pathlib, datetime
ROOT = pathlib.Path(__file__).parent
INV = "a8845772-5025-4702-8850-2b6be776855b"
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
GET_OK = re.compile(r"^/[A-Za-z0-9_\-/.?=&%:,+]*$")
TOOL_OK = re.compile(r"^/operator/tools/[a-z_.]+$")

def log(entry):
    with open(ROOT / "journal.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def handle(p):
    req = json.loads(p.read_text())
    rid = re.sub(r"[^A-Za-z0-9_\-]", "", str(req["id"]))[:80]
    method, rel = req["method"].upper(), req["path"]
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ok = GET_OK.fullmatch(rel) and ".." not in rel and (
        (method == "GET") or (method == "POST" and TOOL_OK.fullmatch(rel)))
    entry = {"ts": ts, "id": rid, "agent": req.get("agent", "CTI-ANALYST"), "method": method, "path": rel,
             "purpose": (req.get("body") or {}).get("purpose") if method == "POST" else req.get("purpose"),
             "arguments_sha256": hashlib.sha256(json.dumps((req.get("body") or {}).get("arguments"), sort_keys=True).encode()).hexdigest() if method == "POST" else None,
             "context": req.get("context")}
    if not ok:
        out = {"http": None, "rejected": "path_or_method_not_allowed"}
    else:
        q = {"method": method, "path": f"/api/investigations/{INV}{rel}", "body": req.get("body"),
             "idem": f"m07-{rid}" if method == "POST" else None, "timeout": 7400 if method == "POST" else 300}
        r = subprocess.run(["docker", "exec", "-i", "cti-bulletin-backend-1", "python", "-c", SCRIPT],
                           input=json.dumps(q), capture_output=True, text=True)
        try:
            out = json.loads(r.stdout)
        except Exception:
            out = {"http": None, "relay_error": "bad_container_output", "ambiguous": method == "POST"}
    body = out.get("body", "")
    entry.update({"http": out.get("http"), "response_sha256": hashlib.sha256(body.encode()).hexdigest(),
                  "response_bytes": len(body), "ambiguous": out.get("ambiguous", False)})
    try:
        b = json.loads(body)
        inv = b.get("invocation") or {}
        if inv:
            entry["invocation_id"] = inv.get("id"); entry["invocation_status"] = inv.get("status")
        entry["working_state_version"] = b.get("working_state_version")
    except Exception:
        pass
    (ROOT / "outbox" / f"{rid}.json").write_text(json.dumps(out, ensure_ascii=False))
    log(entry)
    p.rename(ROOT / "processed" / p.name)

while True:
    for p in sorted((ROOT / "inbox").glob("*.json")):
        try:
            handle(p)
        except Exception as e:
            log({"ts": time.time(), "relay_exception": type(e).__name__, "file": p.name})
            p.rename(ROOT / "processed" / (p.name + ".err"))
    time.sleep(1.5)

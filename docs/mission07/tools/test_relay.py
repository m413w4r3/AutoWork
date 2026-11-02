import hashlib
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from relay import Relay
from relayget import _wait, request_state, request_status


class FakeExecutor:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.started = {}
        self.release = {}
        self.active_posts = 0
        self.max_active_posts = 0

    def __call__(self, request):
        rid = request["id"]
        with self.lock:
            self.calls.append(request)
            started = self.started.setdefault(rid, threading.Event())
            release = self.release.setdefault(rid, threading.Event())
            if request["method"] == "POST":
                self.active_posts += 1
                self.max_active_posts = max(self.max_active_posts, self.active_posts)
        started.set()
        if request["method"] == "POST":
            release.wait(3)
            with self.lock:
                self.active_posts -= 1
        return {"http": 200, "body": json.dumps({"working_state_version": 7})}

    def event(self, request_id):
        with self.lock:
            return self.started.setdefault(request_id, threading.Event())

    def unblock(self, request_id):
        with self.lock:
            event = self.release.setdefault(request_id, threading.Event())
        event.set()


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.executor = FakeExecutor()
        self.relay = Relay(
            self.root,
            "a8845772-5025-4702-8850-2b6be776855b",
            executor=self.executor,
            get_workers=3,
            poll_interval=0.01,
        )
        self.thread = threading.Thread(target=self.relay.run, daemon=True)
        self.thread.start()

    def tearDown(self):
        for request_id in ("m07-p-post", "m07-p-first", "m07-p-second"):
            self.executor.unblock(request_id)
        self.relay.request_stop()
        self.thread.join(timeout=5)
        self.tempdir.cleanup()

    def submit(self, rid, method, path):
        request = {"id": rid, "agent": "unittest", "method": method, "path": path}
        if method == "POST":
            request["body"] = {"purpose": "offline test", "arguments": {}}
        (self.root / "inbox" / f"{rid}.json").write_text(json.dumps(request))

    def wait_for(self, path, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return json.loads(path.read_text())
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")

    def test_get_runs_while_post_is_blocked_and_status_is_visible(self):
        self.submit("m07-p-post", "POST", "/operator/tools/advisor.consult")
        self.assertTrue(self.executor.event("m07-p-post").wait(2))
        inflight = self.root / "inflight" / "m07-p-post.json"
        running = json.loads(inflight.read_text())
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["lane"], "post")
        self.assertEqual(running["request_id"], "m07-p-post")
        self.assertIsNone(running["completed_at"])
        self.assertIsNotNone(running["started_at"])
        self.assertIsNotNone(running["queue_wait_seconds"])
        self.assertIsNotNone(running["run_seconds"])
        running_status = request_status("m07-p-post", self.root)
        self.assertEqual(running_status["status"], "running")
        self.assertIn("Running in the POST lane", running_status["message"])
        self.assertEqual(
            running["idempotency_key_sha256"],
            hashlib.sha256(b"m07-m07-p-post").hexdigest(),
        )

        self.submit("m07-get", "GET", "/operator-status")
        self.assertTrue(self.executor.event("m07-get").wait(2))
        result = self.wait_for(self.root / "outbox" / "m07-get.json")
        self.assertEqual(result["http"], 200)
        journal = [
            json.loads(line) for line in (self.root / "journal.jsonl").read_text().splitlines()
        ]
        get_entry = next(entry for entry in journal if entry["id"] == "m07-get")
        self.assertEqual(get_entry["lane"], "get")
        self.assertIsNone(get_entry["idempotency_key_sha256"])

        status = json.loads((self.root / "relay-status.json").read_text())
        self.assertEqual(status["pid"], self.relay.pid)
        self.assertEqual(status["relay_version"], "2.0")
        self.assertEqual(status["investigation_id"], "a8845772-5025-4702-8850-2b6be776855b")
        self.assertTrue(status["heartbeat_ts"])
        self.assertEqual([item["id"] for item in status["in_flight"]], ["m07-p-post"])
        self.assertEqual(status["in_flight"][0]["lane"], "post")
        self.assertIsNotNone(status["in_flight"][0]["received_at"])

        self.executor.unblock("m07-p-post")
        self.wait_for(self.root / "outbox" / "m07-p-post.json")
        self.assertFalse(inflight.exists())
        completed_status = json.loads((self.root / "relay-status.json").read_text())
        self.assertEqual(completed_status["last_completed_id"], "m07-p-post")
        self.assertEqual(completed_status["last_completed"]["status"], "completed")
        self.assertIsNotNone(completed_status["last_completed"]["completed_at"])
        self.assertEqual(request_state("m07-p-post", self.root), "completed")
        self.assertEqual(request_status("m07-p-post", self.root)["message"], "Completed.")

    def test_posts_are_serialized(self):
        self.submit("m07-p-first", "POST", "/operator/tools/advisor.consult")
        self.assertTrue(self.executor.event("m07-p-first").wait(2))
        self.submit("m07-p-second", "POST", "/operator/tools/advisor.consult")
        time.sleep(0.15)
        self.assertFalse(self.executor.event("m07-p-second").is_set())
        status = json.loads((self.root / "relay-status.json").read_text())
        self.assertEqual([item["id"] for item in status["queue"]], ["m07-p-second"])
        self.assertEqual(status["queue"][0]["lane"], "post")
        self.assertEqual(status["queue"][0]["queue_position"], 1)
        self.assertIsNotNone(status["queue"][0]["queue_wait_seconds"])
        self.assertGreater(
            request_status("m07-p-second", self.root)["queue_wait_seconds"], 0
        )
        queued = json.loads((self.root / "inflight" / "m07-p-second.json").read_text())
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["queue_position"], 1)

        self.executor.unblock("m07-p-first")
        self.assertTrue(self.executor.event("m07-p-second").wait(2))
        self.executor.unblock("m07-p-second")
        self.wait_for(self.root / "outbox" / "m07-p-second.json")
        self.assertEqual(self.executor.max_active_posts, 1)
        journal = [
            json.loads(line)
            for line in (self.root / "journal.jsonl").read_text().splitlines()
        ]
        second = next(entry for entry in journal if entry["id"] == "m07-p-second")
        self.assertEqual(second["lane"], "post")
        self.assertEqual(second["request_id"], "m07-p-second")
        self.assertIsNotNone(second["received_at"])
        self.assertIsNotNone(second["started_at"])
        self.assertIsNotNone(second["completed_at"])
        self.assertGreater(second["queue_wait_seconds"], 0)
        self.assertIsNotNone(second["run_seconds"])
        self.assertIsNotNone(second["idempotency_key_sha256"])

    def test_whitelist_refusal_does_not_call_executor(self):
        self.submit("m07-denied", "POST", "/operator/tools/NOT_ALLOWED")
        result = self.wait_for(self.root / "outbox" / "m07-denied.json")
        self.assertEqual(result["rejected"], "path_or_method_not_allowed")
        self.assertEqual(self.executor.calls, [])
        self.assertFalse((self.root / "inflight" / "m07-denied.json").exists())

    def test_transport_error_is_ambiguous(self):
        calls = []

        def broken_executor(request):
            calls.append(request)
            raise TimeoutError("offline fake transport failure")

        self.relay.executor = broken_executor
        self.submit("m07-timeout", "POST", "/operator/tools/advisor.consult")
        result = self.wait_for(self.root / "outbox" / "m07-timeout.json")
        self.assertEqual(result["transport_error"], "TimeoutError")
        self.assertIs(result["ambiguous"], True)
        entry = json.loads((self.root / "journal.jsonl").read_text().splitlines()[-1])
        self.assertIs(entry["ambiguous"], True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(request_state("m07-timeout", self.root), "ambiguous")
        status = request_status("m07-timeout", self.root)
        self.assertIn("did not resend", status["message"])
        self.assertIsNotNone(status["completed_at"])
        output = StringIO()
        with patch("relayget.time.sleep", return_value=None), redirect_stdout(output):
            self.assertEqual(_wait("m07-timeout", 1, self.root)[0], None)
        rendered = output.getvalue()
        self.assertIn("Ambiguous transport outcome", rendered)
        self.assertIn("queue_wait_seconds=", rendered)
        self.assertIn("run_seconds=", rendered)
        self.assertIn("idempotency_key_sha256=", rendered)

    def test_status_text_explains_queued_post_position(self):
        self.submit("m07-p-first", "POST", "/operator/tools/advisor.consult")
        self.assertTrue(self.executor.event("m07-p-first").wait(2))
        self.submit("m07-p-second", "POST", "/operator/tools/advisor.consult")
        queued_file = self.root / "inflight" / "m07-p-second.json"
        self.wait_for(queued_file)
        status = request_status("m07-p-second", self.root)
        self.assertEqual(status["status"], "queued")
        self.assertIn("waiting for a POST slot", status["message"])
        self.assertIn("queue position 1", status["message"])

    def test_status_text_explains_received_request(self):
        received = {
            "id": "m07-received",
            "request_id": "m07-received",
            "method": "POST",
            "lane": "post",
            "status": "received",
            "received_at": "2026-11-02T12:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "queue_wait_seconds": 0.0,
            "run_seconds": None,
            "idempotency_key_sha256": "opaque-hash",
            "queue_position": None,
        }
        (self.root / "inflight" / "m07-received.json").write_text(json.dumps(received))
        status = request_status("m07-received", self.root)
        self.assertEqual(status["status"], "received")
        self.assertIn("Received by the relay", status["message"])


if __name__ == "__main__":
    unittest.main()

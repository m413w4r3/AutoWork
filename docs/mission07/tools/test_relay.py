import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from relay import Relay


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
        self.assertEqual(json.loads(inflight.read_text())["status"], "running")

        self.submit("m07-get", "GET", "/operator-status")
        self.assertTrue(self.executor.event("m07-get").wait(2))
        result = self.wait_for(self.root / "outbox" / "m07-get.json")
        self.assertEqual(result["http"], 200)

        status = json.loads((self.root / "relay-status.json").read_text())
        self.assertEqual(status["pid"], self.relay.pid)
        self.assertEqual(status["relay_version"], "2.0")
        self.assertEqual(status["investigation_id"], "a8845772-5025-4702-8850-2b6be776855b")
        self.assertTrue(status["heartbeat_ts"])
        self.assertEqual([item["id"] for item in status["in_flight"]], ["m07-p-post"])

        self.executor.unblock("m07-p-post")
        self.wait_for(self.root / "outbox" / "m07-p-post.json")
        self.assertFalse(inflight.exists())
        completed_status = json.loads((self.root / "relay-status.json").read_text())
        self.assertEqual(completed_status["last_completed_id"], "m07-p-post")

    def test_posts_are_serialized(self):
        self.submit("m07-p-first", "POST", "/operator/tools/advisor.consult")
        self.assertTrue(self.executor.event("m07-p-first").wait(2))
        self.submit("m07-p-second", "POST", "/operator/tools/advisor.consult")
        time.sleep(0.15)
        self.assertFalse(self.executor.event("m07-p-second").is_set())
        status = json.loads((self.root / "relay-status.json").read_text())
        self.assertEqual([item["id"] for item in status["queue"]], ["m07-p-second"])
        queued = json.loads((self.root / "inflight" / "m07-p-second.json").read_text())
        self.assertEqual(queued["status"], "received")

        self.executor.unblock("m07-p-first")
        self.assertTrue(self.executor.event("m07-p-second").wait(2))
        self.executor.unblock("m07-p-second")
        self.wait_for(self.root / "outbox" / "m07-p-second.json")
        self.assertEqual(self.executor.max_active_posts, 1)

    def test_whitelist_refusal_does_not_call_executor(self):
        self.submit("m07-denied", "POST", "/operator/tools/NOT_ALLOWED")
        result = self.wait_for(self.root / "outbox" / "m07-denied.json")
        self.assertEqual(result["rejected"], "path_or_method_not_allowed")
        self.assertEqual(self.executor.calls, [])
        self.assertFalse((self.root / "inflight" / "m07-denied.json").exists())

    def test_transport_error_is_ambiguous(self):
        def broken_executor(request):
            raise TimeoutError("offline fake transport failure")

        self.relay.executor = broken_executor
        self.submit("m07-timeout", "POST", "/operator/tools/advisor.consult")
        result = self.wait_for(self.root / "outbox" / "m07-timeout.json")
        self.assertEqual(result["transport_error"], "TimeoutError")
        self.assertIs(result["ambiguous"], True)
        entry = json.loads((self.root / "journal.jsonl").read_text().splitlines()[-1])
        self.assertIs(entry["ambiguous"], True)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

MAX_HTML_BYTES = int(os.environ.get("PLAYWRIGHT_ACQUIRER_MAX_HTML_BYTES", "10485760"))
MAX_TIMEOUT_MS = 45_000
RENDER_SLOTS = threading.BoundedSemaphore(1)


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoWorkPlaywrightAcquirer/1"

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self._write_json(200, {"status": "ready"})

    def do_POST(self) -> None:
        if self.path != "/render":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            self.send_error(400)
            return
        if length < 1 or length > MAX_HTML_BYTES * 2:
            self.send_error(413)
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400)
            return
        if not isinstance(payload, dict):
            self.send_error(400)
            return
        url = payload.get("url")
        html = payload.get("html")
        timeout_ms = payload.get("timeout_ms")
        if not isinstance(url, str) or not _public_http_url(url):
            self.send_error(400)
            return
        if not isinstance(html, str) or not html.strip():
            self.send_error(400)
            return
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            self.send_error(413)
            return
        if type(timeout_ms) is not int or not 500 <= timeout_ms <= MAX_TIMEOUT_MS:
            self.send_error(400)
            return
        if not RENDER_SLOTS.acquire(timeout=1):
            self._write_json(503, {"error": "renderer_busy"})
            return
        try:
            rendered_html = self._render(html, timeout_ms)
        except (PlaywrightError, RuntimeError):
            self._write_json(503, {"error": "renderer_unavailable"})
            return
        finally:
            RENDER_SLOTS.release()
        if len(rendered_html.encode("utf-8")) > MAX_HTML_BYTES:
            self.send_error(413)
            return
        self._write_json(200, {"rendered_html": rendered_html})

    @staticmethod
    def _render(html: str, timeout_ms: int) -> str:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                context = browser.new_context(
                    java_script_enabled=True,
                    service_workers="block",
                    accept_downloads=False,
                )
                try:
                    page = context.new_page()
                    # set_content executes inline page scripts. Every HTTP(S)
                    # request they create is aborted: Chromium never contacts
                    # the source, private networks, or any other site.
                    page.route("**/*", lambda route: route.abort())
                    page.set_content(html, wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_timeout(min(timeout_ms, 500))
                    return page.content()
                finally:
                    context.close()
            finally:
                browser.close()

    def _write_json(self, status: int, payload: dict[str, str]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        # Do not log document bodies or source URLs.
        del format, args


def _public_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def main() -> None:
    bind = os.environ.get("PLAYWRIGHT_ACQUIRER_BIND", "127.0.0.1")
    port = int(os.environ.get("PLAYWRIGHT_ACQUIRER_PORT", "8790"))
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()

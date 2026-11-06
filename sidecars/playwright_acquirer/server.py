from __future__ import annotations

import base64
import json
import os
import re
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_HTML_BYTES = int(os.environ.get("PLAYWRIGHT_ACQUIRER_MAX_HTML_BYTES", "10485760"))
MAX_REQUEST_BYTES = MAX_HTML_BYTES * 2 + 1024 * 1024
MAX_TIMEOUT_MS = 45_000
MAX_SETTLE_MS = 500
MAX_CAPTURE_JSON_COUNT = 10
MAX_CAPTURE_JSON_BODY_BYTES = 512 * 1024
MAX_CAPTURE_JSON_TOTAL_BYTES = 2 * 1024 * 1024
MAX_TRACKED_RESPONSES = 500
POLICY_VERSION = "browser-acquisition-v1"
RENDER_SLOTS = threading.BoundedSemaphore(1)
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "client_secret",
        "cookie",
        "key",
        "password",
        "refresh_token",
        "secret",
        "session",
        "sessionid",
        "sig",
        "signature",
        "token",
    }
)


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoWorkPlaywrightAcquirer/2"

    def do_GET(self) -> None:
        if self.path != "/health":
            self._write_json(404, {"error": "not_found"})
            return
        self._write_json(
            200,
            {
                "status": "ready",
                "browser": "chromium",
                "navigation": _proxy_configuration() is not None,
                "policy_version": POLICY_VERSION,
            },
        )

    def do_POST(self) -> None:
        if self.path != "/render":
            self._write_json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            self._write_json(400, {"error": "invalid_request"})
            return
        if length < 1 or length > MAX_REQUEST_BYTES:
            self._write_json(413, {"error": "request_size_limit"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(400, {"error": "invalid_request"})
            return
        if not isinstance(payload, dict):
            self._write_json(400, {"error": "invalid_request"})
            return
        url = payload.get("url")
        timeout_ms = payload.get("timeout_ms")
        html = payload.get("html")
        if not isinstance(url, str) or not _http_url_without_userinfo(url):
            self._write_json(400, {"error": "invalid_url"})
            return
        if type(timeout_ms) is not int or not 500 <= timeout_ms <= MAX_TIMEOUT_MS:
            self._write_json(400, {"error": "invalid_timeout"})
            return
        if html is not None:
            if not isinstance(html, str) or not html.strip():
                self._write_json(400, {"error": "invalid_html"})
                return
            if len(html.encode("utf-8")) > MAX_HTML_BYTES:
                self._write_json(413, {"error": "render_size_limit"})
                return
            render_kind = "html"
        elif payload.get("capture_json") is True:
            render_kind = "url"
            if _proxy_configuration() is None:
                self._write_json(503, {"error": "secure_egress_unconfigured"})
                return
        else:
            self._write_json(400, {"error": "invalid_request"})
            return
        if not RENDER_SLOTS.acquire(timeout=1):
            self._write_json(503, {"error": "renderer_busy"})
            return
        started = time.monotonic()
        try:
            if render_kind == "html":
                result = _render_offline(html, url, timeout_ms)
            else:
                result = _render_navigation(url, timeout_ms)
        except TimeoutError:
            self._write_json(504, {"error": "navigation_timeout"})
            return
        except Exception as exc:
            # Playwright and browser errors are intentionally kept out of logs/responses.
            del exc
            self._write_json(503, {"error": "renderer_unavailable"})
            return
        finally:
            RENDER_SLOTS.release()
        if not isinstance(result.get("rendered_html"), str) or not result["rendered_html"].strip():
            self._write_json(422, {"error": "empty_render"})
            return
        rendered_bytes = result["rendered_html"].encode("utf-8")
        if len(rendered_bytes) > MAX_HTML_BYTES:
            self._write_json(413, {"error": "render_size_limit"})
            return
        result["duration_ms"] = min(
            MAX_TIMEOUT_MS + MAX_SETTLE_MS,
            max(0, int((time.monotonic() - started) * 1000)),
        )
        self._write_json(200, result)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        # HTTP logs can contain URLs or headers, so the control API is silent.
        del format, args


class UnixThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX

    def __init__(self, socket_path: str, handler_class: type[BaseHTTPRequestHandler]) -> None:
        self.socket_path = socket_path
        path = Path(socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            info = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode):
                raise RuntimeError("renderer control path is not a socket")
            path.unlink()
        old_umask = os.umask(0o007)
        try:
            super().__init__(socket_path, handler_class)
        finally:
            os.umask(old_umask)
        os.chmod(socket_path, 0o770)

    def server_close(self) -> None:
        super().server_close()
        try:
            Path(self.socket_path).unlink()
        except FileNotFoundError:
            pass


def _render_offline(html: str, url: str, timeout_ms: int) -> dict[str, Any]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = browser.new_context(
                java_script_enabled=True,
                service_workers="block",
                accept_downloads=False,
                ignore_https_errors=False,
            )
            try:
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                try:
                    page.set_content(html, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeoutError as exc:
                    raise TimeoutError from exc
                page.wait_for_timeout(min(timeout_ms, MAX_SETTLE_MS))
                rendered = page.content()
                return {
                    "rendered_html": rendered,
                    "final_url": _credential_free_url(url),
                    "status": None,
                    "captured_json": [],
                    "challenge_detected": False,
                    "login_wall_detected": False,
                    "paywall_detected": False,
                    "engine": "chromium",
                }
            finally:
                context.close()
        finally:
            browser.close()


def _render_navigation(url: str, timeout_ms: int) -> dict[str, Any]:
    proxy = _proxy_configuration()
    if proxy is None:
        raise RuntimeError("secure egress is not configured")
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    proxy_settings: dict[str, str] = {
        "server": proxy[0],
        # Chromium bypasses loopback by default. Remove that implicit bypass so
        # every HTTP(S) request uses the enforcing proxy.
        "bypass": "<-loopback>",
    }
    if proxy[1]:
        proxy_settings["username"] = "autowork"
        proxy_settings["password"] = proxy[1]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            proxy=proxy_settings,
        )
        try:
            context = browser.new_context(
                java_script_enabled=True,
                service_workers="block",
                accept_downloads=False,
                ignore_https_errors=False,
            )
            try:
                context.route("**/*", _route_http_only)
                context.route_web_socket("**/*", lambda route: route.close())
                page = context.new_page()
                responses: list[Any] = []
                page.on(
                    "response",
                    lambda response: responses.append(response)
                    if len(responses) < MAX_TRACKED_RESPONSES
                    else None,
                )
                try:
                    response = page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                except PlaywrightTimeoutError as exc:
                    raise TimeoutError from exc
                page.wait_for_timeout(min(timeout_ms, MAX_SETTLE_MS))
                rendered = page.content()
                final_url = _credential_free_url(page.url)
                status = response.status if response is not None else None
                captured = _capture_json_responses(responses)
                challenge, login_wall, paywall = _detect_markers(rendered)
                return {
                    "rendered_html": rendered,
                    "final_url": final_url,
                    "status": status,
                    "captured_json": captured,
                    "challenge_detected": challenge or login_wall,
                    "login_wall_detected": login_wall,
                    "paywall_detected": paywall,
                    "engine": "chromium",
                }
            finally:
                context.close()
        finally:
            browser.close()


def _route_http_only(route: Any) -> None:
    try:
        if not _http_url_without_userinfo(route.request.url):
            route.abort()
            return
        route.continue_()
    except Exception:
        route.abort()


def _capture_json_responses(responses: list[Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    total = 0
    seen: set[str] = set()
    for response in responses:
        if len(entries) >= MAX_CAPTURE_JSON_COUNT or total >= MAX_CAPTURE_JSON_TOTAL_BYTES:
            break
        try:
            request = response.request
            if request.resource_type not in {"fetch", "xhr"}:
                continue
            url = _credential_free_url(response.url)
            if url in seen:
                continue
            content_type = str(response.headers.get("content-type", "")).split(";", 1)[0].strip()
            if not _is_json_content_type(content_type):
                continue
            declared_size = response.headers.get("content-length")
            if declared_size is None:
                continue
            declared_size_bytes = int(declared_size)
            if not 0 <= declared_size_bytes <= MAX_CAPTURE_JSON_BODY_BYTES:
                continue
            body = response.body()
            if len(body) > MAX_CAPTURE_JSON_BODY_BYTES or len(body) > declared_size_bytes:
                continue
            if total + len(body) > MAX_CAPTURE_JSON_TOTAL_BYTES:
                continue
        except Exception:
            continue
        entries.append(
            {
                "url": url,
                "content_type": content_type,
                "body_base64": base64.b64encode(body).decode("ascii"),
                "sha256": __import__("hashlib").sha256(body).hexdigest(),
                "size": len(body),
            }
        )
        seen.add(url)
        total += len(body)
    return entries


def _is_json_content_type(value: str) -> bool:
    normalized = value.casefold()
    return normalized == "application/json" or normalized.endswith("+json")


def _detect_markers(rendered_html: str) -> tuple[bool, bool, bool]:
    text = rendered_html.casefold()
    challenge_markers = (
        "cf-chl-",
        "challenge-platform",
        "captcha",
        "verify you are human",
        "unusual traffic",
        "access denied",
        "perimeterx",
        "datadome",
    )
    login_markers = (
        "sign in to continue",
        "log in to continue",
        "login required",
        "<input type=\"password\"",
        "<input type='password'",
    )
    paywall_markers = (
        "subscribe to continue",
        "subscription required",
        "paywall",
        "metered-paywall",
        "article is for subscribers",
    )
    return (
        any(marker in text for marker in challenge_markers),
        any(marker in text for marker in login_markers),
        any(marker in text for marker in paywall_markers),
    )


def _http_url_without_userinfo(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme.casefold() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port not in {0}
        )
    except ValueError:
        return False


def _credential_free_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None and port not in ({80} if parsed.scheme.casefold() == "http" else {443}):
        netloc += f":{port}"
    safe_query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if _safe_query_key(key)
    ]
    return urlunsplit(
        (parsed.scheme.casefold(), netloc, parsed.path, urlencode(safe_query, doseq=True), "")
    )


def _safe_query_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    if normalized in {
        re.sub(r"[^a-z0-9]", "", key) for key in _CREDENTIAL_QUERY_KEYS
    }:
        return False
    return not any(
        marker in normalized
        for marker in ("token", "secret", "password", "authorization", "signature", "credential")
    ) and not normalized.startswith("xamz") and not normalized.startswith("xgoog")


def _proxy_configuration() -> tuple[str, str | None] | None:
    value = os.environ.get("BROWSER_EGRESS_PROXY_URL", "").strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() != "http"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port or 80
        if not 1 <= port <= 65535:
            return None
    except ValueError:
        return None
    server = f"http://{parsed.hostname}:{port}"
    token = os.environ.get("BROWSER_EGRESS_PROXY_TOKEN") or None
    return server, token


def _serve(server: ThreadingHTTPServer) -> None:
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    socket_path = os.environ.get("PLAYWRIGHT_ACQUIRER_SOCKET", "").strip()
    http_bind = os.environ.get("PLAYWRIGHT_ACQUIRER_BIND")
    servers: list[ThreadingHTTPServer] = []
    if socket_path:
        servers.append(UnixThreadingHTTPServer(socket_path, Handler))
    if http_bind or not socket_path:
        bind = http_bind or "127.0.0.1"
        port = int(os.environ.get("PLAYWRIGHT_ACQUIRER_PORT", "8790"))
        servers.append(ThreadingHTTPServer((bind, port), Handler))
    if not servers:
        raise RuntimeError("no renderer control listener configured")
    for server in servers[1:]:
        threading.Thread(target=_serve, args=(server,), daemon=True).start()
    _serve(servers[0])


if __name__ == "__main__":
    main()

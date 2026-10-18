from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from cti_app.ghidra_service.contract import ContractError, parse_analysis_request
from cti_app.ghidra_service.engine import HeadlessGhidraEngine
from cti_app.ghidra_service.service import GhidraReadOnlyService


class _GhidraHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], service: GhidraReadOnlyService) -> None:
        super().__init__(address, _GhidraRequestHandler)
        self.service = service
        self.analysis_slot = threading.BoundedSemaphore(1)

    def get_request(self):  # type: ignore[no-untyped-def]
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


class _GhidraRequestHandler(BaseHTTPRequestHandler):
    server: _GhidraHttpServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/v1/probe" or urlsplit(self.path).query:
            self._send(HTTPStatus.NOT_FOUND, {"error": "unknown_path"})
            return
        payload = self.server.service.probe()
        status = HTTPStatus.OK if payload["status"] == "ok" else HTTPStatus.SERVICE_UNAVAILABLE
        self._send(status, payload)

    def do_POST(self) -> None:
        service = self.server.service
        try:
            request = parse_analysis_request(
                self.path,
                self.headers,
            )
            if self.headers.get("Transfer-Encoding"):
                raise ContractError("transfer_encoding_not_supported")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ContractError("content_length_required", status_code=411)
            try:
                content_length = int(raw_length)
            except ValueError:
                raise ContractError("content_length_invalid") from None
            if content_length < 1:
                raise ContractError("sample_empty")
            if content_length > service.maximum_sample_bytes:
                raise ContractError("sample_too_large", status_code=413)
            if not self.server.analysis_slot.acquire(blocking=False):
                raise ContractError("analysis_busy", status_code=503)
            try:
                sample_bytes = self.rfile.read(content_length)
                if len(sample_bytes) != content_length:
                    raise ContractError("content_length_mismatch")
                result = service.analyze(request, sample_bytes)
            finally:
                self.server.analysis_slot.release()
        except ContractError as exc:
            self._send(exc.status_code, {"error": str(exc)})
            return
        except TimeoutError:
            self._send(HTTPStatus.GATEWAY_TIMEOUT, {"error": "analysis_timeout"})
            return
        except (OSError, RuntimeError, ValueError):
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "analysis_failed"})
            return
        self._send(HTTPStatus.OK, result)

    def do_PUT(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_contract"})

    def do_PATCH(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_contract"})

    def do_DELETE(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_contract"})

    def log_message(self, format: str, *args: object) -> None:
        # Request paths and query strings are not operationally needed in logs.
        return

    def _send(self, status: int | HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def create_server(
    service: GhidraReadOnlyService,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> _GhidraHttpServer:
    return _GhidraHttpServer((host, port), service)


def build_service_from_environment() -> GhidraReadOnlyService:
    install_dir = Path(os.environ.get("GHIDRA_INSTALL_DIR", "/opt/ghidra"))
    expected_version = os.environ.get("GHIDRA_EXPECTED_VERSION", "12.1.4")
    expected_java_version = os.environ.get("GHIDRA_EXPECTED_JAVA_VERSION", "21.0.12.1")
    maximum_sample_bytes = int(os.environ.get("GHIDRA_MAX_SAMPLE_BYTES", str(200 * 1024 * 1024)))
    timeout_seconds = int(os.environ.get("GHIDRA_SERVICE_JOB_TIMEOUT_SECONDS", "120"))
    workspace_root = Path(os.environ.get("GHIDRA_WORKSPACE", "/work"))
    workspace_root.mkdir(parents=True, exist_ok=True)
    engine = HeadlessGhidraEngine(install_dir, timeout_seconds=timeout_seconds)
    return GhidraReadOnlyService(
        engine,
        expected_version=expected_version,
        expected_java_version=expected_java_version,
        maximum_sample_bytes=maximum_sample_bytes,
        timeout_seconds=timeout_seconds,
        workspace_root=workspace_root,
    )


def main() -> None:
    service = build_service_from_environment()
    host = os.environ.get("GHIDRA_SERVICE_BIND", "0.0.0.0")
    port = int(os.environ.get("GHIDRA_SERVICE_PORT", "8787"))
    server = create_server(service, host=host, port=port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

from __future__ import annotations

import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn

import pytest


@dataclass(frozen=True, slots=True)
class RecordedAdapterCall:
    operation: str
    arguments: Mapping[str, object]


class RecordingFakeAdapter:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[RecordedAdapterCall] = []

    def record(self, operation: str, arguments: Mapping[str, object]) -> object:
        self.calls.append(RecordedAdapterCall(operation, dict(arguments)))
        return self.response


class FakeModelGatewayAdapter(RecordingFakeAdapter):
    async def generate(self, arguments: Mapping[str, object]) -> object:
        return self.record("generate", arguments)


class FakeBridgeAdapter(RecordingFakeAdapter):
    async def consult(self, arguments: Mapping[str, object]) -> object:
        return self.record("consult", arguments)


def forbid_network_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny socket creation, network calls, and DNS in editorial tests."""

    def deny_egress(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("network egress is disabled in editorial tests")

    original_socket = socket.socket
    original_socket_init = original_socket.__init__

    def guarded_socket_init(
        instance: socket.socket,
        family: int = -1,
        type: int = -1,
        proto: int = -1,
        fileno: int | None = None,
    ) -> None:
        if family != socket.AF_UNIX:
            deny_egress()
        original_socket_init(instance, family, type, proto, fileno)

    monkeypatch.setattr(socket, "create_connection", deny_egress)
    monkeypatch.setattr(socket, "getaddrinfo", deny_egress)
    monkeypatch.setattr(socket, "gethostbyname", deny_egress)
    monkeypatch.setattr(original_socket, "connect", deny_egress)
    monkeypatch.setattr(original_socket, "connect_ex", deny_egress)
    monkeypatch.setattr(original_socket, "sendto", deny_egress)
    monkeypatch.setattr(original_socket, "sendmsg", deny_egress)
    monkeypatch.setattr(original_socket, "__init__", guarded_socket_init)

"""The workflow socket and DNS guard rejects external access before any network call."""

from __future__ import annotations

import socket

import httpx
import pytest

pytestmark = pytest.mark.workflow_fast


@pytest.mark.asyncio
async def test_network_guard_blocks_external_socket_dns_and_httpx() -> None:
    with pytest.raises(AssertionError, match="blocked non-loopback"):
        socket.create_connection(("example.com", 80), timeout=0.1)

    with pytest.raises(AssertionError, match="blocked non-loopback"):
        socket.gethostbyname("example.com")

    with pytest.raises(AssertionError, match="blocked non-loopback"):
        socket.gethostbyname_ex("example.com")

    with pytest.raises(AssertionError, match="blocked non-loopback"):
        socket.gethostbyaddr("93.184.216.34")

    async with httpx.AsyncClient(trust_env=False, timeout=0.2) as client:
        with pytest.raises(AssertionError, match="blocked non-loopback"):
            await client.get("http://example.com")

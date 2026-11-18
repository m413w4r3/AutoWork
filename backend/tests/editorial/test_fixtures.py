from __future__ import annotations

import hashlib
import socket
from socket import socket as original_socket

import pytest

from cti_app.application.production_editorial_enrichment import (
    MAX_BRIEF_ENRICHMENT_FIGURE_PROPOSALS,
)
from tests.editorial.fixtures.articles import duplicate_paragraph_article
from tests.editorial.fixtures.briefs import synthetic_brief
from tests.editorial.fixtures.enrichment import archived_figure_enrichment
from tests.editorial.fixtures.model_adapters import (
    FakeBridgeAdapter,
    FakeModelGatewayAdapter,
)


def test_duplicate_article_fixture_has_distinct_equal_paragraphs() -> None:
    article = duplicate_paragraph_article()

    assert len(article.paragraphs) == 2
    first, second = article.paragraphs
    assert first.paragraph_id != second.paragraph_id
    assert first.text == second.text
    assert first.position == 0
    assert second.position == 1


def test_archived_figure_fixture_keeps_sha_and_metadata() -> None:
    figure = archived_figure_enrichment().figures[0]

    assert figure.sha256 == hashlib.sha256(figure.content).hexdigest()
    assert figure.sha256 == "44a1d590acdf039e4e198f3ef812e1e6f37ccef2226f0471be79c65331446dc2"
    assert figure.mime_type == "image/svg+xml"
    assert (figure.width_px, figure.height_px) == (320, 180)
    assert figure.caption == "Synthetic archived figure"
    assert figure.archive_id == "synthetic-figure-001"


def test_brief_fixture_is_synthetic() -> None:
    brief = synthetic_brief()

    assert brief.brief_id == "synthetic-brief-001"
    assert brief.title == "Synthetic editorial brief"
    assert brief.summary == "A fixture used only for local editorial tests."
    assert len(brief.candidate_figures) == 4
    assert MAX_BRIEF_ENRICHMENT_FIGURE_PROPOSALS == 3


@pytest.mark.asyncio
async def test_egress_is_denied_and_fake_adapters_record_calls() -> None:
    model = FakeModelGatewayAdapter({"draft": "Synthetic draft."})
    bridge = FakeBridgeAdapter({"status": "Synthetic response."})

    model_result = await model.generate({"purpose": "fixture-test-1"})
    await model.generate({"purpose": "fixture-test-2"})
    bridge_result = await bridge.consult({"purpose": "fixture-test-1"})
    await bridge.consult({"purpose": "fixture-test-2"})

    assert model_result == {"draft": "Synthetic draft."}
    assert bridge_result == {"status": "Synthetic response."}
    assert [(call.operation, call.arguments) for call in model.calls] == [
        ("generate", {"purpose": "fixture-test-1"}),
        ("generate", {"purpose": "fixture-test-2"}),
    ]
    assert [(call.operation, call.arguments) for call in bridge.calls] == [
        ("consult", {"purpose": "fixture-test-1"}),
        ("consult", {"purpose": "fixture-test-2"}),
    ]
    with pytest.raises(AssertionError, match="network egress"):
        socket.create_connection(("192.0.2.1", 443))
    with pytest.raises(AssertionError, match="network egress"):
        with original_socket(socket.AF_UNIX, socket.SOCK_STREAM) as tcp_socket:
            tcp_socket.connect(("192.0.2.1", 443))
    with pytest.raises(AssertionError, match="network egress"):
        with original_socket(socket.AF_UNIX, socket.SOCK_STREAM) as tcp_socket:
            tcp_socket.connect_ex(("192.0.2.1", 443))
    with pytest.raises(AssertionError, match="network egress"):
        with original_socket(socket.AF_UNIX, socket.SOCK_DGRAM) as udp_socket:
            udp_socket.sendto(b"synthetic", ("192.0.2.1", 53))
    with pytest.raises(AssertionError, match="network egress"):
        with original_socket(socket.AF_UNIX, socket.SOCK_DGRAM) as udp_socket:
            udp_socket.sendmsg([b"synthetic"], [], 0, ("192.0.2.1", 53))
    with pytest.raises(AssertionError, match="network egress"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(AssertionError, match="network egress"):
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    with pytest.raises(AssertionError, match="network egress"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(AssertionError, match="network egress"):
        socket.gethostbyname("example.invalid")

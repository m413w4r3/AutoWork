"""Contract checks on the extension's own source (background.js/content.js).

These aren't unit tests of the Python bridge: they pin invariants of the
extension scripts that the Python side has no other way to enforce (there is
no Python import of `extension/`).
"""

from __future__ import annotations

from pathlib import Path


def test_extension_reserves_request_before_real_send_click() -> None:
    root = Path(__file__).parents[1] / "extension"
    background = (root / "background.js").read_text()
    content = (root / "content.js").read_text()

    assert 'requestStates.set(msg.id, "received")' in background
    assert "await Promise.all([requestStatesReady, conversationRegistryReady])" in background
    assert content.index("await claimPrompt(id)") < content.index("sendBtn.click()")
    assert "submittedRequestIds" in content
    assert "bridgeConversationRegistry" in background
    assert (
        "msg.conversation ? resolveConversationTab(msg.conversation) : findChatTab()" in background
    )
    assert 'chrome.tabs.create({ url: "https://chatgpt.com/", active: false })' in background
    assert "candidate.url === conversation.external_locator" in background
    # P0 : le heartbeat est un signal de liveness de l'extension. Il doit être
    # émis avant tout `continue` dépendant du DOM, sinon une phase de recherche
    # web qui remplace le tour assistant provoque un faux idle timeout.
    assert 'type: "heartbeat"' in content
    assert content.index('type: "heartbeat"') < content.index("if (!turn) continue;")
    assert 'type: "chunk"' not in content
    assert "text: serialized.text" in content
    assert '"final-output.js"' in background

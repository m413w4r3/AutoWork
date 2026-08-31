"""Contract checks on the extension's own source (background.js/content.js).

These aren't unit tests of the Python bridge: they pin invariants of the
extension scripts that the Python side has no other way to enforce (there is
no Python import of `extension/`).
"""

from __future__ import annotations

from pathlib import Path


def test_extension_reserves_request_before_real_send_trigger() -> None:
    root = Path(__file__).parents[1] / "extension"
    background = (root / "background.js").read_text()
    content = (root / "content.js").read_text()

    assert 'requestStates.set(msg.id, "received")' in background
    assert "requestStatesReady" in background and "conversationRegistryReady" in background
    assert content.index("await claimPrompt(id)") < content.index("const submissionMethod = triggerComposerSubmission(composer, sendBtn)")
    assert "submittedRequestIds" in content
    assert "bridgeConversationRegistry" in background
    assert "bridgeBrowserTargetRegistry" in background
    route_start = background.index("async function routeTab(")
    route_body = background[route_start : background.index("\n\n/** Envoie", route_start)]
    assert "if (msg.conversation) return resolveConversationTab(msg.conversation);" in route_body
    assert "if (msg.browser_target) return resolveBrowserTarget(msg.browser_target);" in route_body
    assert "if (msg.type === \"prompt\")" in route_body
    assert "return findChatTab();" in route_body
    # P0 : le heartbeat est un signal de liveness de l'extension. Il doit être
    # émis avant tout `continue` dépendant du DOM, sinon une phase de recherche
    # web qui remplace le tour assistant provoque un faux idle timeout.
    assert 'type: "heartbeat"' in content
    assert content.index('type: "heartbeat"') < content.index("if (!turn) continue;")
    assert 'type: "chunk"' not in content
    assert "text: serialized.text" in content
    send_start = background.index("async function sendToTab(")
    send_end = background.index("\nasync function cleanupReservationAfterDeliveryFailure", send_start)
    send_body = background[send_start:send_end]
    assert "chrome.scripting.executeScript" not in send_body
    assert send_body.count("chrome.tabs.sendMessage") == 1


def test_fresh_conversations_are_temporary_chat_url_based() -> None:
    root = Path(__file__).parents[1] / "extension"
    background = (root / "background.js").read_text()

    assert (
        'const TEMPORARY_CHAT_URL = "https://chatgpt.com/?temporary-chat=true";' in background
    )
    assert "?temporary-chat=true" in background
    assert "chrome.tabs.create({ url: TEMPORARY_CHAT_URL, active: false })" in background
    # The original regression: opening a bare chatgpt.com/ root and depending
    # on a later navigation for identity must never come back.
    assert 'chrome.tabs.create({ url: "https://chatgpt.com/", active: false })' not in background


def test_conversation_registry_lives_in_session_storage_only() -> None:
    root = Path(__file__).parents[1] / "extension"
    background = (root / "background.js").read_text()

    assert "chrome.storage.session\n  .get(\"bridgeConversationRegistry\")" in background
    assert 'chrome.storage.session.set({\n    bridgeConversationRegistry' in background
    # The live registry must never be persisted to chrome.storage.local.
    assert "bridgeConversationRegistry" not in _local_storage_calls(background)


def test_stateless_target_registry_lives_in_session_storage_only() -> None:
    root = Path(__file__).parents[1] / "extension"
    background = (root / "background.js").read_text()

    assert 'chrome.storage.session\n  .get("bridgeBrowserTargetRegistry")' in background
    assert "bridgeBrowserTargetRegistry: Object.fromEntries" in background
    assert "bridgeBrowserTargetRegistry" not in _local_storage_calls(background)


def _local_storage_calls(source: str) -> str:
    """Every `chrome.storage.local.set({...})` / `.get([...])` call site,
    concatenated — used to assert a key never appears among them."""
    calls: list[str] = []
    cursor = 0
    for marker in ("chrome.storage.local.set(", "chrome.storage.local.get("):
        while True:
            start = source.find(marker, cursor)
            if start == -1:
                break
            end = source.find(");", start)
            calls.append(source[start : end + 2] if end != -1 else source[start:])
            cursor = start + 1
        cursor = 0
    return "\n".join(calls)


def test_resolve_conversation_tab_never_routes_by_external_locator() -> None:
    root = Path(__file__).parents[1] / "extension"
    background = (root / "background.js").read_text()

    start = background.index("async function resolveConversationTab(")
    end = background.index("\nasync function routeTab(")
    body = background[start:end]
    continue_start = body.index('// mode === "continue"')
    continue_body = body[continue_start:]

    # external_locator may be *stored* as diagnostic metadata inside the
    # registry entry, but it must never be read/compared to make a routing
    # decision within resolveConversationTab.
    assert "conversation.external_locator" not in body
    assert "known.external_locator" not in body
    assert "candidate.url === conversation.external_locator" not in background
    # continue: exact tab retrieval, never a query/discovery, never a new tab.
    assert "chrome.tabs.get(known.tab_id)" in continue_body
    assert "chrome.tabs.query(" not in continue_body
    assert "chrome.tabs.create(" not in continue_body
    # expected_turn_id / head_turn_id participate in continuation verification.
    assert "known.head_turn_id !== conversation.expected_turn_id" in continue_body


def test_no_locator_based_identity_concepts_remain() -> None:
    root = Path(__file__).parents[1] / "extension"
    background = (root / "background.js").read_text()
    content = (root / "content.js").read_text()

    assert "validChatLocator" not in background
    assert "verifiedLocator" not in content
    assert "window.location.href !== conversation.external_locator" not in content
    assert "locator de conversation non attribué" not in content


def test_stateless_prompt_never_uses_new_chat_dom_click() -> None:
    root = Path(__file__).parents[1] / "extension"
    background = (root / "background.js").read_text()
    content = (root / "content.js").read_text()

    assert "SELECTORS.newChat" not in content
    assert "will_click_new_chat" not in content
    route_start = background.index("async function routeTab(")
    route_end = background.index("\n\n/** Envoie", route_start)
    route_body = background[route_start:route_end]
    assert route_body.index('if (msg.type === "prompt")') < route_body.index("return findChatTab();")

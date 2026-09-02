/**
 * Behavioral tests for background.js's conversation routing: fresh always
 * opens Temporary Chat, continue resolves the exact live tab by
 * conversation.id + expected_turn_id (never a locator/URL), a lost session
 * never reopens a replacement tab, and archive/cleanup only ever touch the
 * exact conversation they were asked about.
 *
 * background.js runs as a plain script (no DOM) inside a Node `vm` context
 * with a minimal in-memory mock of the chrome.* APIs it touches. Top-level
 * `const`/`function` declarations in a script run via `vm.runInContext`
 * remain visible to later `runInContext` calls against the same context —
 * the same pattern `content-dom.test.js` uses to reach into content.js.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const EXTENSION = path.join(__dirname, "..", "extension");
const BACKGROUND_SOURCE = fs.readFileSync(path.join(EXTENSION, "background.js"), "utf8");

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
  }
  send() {}
  close() {}
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;

/** In-memory chrome.* mock: tabs are a real Map so tests can assert on them
 * directly, storage.session/local are plain objects a test can inspect. */
function makeChromeMock() {
  let nextTabId = 1;
  const tabsById = new Map();
  const sessionStore = {};
  const localStore = {};
  const removedListeners = [];
  const updatedListeners = [];
  const messageListeners = [];

  const chrome = {
    storage: {
      session: {
        get: async (key) => ({ [key]: sessionStore[key] }),
        set: async (obj) => {
          Object.assign(sessionStore, obj);
        },
      },
      local: {
        get: async (keys) => {
          const list = Array.isArray(keys) ? keys : [keys];
          const result = {};
          for (const key of list) result[key] = localStore[key];
          return result;
        },
        set: async (obj) => {
          Object.assign(localStore, obj);
        },
      },
    },
    tabs: {
      create: async ({ url, active }) => {
        const tab = { id: nextTabId++, url, windowId: 900, status: "complete", active: !!active };
        tabsById.set(tab.id, tab);
        return { ...tab };
      },
      get: async (tabId) => {
        const tab = tabsById.get(tabId);
        if (!tab) throw new Error(`No tab with id: ${tabId}`);
        return { ...tab };
      },
      query: async () => [...tabsById.values()].map((tab) => ({ ...tab })),
      remove: async (tabId) => {
        const existed = tabsById.has(tabId);
        tabsById.delete(tabId);
        if (existed) {
          for (const fn of removedListeners) fn(tabId);
        }
      },
      sendMessage: async () => ({}),
      update: async (tabId, props) => {
        const tab = tabsById.get(tabId);
        if (!tab) throw new Error(`No tab with id: ${tabId}`);
        Object.assign(tab, props);
        return { ...tab };
      },
      onRemoved: { addListener: (fn) => removedListeners.push(fn) },
      onUpdated: { addListener: (fn) => updatedListeners.push(fn) },
      reload: async () => {},
    },
    scripting: { executeScript: async () => {} },
    runtime: {
      onMessage: { addListener: (fn) => messageListeners.push(fn) },
      onStartup: { addListener: () => {} },
      onInstalled: { addListener: () => {} },
    },
    alarms: { create: () => {}, onAlarm: { addListener: () => {} } },
  };

  return { chrome, tabsById, sessionStore, localStore, removedListeners, updatedListeners, messageListeners };
}

/** Loads background.js fresh into its own vm context. Passing the *same*
 * `chrome` mock (and therefore the same tabsById/sessionStore) across two
 * calls simulates a service-worker suspension/restart: browser-owned state
 * (tabs, chrome.storage.session) survives, in-memory module state doesn't. */
function loadBackground(chrome) {
  const sandbox = { chrome, console, URL, setTimeout, clearTimeout, WebSocket: FakeWebSocket };
  const context = vm.createContext(sandbox);
  vm.runInContext(BACKGROUND_SOURCE, context, { filename: "background.js" });
  return { run: (expression) => vm.runInContext(expression, context) };
}

async function main() {
  assert.doesNotMatch(
    BACKGROUND_SOURCE,
    /chrome\.runtime\.onMessage\.addListener\(async/,
    "runtime.onMessage listener must remain synchronous",
  );

  // 1. FRESH A creates exactly one inactive tab at the Temporary Chat URL.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const tab = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');

    assert.equal(tab.url, "https://chatgpt.com/?temporary-chat=true");
    assert.equal(tab.active, false);
    assert.equal(mock.tabsById.size, 1);
  }

  // 1b. UI preflight and the first prompt share the reserved fresh tab; a
  // second prompt after submission is refused without opening another tab.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const first = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    const preflight = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    assert.equal(preflight.id, first.id);
    await run('conversationRegistry.set("conv-A", { ...conversationRegistry.get("conv-A"), state: "live" })');
    await assert.rejects(
      run('resolveConversationTab({ mode: "fresh", id: "conv-A" })'),
      (err) => err.code === "conversation_unavailable",
    );
    assert.equal(mock.tabsById.size, 1);
  }

  // 2. FRESH A persists A -> tab_id in chrome.storage.session.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const tab = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');

    const stored = mock.sessionStore.bridgeConversationRegistry;
    assert.ok(stored, "bridgeConversationRegistry must be persisted to storage.session");
    assert.equal(stored["conv-A"].tab_id, tab.id);
    assert.equal(mock.localStore.bridgeConversationRegistry, undefined);
  }

  // 3. Service-worker reinitialization: storage.session survives, CONTINUE A
  //    with a matching expected_turn_id reuses the exact same tab.
  {
    const mock = makeChromeMock();
    const first = loadBackground(mock.chrome);
    const tabA = await first.run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    await first.run(
      `conversationRegistry.set("conv-A", { tab_id: ${tabA.id}, window_id: ${tabA.windowId}, head_turn_id: "turn-1", external_locator: null, last_verified_at: Date.now() })`,
    );
    await first.run("persistConversationRegistry()");

    // New vm context = new module state, same chrome mock = the same browser.
    const second = loadBackground(mock.chrome);
    const resumed = await second.run(
      'resolveConversationTab({ mode: "continue", id: "conv-A", expected_turn_id: "turn-1" })',
    );

    assert.equal(resumed.id, tabA.id);
    assert.equal(mock.tabsById.size, 1, "no replacement tab was created");
  }

  // 4. A and B share the exact same Temporary Chat URL; CONTINUE A must
  //    select A's tab, never B's.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const tabA = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    const tabB = await run('resolveConversationTab({ mode: "fresh", id: "conv-B" })');
    assert.equal(tabA.url, tabB.url);

    await run('conversationRegistry.get("conv-A").head_turn_id = "turn-A1"');
    await run('conversationRegistry.get("conv-B").head_turn_id = "turn-B1"');

    const continued = await run(
      'resolveConversationTab({ mode: "continue", id: "conv-A", expected_turn_id: "turn-A1" })',
    );
    assert.equal(continued.id, tabA.id);
    assert.notEqual(continued.id, tabB.id);
  }

  // 4b. An incomplete assistant turn becomes the live head and recovery uses
  // that exact external identity on the same tab.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const tab = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    const listener = mock.messageListeners[0];
    const response = () => {};
    await listener(
      { type: "conversation_bound", id: "req-1", conversation: { id: "conv-A", mode: "fresh", verified: true, ephemeral: true } },
      { tab: { id: tab.id, windowId: tab.windowId } },
      response,
    );
    await listener(
      { type: "incomplete", id: "req-1", metadata: { initial_turn_id: "turn-X" } },
      { tab: { id: tab.id, windowId: tab.windowId } },
      response,
    );
    const resumed = await run('resolveConversationTab({ mode: "continue", id: "conv-A", expected_turn_id: "turn-X" })');
    assert.equal(resumed.id, tab.id);
    assert.equal(await run('conversationRegistry.get("conv-A").head_turn_id'), "turn-X");
  }

  // 5. A's tab is closed: CONTINUE A returns conversation_unavailable, and
  //    zero replacement tabs are created.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const tabA = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    await run('conversationRegistry.get("conv-A").head_turn_id = "turn-A1"');
    await mock.chrome.tabs.remove(tabA.id);
    const before = mock.tabsById.size;

    await assert.rejects(
      run(
        'resolveConversationTab({ mode: "continue", id: "conv-A", expected_turn_id: "turn-A1" })',
      ),
      (err) => err.code === "conversation_unavailable",
    );
    assert.equal(mock.tabsById.size, before);
  }

  // 6. expected_turn_id does not equal registry.head_turn_id: no message is
  //    ever sent to the tab, and handlePrompt reports conversation_unavailable.
  {
    const mock = makeChromeMock();
    let sendMessageCalls = 0;
    mock.chrome.tabs.sendMessage = async () => {
      sendMessageCalls += 1;
      return {};
    };
    const { run } = loadBackground(mock.chrome);
    await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    await run('conversationRegistry.get("conv-A").head_turn_id = "turn-A1"');

    const queued = await run(`(async () => {
      await handlePrompt({
        id: "req-1",
        prompt: "hi",
        conversation: { mode: "continue", id: "conv-A", expected_turn_id: "wrong-turn" },
      });
      return enAttente.slice();
    })()`);

    const errorMsg = queued.find((m) => m.id === "req-1" && m.type === "error");
    assert.ok(errorMsg, "handlePrompt must report an error for the mismatched turn");
    assert.equal(errorMsg.code, "conversation_unavailable");
    assert.equal(sendMessageCalls, 0, "no prompt may ever reach the tab");
  }

  // 7. Archiving A closes only A's tab and removes only A's binding.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const tabA = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    const tabB = await run('resolveConversationTab({ mode: "fresh", id: "conv-B" })');

    await run('handleConversationArchive({ conversation_id: "conv-A", id: "archive-1" })');

    assert.equal(mock.tabsById.has(tabA.id), false);
    assert.equal(mock.tabsById.has(tabB.id), true);
    assert.equal(await run('conversationRegistry.has("conv-A")'), false);
    assert.equal(await run('conversationRegistry.has("conv-B")'), true);
  }

  // 8. A tab navigating off the ChatGPT origin invalidates its binding.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const tabA = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    assert.equal(await run('conversationRegistry.has("conv-A")'), true);

    for (const fn of mock.updatedListeners) fn(tabA.id, { url: "https://example.com/" });

    assert.equal(await run('conversationRegistry.has("conv-A")'), false);
  }

  // 9. A delivery error is ambiguous: Chrome does not tell us whether the
  // content script received the prompt before sendMessage failed. Keep the
  // exact submitted FRESH binding and refuse a second submission.
  {
    const mock = makeChromeMock();
    let sendMessageCalls = 0;
    mock.chrome.tabs.sendMessage = async () => {
      sendMessageCalls += 1;
      throw new Error("content script unavailable");
    };
    mock.chrome.scripting.executeScript = async () => {
      throw new Error("injection unavailable");
    };
    const { run } = loadBackground(mock.chrome);
    const fresh = {
      id: "req-1",
      prompt: "hello",
      conversation: { mode: "fresh", id: "conv-A" },
    };

    await run(`handlePrompt(${JSON.stringify(fresh)})`);
    assert.equal(await run('requestStates.get("req-1")'), "failed");
    assert.equal(await run('conversationRegistry.get("conv-A").state'), "submitted");
    assert.equal(await run('conversationRegistry.get("conv-A").bridge_run_id'), "req-1");
    assert.equal(mock.tabsById.size, 1);

    await run(
      `handlePrompt(${JSON.stringify({ ...fresh, id: "req-2" })})`,
    );
    assert.equal(mock.tabsById.size, 1, "retry FRESH must not create a replacement tab");
    assert.equal(sendMessageCalls, 1, "ambiguous delivery must never send a second prompt");
    const errorMsg = await run('enAttente.find((m) => m.id === "req-1" && m.type === "error")');
    assert.equal(errorMsg.code, "bridge_extension_disconnected");
    assert.equal(errorMsg.phase, "submission_confirmation");
    assert.equal(errorMsg.submission_state, "submission_attempted");
  }

  // 9b. A stateless delivery error keeps the exact request-scoped target
  // recoverable and a new request cannot reserve another Temporary Chat.
  {
    const mock = makeChromeMock();
    let sendMessageCalls = 0;
    mock.chrome.tabs.sendMessage = async () => {
      sendMessageCalls += 1;
      throw new Error("content script unavailable");
    };
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-ambiguous" };
    const prompt = { id: "req-stateless-1", prompt: "hello", browser_target: target };

    await run(`handlePrompt(${JSON.stringify(prompt)})`);
    assert.equal(await run('browserTargetRegistry.get("target-ambiguous").state'), "recoverable");
    assert.equal(await run('browserTargetRegistry.get("target-ambiguous").bridge_run_id'), "req-stateless-1");
    assert.equal(mock.tabsById.size, 1);

    await run(`handlePrompt(${JSON.stringify({ ...prompt, id: "req-stateless-2" })})`);
    assert.equal(mock.tabsById.size, 1, "recovery must not create a replacement tab");
    assert.equal(sendMessageCalls, 1, "recovery must not submit a second prompt");
  }

  // 10. A content-script pre-submission error closes the exact reserved tab.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const tab = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    await run(
      `conversationRegistry.set("conv-A", { tab_id: ${tab.id}, state: "submitted", bridge_run_id: "req-1" })`,
    );
    mock.messageListeners[0](
      {
        type: "error",
        id: "req-1",
        conversation: { id: "conv-A", mode: "fresh" },
        submission_state: "pre_submission",
      },
      { tab: { id: tab.id, windowId: tab.windowId } },
      () => {},
    );
    await Promise.resolve();
    assert.equal(mock.tabsById.has(tab.id), false);
    assert.equal(await run('conversationRegistry.has("conv-A")'), false);
  }

  // 11. A post-submission error preserves the live binding for recovery.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const tab = await run('resolveConversationTab({ mode: "fresh", id: "conv-A" })');
    await run(
      `conversationRegistry.set("conv-A", { tab_id: ${tab.id}, state: "submitted", bridge_run_id: "req-1" })`,
    );
    mock.messageListeners[0](
      {
        type: "error",
        id: "req-1",
        conversation: { id: "conv-A", mode: "fresh" },
        submission_state: "post_submission",
      },
      { tab: { id: tab.id, windowId: tab.windowId } },
      () => {},
    );
    await Promise.resolve();
    assert.equal(mock.tabsById.has(tab.id), true);
    assert.equal(await run('conversationRegistry.get("conv-A").state'), "submitted");
  }

  // 12. Stateless A, sans onglet initial : une seule target réserve un seul
  // Temporary Chat, et UI preflight -> contrôle -> prompt restent sur ce tab.
  {
    const mock = makeChromeMock();
    const sentToTabs = [];
    mock.chrome.tabs.sendMessage = async (tabId, msg) => {
      sentToTabs.push({ tabId, msg });
      if (msg.type === "ui_state" || msg.type === "ui_control") {
        return { ok: true, state: {}, applied: {} };
      }
      return {};
    };
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-A" };
    await run(`handleUiRequest(${JSON.stringify({ type: "ui_state", id: "ui-A", browser_target: target })})`);
    await run(`handleUiRequest(${JSON.stringify({ type: "ui_control", id: "control-A", controls: { web_search: true }, browser_target: target })})`);
    await run(`handlePrompt(${JSON.stringify({ type: "prompt", id: "run-A", prompt: "bonjour", new_chat: true, browser_target: target })})`);

    assert.equal(mock.tabsById.size, 1, "un run stateless ne doit créer qu'un Temporary Chat");
    const tab = [...mock.tabsById.values()][0];
    assert.equal(tab.url, "https://chatgpt.com/?temporary-chat=true");
    assert.equal(tab.active, false);
    assert.deepEqual(
      sentToTabs.map(({ tabId }) => tabId),
      [tab.id, tab.id, tab.id],
      "ui_state, ui_control et prompt doivent partager le tab exact",
    );
    assert.ok(sentToTabs.every(({ msg }) => msg.browser_target?.id === target.id));
    assert.equal(sentToTabs.filter(({ msg }) => msg.type === "prompt").length, 1);

    const listener = mock.messageListeners[0];
    listener(
      { type: "done", id: "run-A", text: "ok", conversation: null, metadata: { output_chars: 2 } },
      { tab: { id: tab.id, windowId: tab.windowId } },
      () => {},
    );
    await new Promise((resolve) => setImmediate(resolve));
    const doneEvent = await run('enAttente.find((message) => message.type === "done" && message.id === "run-A")');
    assert.equal(doneEvent.target_id, target.id);
    assert.equal(doneEvent.tab_id, tab.id, "le submit/fin doit rester sur le tab du prompt");
    assert.equal(mock.tabsById.size, 0, "done doit fermer le Temporary Chat exact");
    assert.equal(await run('browserTargetRegistry.has("target-A")'), false);
  }

  // 12a. A stateless post-submission error keeps the exact target, clears the
  // busy/inflight state, and persists an explicit recoverable binding.
  {
    const mock = makeChromeMock();
    mock.chrome.tabs.sendMessage = async () => ({});
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-error" };
    await run(`handlePrompt(${JSON.stringify({
      type: "prompt",
      id: "run-error",
      prompt: "bonjour",
      new_chat: true,
      browser_target: target,
    })})`);
    const tab = [...mock.tabsById.values()][0];
    mock.messageListeners[0](
      {
        type: "error",
        id: "run-error",
        code: "bridge_ui_timeout",
        submission_state: "post_submission",
      },
      { tab: { id: tab.id, windowId: tab.windowId } },
      () => {},
    );
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(mock.tabsById.has(tab.id), true);
    assert.equal(await run('inflight.has("run-error")'), false);
    assert.equal(await run(`busyTabs.has(${tab.id})`), false);
    assert.equal(await run('browserTargetRegistry.get("target-error").state'), "recoverable");
    assert.equal(await run('browserTargetRegistry.get("target-error").recoverable'), true);
    assert.equal(await run('browserTargetRegistry.get("target-error").bridge_run_id'), "run-error");
    assert.equal(mock.sessionStore.bridgeBrowserTargetRegistry["target-error"].tab_id, tab.id);
  }

  // 12b. An incomplete post-submit result has the same retention semantics,
  // including when submission_state exists only in its metadata.
  {
    const mock = makeChromeMock();
    mock.chrome.tabs.sendMessage = async () => ({});
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-incomplete" };
    await run(`handlePrompt(${JSON.stringify({
      type: "prompt",
      id: "run-incomplete",
      prompt: "bonjour",
      new_chat: true,
      browser_target: target,
    })})`);
    const tab = [...mock.tabsById.values()][0];
    mock.messageListeners[0](
      {
        type: "incomplete",
        id: "run-incomplete",
        metadata: { submission_state: "post_submission" },
      },
      { tab: { id: tab.id, windowId: tab.windowId } },
      () => {},
    );
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(mock.tabsById.has(tab.id), true);
    assert.equal(await run('browserTargetRegistry.get("target-incomplete").state'), "recoverable");
    assert.equal(await run('browserTargetRegistry.get("target-incomplete").bridge_run_id'), "run-incomplete");
  }

  // 12c. Recovery only resolves an already-preserved binding: a missing
  // binding never reserves a replacement Temporary Chat.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-missing" };
    await assert.rejects(
      run(`resolveRecoverableBrowserTarget(${JSON.stringify(target)}, "run-missing")`),
      (err) => err.code === "recovery_unavailable",
    );
    assert.equal(mock.tabsById.size, 0);
  }

  // 12d. Recovery routes to the exact preserved target/run and rejects an
  // unrelated run without touching either tab.
  {
    const mock = makeChromeMock();
    const calls = [];
    mock.chrome.tabs.sendMessage = async (tabId, msg) => {
      calls.push({ tabId, msg });
      return {
        text: "réponse finale",
        turn_id: "assistant-final",
        metadata: {},
      };
    };
    const { run } = loadBackground(mock.chrome);
    const targetA = { kind: "temporary_chat_run", id: "target-recovery-A" };
    const targetB = { kind: "temporary_chat_run", id: "target-recovery-B" };
    const tabA = await run(`resolveBrowserTarget(${JSON.stringify(targetA)})`);
    const tabB = await run(`resolveBrowserTarget(${JSON.stringify(targetB)})`);
    await run(`browserTargetRegistry.set("${targetA.id}", { target_id: "${targetA.id}", tab_id: ${tabA.id}, state: "recoverable", recoverable: true, bridge_run_id: "run-A" })`);
    await run(`browserTargetRegistry.set("${targetB.id}", { target_id: "${targetB.id}", tab_id: ${tabB.id}, state: "recoverable", recoverable: true, bridge_run_id: "run-B" })`);

    await run(`handleRecoveryCapture({ id: "recovery-A", bridge_run_id: "run-A", browser_target: ${JSON.stringify(targetA)} })`);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].tabId, tabA.id);
    assert.equal(calls[0].msg.bridge_run_id, "run-A");
    assert.equal(calls[0].msg.browser_target.id, targetA.id);

    await run(`handleRecoveryCapture({ id: "recovery-wrong", bridge_run_id: "run-B", browser_target: ${JSON.stringify(targetA)} })`);
    assert.equal(calls.length, 1, "un run différent ne doit pas atteindre l'onglet");
    assert.equal(mock.tabsById.size, 2);
  }

  // 12e. Explicit release is exact and idempotent: it cannot close another
  // preserved target, and repeating it is harmless.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const targetA = { kind: "temporary_chat_run", id: "target-release-A" };
    const targetB = { kind: "temporary_chat_run", id: "target-release-B" };
    const tabA = await run(`resolveBrowserTarget(${JSON.stringify(targetA)})`);
    const tabB = await run(`resolveBrowserTarget(${JSON.stringify(targetB)})`);
    await run(`browserTargetRegistry.set("${targetA.id}", { target_id: "${targetA.id}", tab_id: ${tabA.id}, state: "recoverable", recoverable: true, bridge_run_id: "run-A" })`);
    await run(`browserTargetRegistry.set("${targetB.id}", { target_id: "${targetB.id}", tab_id: ${tabB.id}, state: "recoverable", recoverable: true, bridge_run_id: "run-B" })`);

    await run(`handleBrowserTargetRelease({ id: "release-wrong", run_id: "run-wrong", browser_target: ${JSON.stringify(targetA)} })`);
    assert.equal(mock.tabsById.has(tabA.id), true, "un autre run ne doit pas libérer cette target");
    await run(`handleBrowserTargetRelease({ id: "release-A", run_id: "run-A", browser_target: ${JSON.stringify(targetA)} })`);
    await run(`handleBrowserTargetRelease({ id: "release-A-retry", run_id: "run-A", browser_target: ${JSON.stringify(targetA)} })`);
    assert.equal(mock.tabsById.has(tabA.id), false);
    assert.equal(mock.tabsById.has(tabB.id), true);
    assert.equal(await run(`browserTargetRegistry.has("${targetA.id}")`), false);
    assert.equal(await run(`browserTargetRegistry.has("${targetB.id}")`), true);
  }

  // 12f. Redémarrage du navigateur / rechargement de l'extension :
  // chrome.storage.session est entièrement perdu. Une target stateless connue
  // avant le redémarrage doit échouer fermé — jamais de nouvel onglet, jamais
  // de repli sur un onglet ChatGPT existant, jamais de resoumission.
  {
    const mock = makeChromeMock();
    const survivor = await mock.chrome.tabs.create({
      url: "https://chatgpt.com/?temporary-chat=true",
      active: true,
    });
    const sent = [];
    mock.chrome.tabs.sendMessage = async (tabId, msg) => {
      sent.push({ tabId, msg });
      return {};
    };
    // storage.session est vide : rien n'a survécu au redémarrage.
    assert.deepEqual(mock.sessionStore, {});
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-lost-after-restart" };

    await assert.rejects(
      run(`resolveRecoverableBrowserTarget(${JSON.stringify(target)}, "run-lost")`),
      (err) => typeof err.code === "string" && err.code.length > 0,
      "une target inconnue doit échouer avec un code typé",
    );

    await run(`handleRecoveryCapture({ id: "recovery-lost", bridge_run_id: "run-lost", browser_target: ${JSON.stringify(target)} })`);
    await run(`handleBrowserTargetRelease({ id: "release-lost", run_id: "run-lost", browser_target: ${JSON.stringify(target)} })`);

    assert.equal(mock.tabsById.size, 1, "aucun onglet ne doit être créé ni fermé");
    assert.equal(mock.tabsById.has(survivor.id), true);
    assert.equal(sent.length, 0, "aucun message ne doit atteindre un onglet ChatGPT arbitraire");
    assert.equal(await run(`browserTargetRegistry.has("${target.id}")`), false);
  }

  // 13. Un onglet normal préexistant reste intact : le run stateless ouvre sa
  // propre target et ne lui envoie ni contrôle ni prompt.
  {
    const mock = makeChromeMock();
    const normal = await mock.chrome.tabs.create({ url: "https://chatgpt.com/", active: true });
    const sentToTabs = [];
    mock.chrome.tabs.sendMessage = async (tabId, msg) => {
      sentToTabs.push({ tabId, msg });
      return msg.type === "ui_state" || msg.type === "ui_control" ? { ok: true, state: {}, applied: {} } : {};
    };
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-B" };
    await run(`handleUiRequest(${JSON.stringify({ type: "ui_control", id: "control-B", controls: { web_search: true }, browser_target: target })})`);
    await run(`handlePrompt(${JSON.stringify({ type: "prompt", id: "run-B", prompt: "bonjour", new_chat: true, browser_target: target })})`);

    assert.equal(mock.tabsById.size, 2);
    const temporary = [...mock.tabsById.values()].find((tab) => tab.id !== normal.id);
    assert.equal(temporary.url, "https://chatgpt.com/?temporary-chat=true");
    assert.ok(sentToTabs.every(({ tabId }) => tabId === temporary.id));
    assert.ok(mock.tabsById.has(normal.id), "l'onglet normal ne doit pas être fermé");
    assert.equal(normal.url, "https://chatgpt.com/");
  }

  // 14. Deux targets stateless sont deux bindings indépendants, même si leurs
  // onglets partagent exactement la même URL.
  {
    const mock = makeChromeMock();
    const { run } = loadBackground(mock.chrome);
    const targetA = { kind: "temporary_chat_run", id: "target-C-A" };
    const targetB = { kind: "temporary_chat_run", id: "target-C-B" };
    const tabA = await run(`resolveBrowserTarget(${JSON.stringify(targetA)})`);
    const tabB = await run(`resolveBrowserTarget(${JSON.stringify(targetB)})`);
    assert.notEqual(tabA.id, tabB.id);
    assert.equal(await run('browserTargetRegistry.get("target-C-A").tab_id'), tabA.id);
    assert.equal(await run('browserTargetRegistry.get("target-C-B").tab_id'), tabB.id);
  }

  // 15. PRE_SUBMISSION supprime la réservation exacte ; un nouvel id crée une
  // nouvelle target et ne réanime jamais l'ancien onglet.
  {
    const mock = makeChromeMock();
    mock.chrome.tabs.sendMessage = async () => ({});
    const { run } = loadBackground(mock.chrome);
    const targetA = { kind: "temporary_chat_run", id: "target-D-A" };
    await run(`handlePrompt(${JSON.stringify({ type: "prompt", id: "run-D-A", prompt: "bonjour", new_chat: true, browser_target: targetA })})`);
    const tabA = [...mock.tabsById.values()][0];
    mock.messageListeners[0](
      {
        type: "error",
        id: "run-D-A",
        code: "bridge_browser_target_required",
        conversation: null,
        submission_state: "pre_submission",
      },
      { tab: { id: tabA.id, windowId: tabA.windowId } },
      () => {},
    );
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(mock.tabsById.has(tabA.id), false);
    assert.equal(await run('browserTargetRegistry.has("target-D-A")'), false);

    const targetB = { kind: "temporary_chat_run", id: "target-D-B" };
    const tabB = await run(`resolveBrowserTarget(${JSON.stringify(targetB)})`);
    assert.notEqual(tabA.id, tabB.id);
    assert.equal(mock.tabsById.size, 1);
  }

  // --- Autonomie de l'onglet d'arrière-plan --------------------------------- //

  // 16. L'onglet exact d'un run lié est protégé du déchargement de Chrome,
  // sans jamais être activé ni focalisé.
  {
    const mock = makeChromeMock();
    mock.chrome.tabs.sendMessage = async () => ({});
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-E" };
    await run(
      `handlePrompt(${JSON.stringify({ type: "prompt", id: "run-E", prompt: "bonjour", new_chat: true, browser_target: target })})`,
    );
    const tab = [...mock.tabsById.values()][0];
    assert.equal(tab.autoDiscardable, false, "l'onglet lié ne doit pas être déchargeable");
    assert.equal(tab.active, false, "l'onglet ne doit jamais être activé");
  }

  // 17. Ticks d'observation : le ping du serveur réveille exactement l'onglet
  // du run en vol, et lui seul. Aucun autre onglet n'est touché, et le tick ne
  // porte aucun contenu.
  {
    const mock = makeChromeMock();
    const ticks = [];
    mock.chrome.tabs.sendMessage = async (tabId, message) => {
      if (message?.type === "observe_tick") ticks.push({ tabId, message });
      return {};
    };
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-F" };
    await run(
      `handlePrompt(${JSON.stringify({ type: "prompt", id: "run-F", prompt: "bonjour", new_chat: true, browser_target: target })})`,
    );
    const bound = [...mock.tabsById.values()][0];
    // Un onglet ChatGPT étranger au run : il ne doit jamais recevoir de tick.
    const other = await mock.chrome.tabs.create({ url: "https://chatgpt.com/", active: false });
    await run("pumpObservationTicks()");
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(
      ticks.map((tick) => tick.tabId),
      [bound.id],
      "seul l'onglet exact du run en vol reçoit un tick",
    );
    assert.notEqual(ticks[0].tabId, other.id);
    assert.deepEqual(
      { ...ticks[0].message },
      { type: "observe_tick", id: "run-F" },
      "le tick ne porte que le type et l'id du run",
    );
  }

  // 18. Onglet déchargé pendant un run : échec typé et fermé. Aucune
  // resoumission, aucun onglet de remplacement, target exacte conservée pour
  // une recovery explicite.
  {
    const mock = makeChromeMock();
    mock.chrome.tabs.sendMessage = async () => ({});
    const { run } = loadBackground(mock.chrome);
    const target = { kind: "temporary_chat_run", id: "target-G" };
    await run(
      `handlePrompt(${JSON.stringify({ type: "prompt", id: "run-G", prompt: "bonjour", new_chat: true, browser_target: target })})`,
    );
    const tab = [...mock.tabsById.values()][0];
    const tabsBefore = mock.tabsById.size;

    tab.discarded = true;
    for (const fn of mock.updatedListeners) fn(tab.id, { discarded: true });
    await new Promise((resolve) => setImmediate(resolve));

    const failure = await run('enAttente.find((m) => m.id === "run-G" && m.type === "error")');
    assert.ok(failure, "un déchargement doit produire un échec typé");
    assert.equal(failure.code, "bridge_extension_disconnected");
    assert.equal(failure.submission_state, "post_submission");
    assert.equal(failure.retryable, false, "un run déchargé n'est jamais rejoué");
    assert.equal(failure.tab_id, tab.id);
    assert.equal(failure.diagnostics.tab_state.discarded, true);
    assert.equal(await run('requestStates.get("run-G")'), "failed");
    assert.equal(await run('inflight.has("run-G")'), false);
    assert.equal(mock.tabsById.size, tabsBefore, "aucun onglet de remplacement");
    // Fin ambiguë : la target exacte est conservée pour une recovery explicite,
    // jamais réutilisée pour une nouvelle réservation sous la même identité.
    assert.equal(await run('browserTargetRegistry.get("target-G").state'), "recoverable");
    await assert.rejects(
      run(`resolveBrowserTarget(${JSON.stringify(target)})`),
      (err) => err.code === "recovery_unavailable",
    );
    assert.equal(mock.tabsById.size, tabsBefore);
  }

  console.log("background conversation routing contract: ok");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

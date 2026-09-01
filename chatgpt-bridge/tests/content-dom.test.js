/**
 * Tests du périmètre DOM de content.js : quels nœuds ont le droit de dire
 * qu'une génération est encore active. Un signal lu trop large (Stop d'un
 * widget quelconque, indicateur de streaming laissé par un ancien tour)
 * empêchait la finalisation du tour surveillé.
 *
 * jsdom est résolu depuis frontend/node_modules : la CI installe déjà ces
 * dépendances avant de lancer les tests du bridge (cf. .github/workflows/ci.yml).
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createRequire } = require("node:module");

const EXTENSION = path.join(__dirname, "..", "extension");
const FRONTEND = path.join(__dirname, "..", "..", "frontend", "package.json");

let JSDOM;
try {
  ({ JSDOM } = createRequire(FRONTEND)("jsdom"));
} catch (err) {
  console.error(
    "jsdom introuvable : lancer `pnpm install` dans frontend/ avant ce test.",
  );
  throw err;
}

/**
 * Charge les scripts de l'extension dans un DOM simulé et rend leurs fonctions
 * de haut niveau appelables depuis le test.
 */
function loadExtension(body, url = "https://chatgpt.com/") {
  const dom = new JSDOM(`<!doctype html><html><body>${body}</body></html>`, {
    runScripts: "outside-only",
    url,
  });
  const { window } = dom;

  // jsdom ne calcule aucune mise en page : sans ce repli, `visible()` renverrait
  // false pour tous les éléments et aucun signal ne serait jamais lu.
  window.Element.prototype.getClientRects = function getClientRects() {
    return this.hasAttribute("data-test-offscreen") ? [] : [{}];
  };
  // `CSS.escape` existe dans le navigateur mais pas dans jsdom ; les locators de
  // tour sont de simples identifiants, un échappement minimal suffit ici.
  window.CSS = window.CSS || {
    escape: (value) => String(value).replace(/["\\]/g, "\\$&"),
  };
  window.chrome = {
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: {
      sendMessage: async () => {},
      onMessage: { addListener: () => {} },
    },
  };

  const context = dom.getInternalVMContext();
  for (const file of [
    "serializer.js",
    "completion.js",
    "final-output.js",
    "content.js",
  ]) {
    vm.runInContext(
      fs.readFileSync(path.join(EXTENSION, file), "utf8"),
      context,
      {
        filename: file,
      },
    );
  }
  return {
    window,
    run: (expression) => vm.runInContext(expression, context),
    // Les objets nés dans le contexte vm ont un autre prototype : on les
    // recopie pour que deepEqual compare des valeurs, pas des realms.
    state: (expression) => ({ ...vm.runInContext(expression, context) }),
  };
}

const WATCHED_TURN = `
  <article data-testid="conversation-turn-3">
    <div data-message-author-role="assistant" data-message-id="m3">
      <div class="markdown"><p>réponse finale</p></div>
    </div>
    __ACTIONS__
  </article>`;

const composer = `
  <form>
    <div id="prompt-textarea" contenteditable="true"></div>
    <button data-testid="send-button">Envoyer</button>
    __COMPOSER_STOP__
  </form>`;

const copyButton = `<button data-testid="copy-turn-action-button">Copy response</button>`;
const stopButton = `<button data-testid="stop-button" aria-label="Stop streaming"></button>`;

function page({
  staleStreaming = false,
  watchedStreaming = false,
  actions = false,
  strayStop = false,
  composerStop = false,
  withComposer = true,
} = {}) {
  const stale = `
    <article data-testid="conversation-turn-1">
      <div data-message-author-role="assistant" data-message-id="m1">
        <div class="markdown"><p>ancienne réponse</p></div>
        ${staleStreaming ? `<div data-is-streaming="true"></div>` : ""}
      </div>
      ${copyButton}
    </article>`;
  const watched = WATCHED_TURN.replace(
    "__ACTIONS__",
    `${watchedStreaming ? `<div class="result-streaming"></div>` : ""}${
      actions ? copyButton : ""
    }`,
  );
  const aside = strayStop
    ? `<aside><button aria-label="Stop la lecture">Stop</button></aside>`
    : "";
  const form = withComposer
    ? composer.replace("__COMPOSER_STOP__", composerStop ? stopButton : "")
    : "";
  return `<main>${stale}${watched}</main>${aside}${form}`;
}

const WATCHED = `document.querySelector("[data-testid='conversation-turn-3'] [data-message-author-role='assistant']")`;

function serializeMarkup(body) {
  const { run } = loadExtension(body);
  return run(
    `ChatGPTBridgeSerializer.serializeResponse(document.querySelector("#serialize-root"))`,
  );
}

function serializeCode(raw, className = "") {
  const classAttribute = className ? ` class="${className}"` : "";
  const { run } = loadExtension(
    `<div id="serialize-root"><pre><code${classAttribute}></code></pre></div>`,
  );
  run(
    `document.querySelector("#serialize-root code").textContent = ${JSON.stringify(raw)}`,
  );
  return run(
    `ChatGPTBridgeSerializer.serializeResponse(document.querySelector("#serialize-root"))`,
  );
}

function recoverFencedCode(markdown) {
  const openingLineEnd = markdown.indexOf("\n");
  const closingFence = "\n```";
  assert.ok(openingLineEnd >= 0, "le code doit avoir une ligne d'ouverture");
  assert.ok(markdown.endsWith(closingFence), "le code doit avoir une fence fermante");
  return markdown.slice(openingLineEnd + 1, -closingFence.length);
}

// --- Fidélité du serializer Markdown ------------------------------------- //
{
  const serialized = serializeMarkup(`
    <div id="serialize-root">
      <h1>Premier</h1>
      <h2>Deuxième</h2>
      <h3>Troisième</h3>
      <h6>Sixième</h6>
    </div>`);
  assert.equal(
    serialized.text,
    "# Premier\n\n## Deuxième\n\n### Troisième\n\n###### Sixième",
  );
  assert.equal(serialized.serializer_version, "chatgpt-dom-v3");
}

{
  const serialized = serializeMarkup(`
    <div id="serialize-root">
      <h2>Liste</h2>
      <ul><li>un</li><li>deux</li></ul>
    </div>`);
  assert.equal(serialized.text, "## Liste\n\n- un\n- deux");
}

{
  const raw = "  const answer = 42;  \n    return answer;";
  const { run } = loadExtension(
    `<div id="serialize-root"><h2>Exemple</h2><pre><code class="language-js"></code></pre></div>`,
  );
  run(`document.querySelector("#serialize-root code").textContent = ${JSON.stringify(raw)}`);
  const serialized = run(
    `ChatGPTBridgeSerializer.serializeResponse(document.querySelector("#serialize-root"))`,
  );
  assert.equal(
    serialized.text,
    `## Exemple\n\n\`\`\`js\n${raw}\n\`\`\``,
  );
  assert.equal(recoverFencedCode(serialized.text.split("\n\n")[1]), raw);
}

{
  const raw = `  trailing spaces  \n\n\n\n  backslashes C:\\tmp\\file  \nhxxps\\://example.test/path  \n`;
  const serialized = serializeCode(raw);
  assert.equal(recoverFencedCode(serialized.text), raw);
  assert.equal(serialized.text, `\`\`\`\n${raw}\n\`\`\``);
}

{
  const withoutFinalNewline = serializeCode("ligne");
  const withFinalNewline = serializeCode("ligne\n");
  assert.equal(withoutFinalNewline.text, "\`\`\`\nligne\n\`\`\`");
  assert.equal(withFinalNewline.text, "\`\`\`\nligne\n\n\`\`\`");
  assert.equal(recoverFencedCode(withoutFinalNewline.text), "ligne");
  assert.equal(recoverFencedCode(withFinalNewline.text), "ligne\n");
}

{
  const serialized = serializeMarkup(
    `<div id="serialize-root">prose   \n\n\nprose hxxps\\://example.test</div>`,
  );
  assert.equal(serialized.text, "prose\n\nprose hxxps\\://example.test");
}

{
  const serialized = serializeMarkup(`
    <div id="serialize-root">
      <p>Voir <a href="https://example.test/page?utm_source=chatgpt&amp;b=2">la page</a>
        <sup data-testid="citation"><a href="https://example.test/source">[1]</a></sup>
      </p>
    </div>`);
  assert.equal(
    serialized.text,
    "Voir [la page](https://example.test/page?utm_source=chatgpt&b=2)",
  );
  assert.deepEqual(JSON.parse(JSON.stringify(serialized.visible_citations)), [
    {
      label: "[1]",
      url: "https://example.test/source",
      canonical_url: "https://example.test/source",
      position: null,
    },
  ]);
}

// --- Périmètre du streaming : le tour surveillé, pas la page entière -------- //
{
  const { state } = loadExtension(
    page({ staleStreaming: true, strayStop: true, actions: true }),
  );
  assert.deepEqual(
    state(`completionState(${WATCHED})`),
    { finished: true, signal: "assistant_actions", confidence: "high" },
    "un indicateur de streaming d'un ancien tour ne bloque pas la finalisation",
  );
}

{
  const { state } = loadExtension(page({ watchedStreaming: true }));
  assert.deepEqual(
    state(`completionState(${WATCHED})`),
    { finished: false, signal: "streaming", confidence: "high" },
    "le streaming du tour surveillé, lui, interdit la finalisation",
  );
}

// --- Périmètre du Stop : le composer, pas la page entière ------------------ //
{
  const { state } = loadExtension(page({ strayStop: true, actions: true }));
  assert.deepEqual(
    state(`completionState(${WATCHED})`),
    { finished: true, signal: "assistant_actions", confidence: "high" },
    "un bouton « Stop » hors composer ne maintient pas le tour en running",
  );
}

{
  const { state } = loadExtension(page({ strayStop: true }));
  assert.deepEqual(
    state(`completionState(${WATCHED})`),
    { finished: null, signal: "unknown", confidence: "low" },
    "un « Stop » hors composer n'est pas lu du tout",
  );
}

{
  const { state } = loadExtension(page({ composerStop: true }));
  assert.deepEqual(
    state(`completionState(${WATCHED})`),
    { finished: false, signal: "stop_button", confidence: "high" },
    "le Stop du composer reste un signal d'activité",
  );
}

{
  // Composer temporairement absent : mieux vaut aucun signal qu'un scope
  // retombant sur document.body, qui rendrait le cloisonnement inutile.
  const { state } = loadExtension(
    page({ withComposer: false, strayStop: true }),
  );
  assert.deepEqual(
    state(`completionState(${WATCHED})`),
    { finished: null, signal: "unknown", confidence: "low" },
    "sans composer, aucun Stop n'est retenu",
  );
}

// --- Garde-fou : un signal actif figé ne boucle pas indéfiniment ------------ //
(async () => {
  const { window, run } = loadExtension(page({ watchedStreaming: true }));

  // Horloge virtuelle : chaque sleep() avance le temps du délai demandé, ce qui
  // rend les deux minutes de stabilité atteignables sans attente réelle.
  let clock = 1_000_000;
  let sleeps = 0;
  window.Date.now = () => clock;
  window.setTimeout = (fn, ms) => {
    clock += ms || 0;
    sleeps += 1;
    queueMicrotask(fn);
    return 0;
  };

  window.testJob = { id: "stall", aborted: false };
  const result = await run(`streamAnswer(testJob, "conversation-turn-3", 1)`);

  assert.equal(result.incomplete, true);
  assert.equal(result.incomplete_reason, "active_signal_stalled");
  assert.equal(result.completion_signal, "streaming");
  assert.equal(result.text, "réponse finale");
  // Le seuil est relu dans le script : le test protège le garde-fou, pas une
  // valeur particulière, qui peut être desserrée quand ChatGPT ralentit.
  const seuil = run("ACTIVE_SIGNAL_STALL_MS");
  assert.ok(seuil >= 120_000, `garde-fou trop court : ${seuil} ms`);
  assert.ok(
    result.stable_for_ms >= seuil,
    `stabilité attendue >= ${seuil} ms, vue ${result.stable_for_ms}`,
  );
  assert.ok(sleeps > 0, "la boucle doit réellement avoir tourné");

  console.log("content dom scope contract: ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});

// --------------------------------------------------------------------------- //
// Temporary Chat : confirmation positive avant Send, jamais best-effort ; et
// identité du tour précédent pour CONTINUE, jamais par index/comptage.
// --------------------------------------------------------------------------- //
/** Contourne les délais réels de waitFor()/sleep() : chaque setTimeout avance
 * une horloge virtuelle et relance immédiatement le callback. */
function useVirtualClock(window) {
  let clock = 0;
  window.Date.now = () => clock;
  window.setTimeout = (fn, ms) => {
    clock += ms || 0;
    queueMicrotask(fn);
    return 0;
  };
}

(async () => {
  const temporaryComposer = `<div id="prompt-textarea" contenteditable="true"></div>`;

  // 1. URL Temporary + composer, sans toggle : le markup de l'UI n'est pas
  // une preuve de confidentialité et n'est jamais requis.
  {
    const { run } = loadExtension(temporaryComposer, "https://chatgpt.com/?temporary-chat=true");
    await run("ensureTemporaryChat()");
  }

  // 2. Toggle au markup inconnu : accepté, sans clic.
  {
    const { window, run } = loadExtension(
      `${temporaryComposer}<button aria-label="Temporary chat"><svg><use href="#unknown"></use></svg></button>`,
      "https://chatgpt.com/?temporary-chat=true",
    );
    useVirtualClock(window);
    let clicked = false;
    window.document.querySelector("button[aria-label='Temporary chat']").addEventListener("click", () => { clicked = true; });
    await run("ensureTemporaryChat()");
    assert.equal(clicked, false, "un toggle au markup inconnu ne doit jamais être cliqué");
  }

  // 3. aria-pressed=false ne doit pas provoquer de mutation.
  {
    const { window, run } = loadExtension(
      `${temporaryComposer}<button aria-label="Temporary chat" aria-pressed="false"></button>`,
      "https://chatgpt.com/?temporary-chat=true",
    );
    useVirtualClock(window);
    let clicked = false;
    window.document.querySelector("button[aria-label='Temporary chat']").addEventListener("click", () => { clicked = true; });
    await run("ensureTemporaryChat()");
    assert.equal(clicked, false, "aria-pressed=false ne doit jamais être cliqué");
  }

  // 4-7. URL non temporaire ou navigation persistante : échec immédiat.
  {
    const { run } = loadExtension(temporaryComposer, "https://chatgpt.com/");
    await assert.rejects(run("ensureTemporaryChat()"), (err) => err.code === "bridge_ui_timeout");
  }
  {
    const { run } = loadExtension(temporaryComposer, "https://chatgpt.com/?temporary-chat=false");
    await assert.rejects(run("ensureTemporaryChat()"), (err) => err.code === "bridge_ui_timeout");
  }
  {
    const { run } = loadExtension(temporaryComposer, "https://chatgpt.com/c/abc123");
    await assert.rejects(run("ensureTemporaryChat()"), (err) => err.code === "conversation_unavailable");
  }
  {
    const { run } = loadExtension(temporaryComposer, "https://example.com/?temporary-chat=true");
    await assert.rejects(run("ensureTemporaryChat()"), (err) => err.code === "bridge_ui_timeout");
  }

  // 8. URL correcte mais composer jamais rendu : timeout de chargement, avec
  // relecture du DOM à chaque poll.
  {
    const { window, run } = loadExtension("", "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    await assert.rejects(
      run("ensureTemporaryChat()"),
      (err) => err.code === "bridge_ui_timeout",
      "composer absent : bridge_ui_timeout",
    );
  }

  // 9. Composer rendu après plusieurs polls : le DOM est relu, sans conserver
  // un nœud obsolète.
  {
    const { window, run } = loadExtension("", "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    window.setTimeout(() => { window.document.body.innerHTML = temporaryComposer; }, 300);
    await run("ensureTemporaryChat()");
  }

  // 10. Chemin comportemental réel : l'onglet est créé directement sur l'URL
  // Temporary, le composer existe et l'ancien markup SVG est absent. Le prompt
  // doit atteindre Send sans aucun contrôle Temporary.
  {
    const body = `<form id="composer-form">
      <textarea data-id="prompt"></textarea>
      <button aria-disabled="false" id="composer-submit-button" aria-label="Send prompt" data-testid="send-button">Send</button>
    </form><button data-testid="create-new-chat-button">New chat</button>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let sendClicks = 0;
    let submitEvents = 0;
    let newChatClicks = 0;
    window.document.querySelector("button[data-testid='send-button']").addEventListener("click", () => { sendClicks += 1; });
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
      window.document.querySelector("textarea[data-id='prompt']").value = "";
      window.document.body.insertAdjacentHTML("beforeend", `
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="assistant" data-message-id="msg-A1">
            <div class="markdown"><p>réponse finale</p></div>
          </div>
          ${copyButton}
        </article>`);
    });
    window.document.querySelector("button[data-testid='create-new-chat-button']").addEventListener("click", () => {
      newChatClicks += 1;
    });
    await run(`handlePrompt({ id: "req-A", prompt: "bonjour", new_chat: true, conversation: { id: "conv-A", mode: "fresh" } })`);
    assert.equal(window.document.querySelector("textarea[data-id='prompt']").value, "");
    assert.equal(newChatClicks, 0, "une conversation explicite ne doit jamais cliquer New Chat");
    assert.equal(submitEvents, 1, "le formulaire doit être soumis exactement une fois");
    assert.equal(sendClicks, 0, "requestSubmit ne doit pas dépendre d'un click synthétique");
    assert.equal(sent.some((message) => message.type === "error"), false, "aucune erreur pre_submission");
    assert.equal(sent.some((message) => message.type === "done"), true, "le chemin comportemental doit terminer");
  }

  // 10b. Un run stateless reçoit sa cible déjà réservée : il n'a aucune
  // autorisation de fabriquer un Temporary Chat par clic DOM.
  {
    const body = `<form id="composer-form">
      <textarea data-id="prompt"></textarea>
      <button aria-disabled="false" data-testid="send-button">Send</button>
    </form><button data-testid="create-new-chat-button">New chat</button>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let newChatClicks = 0;
    let submitEvents = 0;
    window.document.querySelector("button[data-testid='create-new-chat-button']").addEventListener("click", () => {
      newChatClicks += 1;
    });
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
      window.document.querySelector("textarea[data-id='prompt']").value = "";
      window.document.body.insertAdjacentHTML("beforeend", `
        <article data-testid="conversation-turn-stateless">
          <div data-message-author-role="assistant" data-message-id="msg-stateless">
            <div class="markdown"><p>réponse finale</p></div>
          </div>${copyButton}
        </article>`);
    });
    await run(`handlePrompt({ id: "req-stateless", prompt: "bonjour", new_chat: true, browser_target: { kind: "temporary_chat_run", id: "target-stateless" } })`);
    assert.equal(newChatClicks, 0, "un run stateless ne doit jamais cliquer New Chat");
    assert.equal(submitEvents, 1, "le run stateless doit soumettre une seule fois");
    assert.equal(sent.some((message) => message.type === "error"), false);
    assert.equal(sent.some((message) => message.type === "done"), true);
  }

  // 10c. La surface stateless sans browser_target est une erreur PRE_SUBMISSION.
  {
    const body = `<form id="composer-form"><textarea data-id="prompt"></textarea>
      <button aria-disabled="false" data-testid="send-button">Send</button></form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    await run(`handlePrompt({ id: "req-no-target", prompt: "bonjour", new_chat: true })`);
    const error = sent.find((message) => message.type === "error");
    assert.equal(error.code, "bridge_browser_target_required");
    assert.equal(error.phase, "pre_submission");
    assert.equal(error.submission_state, "pre_submission");
  }

  // 10a. Le tour utilisateur est la preuve la plus forte : le composer peut
  // rester rempli, le Stop peut être hors formulaire et l'assistant peut ne
  // pas encore être apparu.
  {
    const body = `<aside><button data-testid="stop-button">ancien Stop</button></aside>
      <form id="composer-form"><textarea data-id="prompt">bonjour</textarea>
        <button aria-disabled="false" data-testid="send-button">Send</button></form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    let submitEvents = 0;
    const form = window.document.querySelector("#composer-form");
    form.addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
      form.insertAdjacentHTML(
        "beforeend",
        `<div data-message-author-role="user">bonjour</div>`,
      );
    });
    const signal = await run(`(async () => {
      const composer = document.querySelector("textarea[data-id='prompt']");
      const send = document.querySelector("button[data-testid='send-button']");
      const before = captureSubmissionSnapshot(composer, send);
      triggerComposerSubmission(composer, send);
      return waitForSubmissionConfirmation(composer, send, before, "requestSubmit");
    })()`);
    assert.equal(signal, "user_turn");
    assert.equal(submitEvents, 1);
  }

  // 10a-bis. Une preuve qui arrive après l'ancienne fenêtre de 5 s reste
  // attachée au même run et réussit sans second trigger.
  {
    const body = `<form id="composer-form"><textarea data-id="prompt">bonjour</textarea>
      <button aria-disabled="false" data-testid="send-button">Send</button></form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    let clock = 0;
    let polls = 0;
    let userAdded = false;
    window.Date.now = () => clock;
    window.setTimeout = (fn, ms) => {
      clock += ms || 0;
      polls += 1;
      if (!userAdded && clock > 5_000) {
        userAdded = true;
        window.document.body.insertAdjacentHTML(
          "beforeend",
          `<div data-message-author-role="user">bonjour</div>`,
        );
      }
      queueMicrotask(fn);
      return 0;
    };
    let submitEvents = 0;
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
    });
    const signal = await run(`(async () => {
      const composer = document.querySelector("textarea[data-id='prompt']");
      const send = document.querySelector("button[data-testid='send-button']");
      const before = captureSubmissionSnapshot(composer, send);
      triggerComposerSubmission(composer, send);
      return waitForSubmissionConfirmation(composer, send, before, "requestSubmit");
    })()`);
    assert.equal(signal, "user_turn");
    assert.equal(submitEvents, 1, "un seul triggerComposerSubmission");
    assert.ok(clock > 5_000);
    assert.ok(polls > 0);
  }

  // 10a-ter. La borne finale sans preuve produit une erreur typée après un
  // seul trigger ; l'absence de confirmation n'autorise aucun rejeu.
  {
    const body = `<textarea data-id="prompt">bonjour</textarea>
      <button aria-disabled="false" data-testid="send-button">Send</button>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    let clicks = 0;
    window.document.querySelector("button[data-testid='send-button']").addEventListener(
      "click",
      () => { clicks += 1; },
    );
    let error;
    try {
      await run(`(async () => {
        const composer = document.querySelector("textarea[data-id='prompt']");
        const send = document.querySelector("button[data-testid='send-button']");
        const before = captureSubmissionSnapshot(composer, send);
        triggerComposerSubmission(composer, send);
        return waitForSubmissionConfirmation(composer, send, before, "click");
      })()`);
    } catch (caught) {
      error = caught;
    }
    assert.equal(error.code, "bridge_ui_timeout");
    assert.equal(error.diagnostics.composer_still_has_text, true);
    assert.equal(clicks, 1, "aucun second clic après un timeout ambigu");
  }

  // 10b. Un click observé sans effet de soumission ne suffit jamais : aucune
  // seconde méthode ne doit être tentée après l'échec de confirmation.
  {
    const body = `<form id="composer-form">
      <textarea data-id="prompt"></textarea>
      <button aria-disabled="false" id="composer-submit-button" aria-label="Send prompt" data-testid="send-button">Send</button>
    </form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let submitEvents = 0;
    let sendClicks = 0;
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
    });
    window.document.querySelector("button[data-testid='send-button']").addEventListener("click", () => { sendClicks += 1; });
    await run(`handlePrompt({ id: "req-no-confirm", prompt: "bonjour", conversation: { id: "conv-A", mode: "fresh" } })`);
    const error = sent.find((message) => message.type === "error");
    assert.equal(submitEvents, 1);
    assert.equal(sendClicks, 0);
    assert.equal(error?.code, "bridge_ui_timeout");
    assert.equal(error?.submission_state, "submission_attempted");
  }

  // 10c. Un bouton disabled avant tout trigger reste un échec pre_submission.
  {
    const body = `<form><textarea data-id="prompt"></textarea>
      <button aria-disabled="true" id="composer-submit-button" aria-label="Send prompt" data-testid="send-button">Send</button></form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    await run(`handlePrompt({ id: "req-pre", prompt: "bonjour", conversation: { id: "conv-A", mode: "fresh" } })`);
    assert.equal(sent.find((message) => message.type === "error")?.submission_state, "pre_submission");
  }

  // 10d. Une soumission confirmée puis une panne de génération est post_submission.
  {
    const body = `<form id="composer-form"><textarea data-id="prompt"></textarea>
      <button aria-disabled="false" id="composer-submit-button" aria-label="Send prompt" data-testid="send-button">Send</button></form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let submitEvents = 0;
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
      window.document.querySelector("textarea[data-id='prompt']").value = "";
    });
    await run(`handlePrompt({ id: "req-post", prompt: "bonjour", conversation: { id: "conv-A", mode: "fresh" } })`);
    assert.equal(submitEvents, 1);
    const error = sent.find((message) => message.type === "error");
    assert.equal(error?.code, "bridge_ui_timeout");
    assert.equal(error?.phase, "generation");
    assert.equal(error?.submission_state, "post_submission");
    assert.equal(error?.diagnostics?.composer_has_text, false);
    assert.equal(error?.diagnostics?.streaming_generation_signal_visible, false);
    assert.equal(error?.diagnostics?.assistant_turns_before, 0);
    assert.equal(error?.diagnostics?.assistant_turns_after, 0);
    assert.equal(
      JSON.stringify(error).includes("bonjour"),
      false,
      "les diagnostics de stall ne doivent pas contenir le prompt",
    );
  }

  // 10e. Le premier tour peut apparaître après plus de 30 s : une activité de
  // génération post-soumission garde l'attente en vie, les heartbeats restent
  // sans contenu, puis le même envoi aboutit sans second trigger.
  {
    const body = `<form id="composer-form"><textarea data-id="prompt"></textarea>
      <button aria-disabled="false" data-testid="send-button">Send</button></form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let clock = 0;
    let assistantAdded = false;
    window.Date.now = () => clock;
    window.setTimeout = (fn, ms) => {
      clock += ms || 0;
      if (!assistantAdded && clock >= 30_500) {
        assistantAdded = true;
        window.document.body.insertAdjacentHTML(
          "beforeend",
          `<article data-testid="conversation-turn-long">
            <div data-message-author-role="assistant" data-message-id="msg-long">
              <div class="markdown"><p>réponse après longue attente</p></div>
            </div>${copyButton}
          </article>`,
        );
      }
      queueMicrotask(fn);
      return 0;
    };
    let submitEvents = 0;
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
      window.document.querySelector("textarea[data-id='prompt']").value = "";
      window.document.querySelector("#composer-form").insertAdjacentHTML(
        "beforeend",
        `<div class="result-streaming"></div>`,
      );
    });

    await run(`handlePrompt({ id: "req-long-first-turn", prompt: "recherche longue", conversation: { id: "conv-long", mode: "fresh" } })`);

    assert.ok(clock > 30_000, "le tour doit être attendu au-delà de l'ancienne borne de 30 s");
    assert.equal(submitEvents, 1, "le formulaire ne doit être soumis qu'une fois");
    assert.equal(sent.filter((message) => message.type === "error").length, 0);
    assert.equal(sent.find((message) => message.type === "done")?.text, "réponse après longue attente");
    const heartbeats = sent.filter((message) => message.type === "heartbeat");
    assert.ok(heartbeats.length >= 5, "les heartbeats doivent continuer avant le premier tour");
    assert.ok(
      heartbeats.every(
        (message) =>
          message.progress?.phase === "waiting_answer" &&
          message.progress?.output_chars === 0,
      ),
      "l'attente du premier tour ne doit publier que de la liveness sans contenu",
    );
    assert.equal(
      heartbeats.some((message) => JSON.stringify(message).includes("recherche longue")),
      false,
      "un heartbeat ne doit jamais contenir le prompt",
    );
    assert.equal(
      heartbeats.some((message) => JSON.stringify(message).includes("réponse après longue attente")),
      false,
      "un heartbeat ne doit jamais contenir la réponse",
    );
  }

  // 10f. Les signaux Stop/reasoning/streaming déjà présents avant l'envoi ne
  // prolongent pas artificiellement l'attente d'un nouveau tour assistant.
  {
    const body = `<article data-testid="conversation-turn-stale">
        <div data-message-author-role="assistant" data-message-id="msg-stale">
          <div class="markdown"><p>ancienne réponse</p></div>
          <div class="result-streaming"></div>
        </div>
      </article>
      <details data-testid="reasoning" open><summary>Reasoning</summary></details>
      <form id="composer-form"><textarea data-id="prompt"></textarea>
        <button data-testid="stop-button" aria-label="Stop streaming">Stop</button>
        <button aria-disabled="false" data-testid="send-button">Send</button></form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let submitEvents = 0;
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
      window.document.querySelector("textarea[data-id='prompt']").value = "";
    });

    await run(`handlePrompt({ id: "req-stale-signals", prompt: "recherche", conversation: { id: "conv-stale", mode: "fresh" } })`);

    const error = sent.find((message) => message.type === "error");
    assert.equal(submitEvents, 1, "un signal DOM périmé ne doit jamais provoquer un second envoi");
    assert.equal(error?.code, "bridge_ui_timeout");
    assert.equal(error?.phase, "generation");
    assert.equal(error?.submission_state, "post_submission");
    assert.equal(error?.diagnostics?.assistant_turns_before, 1);
    assert.equal(error?.diagnostics?.assistant_turns_after, 1);
    assert.equal(error?.diagnostics?.stop_visible, true);
    assert.equal(error?.diagnostics?.reasoning_visible, true);
    assert.equal(error?.diagnostics?.streaming_generation_signal_visible, true);
  }

  // 10g. `generationSignalTransition` distingue les six cas observables. Une
  // persistance stricte n'est jamais de l'activité.
  {
    const { window, run } = loadExtension(
      `<div id="host"><div class="result-streaming" id="s1"></div></div>`,
    );
    const host = window.document.querySelector("#host");
    const capture = (name) =>
      run(`globalThis.${name} = currentSubmissionGenerationSignals(); null`);

    // signal déjà présent, strictement inchangé -> aucune transition
    capture("__a");
    capture("__b");
    assert.equal(run(`generationSignalTransition(__a, __b)`), null);

    // changement de signature/state sur le même élément
    window.document
      .querySelector("#s1")
      .setAttribute("class", "result-streaming busy");
    capture("__c");
    assert.equal(run(`generationSignalTransition(__b, __c)`), "changed");

    // nouvel élément apparu
    host.insertAdjacentHTML(
      "beforeend",
      `<button data-testid="stop-button" aria-label="Stop streaming"></button>`,
    );
    capture("__d");
    assert.equal(run(`generationSignalTransition(__c, __d)`), "appeared");

    // aucune mutation entre deux polls -> toujours aucune activité
    capture("__e");
    assert.equal(run(`generationSignalTransition(__d, __e)`), null);

    // disparition
    window.document.querySelector("#s1").remove();
    capture("__f");
    assert.equal(run(`generationSignalTransition(__e, __f)`), "disappeared");

    // plus aucun signal, deux fois de suite -> aucune activité
    window.document.querySelector("[data-testid='stop-button']").remove();
    capture("__g");
    capture("__h");
    assert.equal(run(`generationSignalTransition(__g, __h)`), null);
  }

  // 10h. Un signal de génération qui apparaît APRÈS Send puis reste
  // parfaitement figé ne doit pas rafraîchir l'activité à chaque poll :
  // l'attente doit finir en bridge_ui_timeout borné.
  {
    const body = `<form id="composer-form"><textarea data-id="prompt"></textarea>
      <button aria-disabled="false" data-testid="send-button">Send</button></form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let clock = 0;
    let polls = 0;
    window.Date.now = () => clock;
    window.setTimeout = (fn, ms) => {
      clock += ms || 0;
      polls += 1;
      if (polls > 20_000) {
        throw new Error(
          "le watchdog n'a jamais conclu : un signal figé maintient l'attente en vie",
        );
      }
      queueMicrotask(fn);
      return 0;
    };
    let submitEvents = 0;
    let sendClicks = 0;
    window.document.querySelector("button[data-testid='send-button']").addEventListener("click", () => { sendClicks += 1; });
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
      window.document.querySelector("textarea[data-id='prompt']").value = "";
      // Apparaît une fois, puis plus jamais aucune mutation.
      window.document.body.insertAdjacentHTML(
        "beforeend",
        `<div class="result-streaming" data-frozen="true"></div>`,
      );
    });

    await run(`handlePrompt({ id: "req-frozen-signal", prompt: "recherche figée", conversation: { id: "conv-frozen", mode: "fresh" } })`);

    const error = sent.find((message) => message.type === "error");
    assert.equal(error?.code, "bridge_ui_timeout");
    assert.equal(error?.phase, "generation");
    assert.equal(error?.submission_state, "post_submission");
    assert.equal(error?.diagnostics?.streaming_generation_signal_visible, true);
    assert.equal(error?.diagnostics?.assistant_turns_after, 0);
    assert.equal(submitEvents, 1, "aucune resoumission après un stall figé");
    assert.equal(sendClicks, 0);
    assert.ok(
      clock >= 300_000,
      `le stall ne doit pas être prématuré (clock=${clock})`,
    );
    assert.ok(
      clock < 400_000,
      `le stall doit rester borné par ACTIVE_SIGNAL_STALL_MS (clock=${clock})`,
    );
    assert.equal(
      JSON.stringify(sent).includes("recherche figée"),
      false,
      "ni heartbeat ni diagnostics ne doivent contenir le prompt",
    );
  }

  // 10i. Une vraie activité prolongée (signature qui change réellement) garde
  // le watchdog vivant bien au-delà de ACTIVE_SIGNAL_STALL_MS, puis le tour
  // assistant arrive et la finalisation se poursuit normalement.
  {
    const body = `<form id="composer-form"><textarea data-id="prompt"></textarea>
      <button aria-disabled="false" data-testid="send-button">Send</button></form>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let clock = 0;
    let polls = 0;
    let assistantAdded = false;
    window.Date.now = () => clock;
    window.setTimeout = (fn, ms) => {
      clock += ms || 0;
      polls += 1;
      if (polls > 40_000) throw new Error("boucle non bornée");
      const signal = window.document.querySelector(".result-streaming");
      if (signal && clock < 600_000) {
        signal.setAttribute("class", `result-streaming step-${polls}`);
      }
      if (!assistantAdded && clock >= 600_000) {
        assistantAdded = true;
        signal?.remove();
        window.document.body.insertAdjacentHTML(
          "beforeend",
          `<article data-testid="conversation-turn-slow">
            <div data-message-author-role="assistant" data-message-id="msg-slow">
              <div class="markdown"><p>réponse après recherche approfondie</p></div>
            </div>${copyButton}
          </article>`,
        );
      }
      queueMicrotask(fn);
      return 0;
    };
    let submitEvents = 0;
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submitEvents += 1;
      event.preventDefault();
      window.document.querySelector("textarea[data-id='prompt']").value = "";
      window.document.body.insertAdjacentHTML(
        "beforeend",
        `<div class="result-streaming"></div>`,
      );
    });

    await run(`handlePrompt({ id: "req-long-activity", prompt: "recherche approfondie", conversation: { id: "conv-long-activity", mode: "fresh" } })`);

    assert.ok(clock > 600_000, "l'attente doit dépasser largement 30 s et 300 s");
    assert.equal(sent.filter((message) => message.type === "error").length, 0);
    assert.equal(
      sent.find((message) => message.type === "done")?.text,
      "réponse après recherche approfondie",
    );
    assert.equal(submitEvents, 1, "exactement une soumission");
    const heartbeats = sent.filter((message) => message.type === "heartbeat");
    assert.ok(heartbeats.length >= 5, "le heartbeat doit continuer pendant l'attente");
    assert.equal(
      JSON.stringify(heartbeats).includes("recherche approfondie"),
      false,
      "aucun contenu de prompt dans les heartbeats",
    );
    assert.equal(
      JSON.stringify(heartbeats).includes("réponse après recherche approfondie"),
      false,
      "aucun contenu de réponse dans les heartbeats",
    );
  }

  // 11. CONTINUE sur une navigation /c/... : refus avant toute saisie/envoi.
  {
    const body = `${temporaryComposer}<button data-testid="send-button">Send</button>
      <article data-testid="conversation-turn-1"><div data-message-author-role="assistant" data-message-id="msg-A1"><div class="markdown">ancien</div></div></article>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/c/abc123");
    useVirtualClock(window);
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let sendClicks = 0;
    window.document.querySelector("button[data-testid='send-button']").addEventListener("click", () => { sendClicks += 1; });
    await run(`handlePrompt({ id: "req-continue", prompt: "suite", conversation: { id: "conv-A", mode: "continue", expected_turn_id: "msg-A1" } })`);
    assert.equal(sendClicks, 0, "une navigation /c/... interdit tout envoi");
    assert.equal(sent.find((message) => message.type === "error")?.code, "conversation_unavailable");
  }

  // 5. CONTINUE : le tour externe attendu existe -> trouvé par identité stable.
  {
    const { run } = loadExtension(page({ actions: true }));
    const found = run(
      `findAssistantTurnByExternalId("m3") !== null`,
    );
    assert.equal(found, true, "le tour attendu doit être retrouvé par son identifiant stable");
    assert.equal(run(`turnExternalId(document.querySelector("[data-testid='conversation-turn-3'] [data-message-author-role='assistant']"))`), "m3");
    assert.equal(run(`turnLocator(document.querySelector("[data-testid='conversation-turn-3'] [data-message-author-role='assistant']"))`), "conversation-turn-3");
  }

  // 6. CONTINUE : le tour externe attendu est absent -> aucune correspondance,
  //    jamais un repli sur le dernier tour visible ou un index.
  {
    const { run } = loadExtension(page({ actions: true }));
    const found = run(
      `findAssistantTurnByExternalId("conversation-turn-does-not-exist")`,
    );
    assert.equal(found, null, "aucun tour ne doit correspondre à un identifiant inconnu");
  }

  // A DOM locator without data-message-id is not a continuation identity.
  {
    const { run } = loadExtension(`
      <article data-testid="conversation-turn-9">
        <div data-message-author-role="assistant"><div class="markdown">réponse</div></div>
      </article>`);
    assert.equal(run(`turnExternalId(document.querySelector("[data-message-author-role='assistant']"))`), null);
    assert.equal(run(`findAssistantTurnByExternalId("conversation-turn-9")`), null);
  }

  // Recovery stateless is a read-only exact-target capture: only an explicitly
  // final answer with a stable external message id is returned, and no Send or
  // requestSubmit path is touched.
  {
    const body = `<form id="composer-form"><textarea data-id="prompt">draft intact</textarea>
      <button aria-disabled="false" data-testid="send-button">Send</button></form>
      <article data-testid="conversation-turn-9">
        <div data-message-author-role="assistant" data-message-id="stable-final">
          <div class="markdown"><p>réponse finale récupérable</p></div>
        </div>${copyButton}
      </article>`;
    const { window, run } = loadExtension(body);
    let clicks = 0;
    let submits = 0;
    window.document.querySelector("button[data-testid='send-button']").addEventListener("click", () => {
      clicks += 1;
    });
    window.document.querySelector("#composer-form").addEventListener("submit", (event) => {
      submits += 1;
      event.preventDefault();
    });
    const preview = await run(`captureLaterResponse(${JSON.stringify({
      id: "recovery-1",
      bridge_run_id: "run-1",
      browser_target: { kind: "temporary_chat_run", id: "target-1" },
    })})`);
    assert.equal(preview.target_id, "target-1");
    assert.equal(preview.bridge_run_id, "run-1");
    assert.equal(preview.turn_id, "stable-final");
    assert.equal(preview.text, "réponse finale récupérable");
    assert.equal(clicks, 0);
    assert.equal(submits, 0);
    assert.equal(window.document.querySelector("textarea[data-id='prompt']").value, "draft intact");
  }

  // A local container/testid without data-message-id is never accepted as a
  // recovery identity.
  {
    const { run } = loadExtension(`
      <article data-testid="conversation-turn-10">
        <div data-message-author-role="assistant"><div class="markdown"><p>final</p></div></div>
        ${copyButton}
      </article>`);
    const preview = await run(`captureLaterResponse(${JSON.stringify({
      id: "recovery-no-id",
      bridge_run_id: "run-no-id",
      browser_target: { kind: "temporary_chat_run", id: "target-no-id" },
    })})`);
    assert.equal(preview.error, "aucune réponse finale postérieure au tour initial");
  }

  // 7. Aucune attente de 15s sur un locator de conversation ne subsiste.
  {
    const source = fs.readFileSync(path.join(EXTENSION, "content.js"), "utf8");
    assert.equal(source.includes("toggle.click()"), false, "le bridge ne doit jamais cliquer le toggle Temporary");
    assert.equal(source.includes("temporaryChatCheckedIcon"), false, "l'ancien signal SVG ne doit plus exister");
    assert.equal(source.includes("SELECTORS.newChat"), false, "New Chat ne doit plus faire partie du chemin stateless");
    assert.ok(
      !source.includes("locator de conversation non attribué"),
      "l'ancienne attente de locator de conversation ne doit plus exister",
    );
    assert.ok(
      !source.includes("verifiedLocator"),
      "verifiedLocator ne doit plus exister en tant que concept d'identité",
    );
  }

  console.log("temporary chat + continuation identity contract: ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});

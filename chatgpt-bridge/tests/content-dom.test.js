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
    const body = `<textarea data-id="prompt"></textarea><button data-testid="send-button">Send</button>`;
    const { window, run } = loadExtension(body, "https://chatgpt.com/?temporary-chat=true");
    useVirtualClock(window);
    const sent = [];
    window.chrome.runtime.sendMessage = async (message) => { sent.push(message); };
    let sendClicks = 0;
    window.document.querySelector("button[data-testid='send-button']").addEventListener("click", () => {
      sendClicks += 1;
      window.document.body.insertAdjacentHTML("beforeend", `
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="assistant" data-message-id="msg-A1">
            <div class="markdown"><p>réponse finale</p></div>
          </div>
          ${copyButton}
        </article>`);
    });
    await run(`handlePrompt({ id: "req-A", prompt: "bonjour", conversation: { id: "conv-A", mode: "fresh" } })`);
    assert.equal(window.document.querySelector("textarea[data-id='prompt']").value, "bonjour");
    assert.equal(sendClicks, 1, "le chemin fresh doit cliquer Send exactement une fois");
    assert.equal(sent.some((message) => message.type === "error"), false, "aucune erreur pre_submission");
    assert.equal(sent.some((message) => message.type === "done"), true, "le chemin comportemental doit terminer");
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

  // 7. Aucune attente de 15s sur un locator de conversation ne subsiste.
  {
    const source = fs.readFileSync(path.join(EXTENSION, "content.js"), "utf8");
    assert.equal(source.includes("toggle.click()"), false, "le bridge ne doit jamais cliquer le toggle Temporary");
    assert.equal(source.includes("temporaryChatCheckedIcon"), false, "l'ancien signal SVG ne doit plus exister");
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

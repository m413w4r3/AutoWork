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
function loadExtension(body) {
  const dom = new JSDOM(`<!doctype html><html><body>${body}</body></html>`, {
    runScripts: "outside-only",
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
  assert.ok(
    result.stable_for_ms >= 120_000,
    `stabilité attendue >= 120000 ms, vue ${result.stable_for_ms}`,
  );
  assert.ok(sleeps > 0, "la boucle doit réellement avoir tourné");

  console.log("content dom scope contract: ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});

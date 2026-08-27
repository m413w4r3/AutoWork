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
function temporaryChatToggleMarkup(active) {
  return `
    <button aria-label="Temporary chat">
      <svg class="${active ? "" : "opacity-0"}"><use href="#chat-temp-checked"></use></svg>
    </button>`;
}

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
  // 1. Temporary Chat déjà actif -> succès immédiat, aucun clic.
  {
    const { window, run } = loadExtension(
      `<main>${temporaryChatToggleMarkup(true)}</main>`,
    );
    useVirtualClock(window);
    let clicked = false;
    window.document
      .querySelector("button[aria-label='Temporary chat']")
      .addEventListener("click", () => {
        clicked = true;
      });
    await run("ensureTemporaryChat()");
    assert.equal(clicked, false, "un toggle déjà actif ne doit jamais être cliqué");
  }

  // 2. Clic puis remplacement du nœud (rendu React) -> la vérification doit
  //    re-interroger le DOM, pas fermer sur l'ancien nœud.
  {
    const { window, run } = loadExtension(
      `<main>${temporaryChatToggleMarkup(false)}</main>`,
    );
    useVirtualClock(window);
    window.document.addEventListener("click", (evt) => {
      const target = evt.target;
      if (
        target &&
        target.matches &&
        target.matches("button[aria-label='Temporary chat']")
      ) {
        const replacement = window.document.createElement("button");
        replacement.setAttribute("aria-label", "Temporary chat");
        replacement.innerHTML = '<svg><use href="#chat-temp-checked"></use></svg>';
        target.replaceWith(replacement);
      }
    });
    await run("ensureTemporaryChat()");
    const stillOpacityZero = run(
      "document.querySelector(\"button[aria-label='Temporary chat'] svg\").classList.contains('opacity-0')",
    );
    assert.equal(
      stillOpacityZero,
      false,
      "après rerendu, la vérification doit lire le nouveau nœud actif",
    );
  }

  // 3. Bascule introuvable -> échec typé avant tout Send, jamais un
  //    repli silencieux.
  {
    const { window, run } = loadExtension(`<main></main>`);
    useVirtualClock(window);
    await assert.rejects(
      run("ensureTemporaryChat()"),
      (err) => err.code === "bridge_ui_timeout",
      "bascule introuvable : doit lever bridge_ui_timeout, jamais réussir silencieusement",
    );
  }

  // 4. Bascule trouvée mais l'activation n'est jamais confirmée après le clic.
  {
    const { window, run } = loadExtension(
      `<main>${temporaryChatToggleMarkup(false)}</main>`,
    );
    useVirtualClock(window);
    // Aucun listener de clic : le toggle ne passe jamais actif.
    await assert.rejects(
      run("ensureTemporaryChat()"),
      (err) => err.code === "bridge_ui_timeout",
      "activation non confirmée : bridge_ui_timeout, jamais un faux succès",
    );
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

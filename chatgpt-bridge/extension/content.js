/**
 * Content script injecté sur chatgpt.com : reçoit un prompt du service worker,
 * le tape dans le composer, puis observe le DOM avant de renvoyer un snapshot final.
 *
 * Tous les sélecteurs dépendants de l'UI OpenAI sont regroupés dans SELECTORS
 * ci-dessous : c'est le seul bloc à retoucher si l'interface change.
 */

// Affichée au chargement : permet de vérifier dans la console quel code tourne
// réellement dans l'onglet (recharger l'extension ne suffit pas à le remplacer).
const VERSION = "17";

// Journalise dans la console les décisions de la boucle de streaming, à chaque
// changement d'état. Utile quand l'UI d'OpenAI change et qu'une réponse arrive
// tronquée ou dupliquée : la ligne indique l'état de fin détecté, le conteneur
// lu et le nombre de blocs de code vus.
const DEBUG = false;

const SELECTORS = {
  composer: [
    "#prompt-textarea",
    "div[contenteditable='true'][id^='prompt']",
    "textarea[data-id]",
  ],
  send: [
    "button[data-testid='send-button']",
    "#composer-submit-button",
    "button[aria-label*='Envoyer']",
    "button[aria-label*='Send']",
  ],
  stop: [
    "button[data-testid='stop-button']",
    "button[aria-label*='Stop']",
    "button[aria-label*='rrêter']",
  ],
  fileInput: ["input[type='file']"],
  assistant: "[data-message-author-role='assistant']",
  markdown: ".markdown",
  newChat: [
    "a[data-testid='create-new-chat-button']",
    "button[data-testid='create-new-chat-button']",
  ],
  // Conteneurs de la phase de réflexion : leur texte n'est pas la réponse.
  reasoning: [
    "[data-testid*='thinking']",
    "[data-testid*='reasoning']",
    "[data-testid*='thought']",
    "[data-message-model-slug] details",
  ],
  // Barre d'actions rendue sous une réponse *terminée* : signal de fin le plus
  // fiable, car elle n'existe pas tant que ChatGPT écrit (ni pendant sa réflexion).
  turnActions: [
    "[data-testid='copy-turn-action-button']",
    "button[data-testid*='copy-turn']",
    "button[aria-label*='Copier']",
    "button[aria-label*='Copy response']",
  ],
  // Conteneur d'un échange complet. La barre d'actions vit ici, *au-dessus* du
  // div [data-message-author-role] : chercher dans le seul tour ne la trouve pas.
  turnContainer: ["[data-testid^='conversation-turn']", "article"],

  // --- Contrôles de l'interface (cf. section « Contrôles typés » plus bas) --- //
  // Déclencheur du sélecteur de modèle, dans l'en-tête de la conversation.
  modelTrigger: [
    "button[data-testid='model-switcher-dropdown-button']",
    "[data-testid='model-switcher-dropdown-button']",
    "button[aria-label*='Modèle']",
    "button[aria-label*='Model']",
  ],
  // Déclencheur du sélecteur de compte / espace de travail.
  profileTrigger: [
    "button[data-testid='accounts-profile-button']",
    "[data-testid='accounts-profile-button']",
    "button[aria-label*='Profil']",
    "button[aria-label*='Account']",
  ],
  // Menu ouvert, et ses entrées (Radix : role=menu / menuitem).
  menu: ["[role='menu']", "[role='listbox']"],
  menuItem: ["[role='menuitem']", "[role='menuitemradio']", "[role='option']"],
  // Bouton dédié à la recherche web, cherché *dans le composer* uniquement :
  // la barre latérale a elle aussi un bouton « Rechercher » (dans les chats).
  searchToggle: [
    "button[data-testid='composer-button-search']",
    "button[data-testid*='search']",
    "button[aria-label*='Recherche web']",
    "button[aria-label*='Search the web']",
  ],
  // Menu d'outils du composer (« + »), où la recherche se trouve dans certaines
  // versions de l'UI au lieu d'un bouton dédié.
  toolsTrigger: [
    "button[data-testid='composer-plus-btn']",
    "button[id^='system-hint']",
    "button[aria-haspopup='menu'][aria-label*='Ajouter']",
  ],
};

// Libellés reconnus comme « recherche web » dans un menu d'outils (FR/EN).
const MOTS_RECHERCHE =
  /recherche web|rechercher sur le web|search the web|web search/;
// Entrée d'un menu de modèles repliant les autres modèles dans un sous-menu.
const MOTS_PLUS_MODELES = /plus de mod|autres mod|more models|legacy models/;

const POLL_MS = 120;
const APPEAR_TIMEOUT_MS = 30000; // délai d'apparition de la bulle de réponse
const UPLOAD_TIMEOUT_MS = 120000; // upload des pièces jointes
// const SETTLE_MS = 2000; // stabilité exigée quand un signal de fin est confirmé
// const SETTLE_UNKNOWN_MS = 8000; // stabilité exigée quand aucun signal n'est reconnu
// const EMPTY_FINAL_SETTLE_MS = 10000; // fin fiable sans corps final
// const NO_MARKDOWN_FALLBACK_MS = 25000; // au-delà, on lit le tour entier faute de mieux
// const HEARTBEAT_INTERVAL_MS = 20000; // réarme l'idle timeout sans transmettre le texte

const SETTLE_MS = 2000; // fin UI confirmée
const SETTLE_UNKNOWN_MS = 15000; // pas de signal UI fiable : prudence
const EMPTY_FINAL_SETTLE_MS = 10000;
const NO_MARKDOWN_FALLBACK_MS = 25000;
const HEARTBEAT_INTERVAL_MS = 5000;

// Une réponse non vide et inchangée ne doit jamais rester "running"
// pendant plusieurs minutes uniquement à cause d'un signal DOM périmé.
const FINALIZATION_STALL_MS = 45000;

// Garde-fou du cas symétrique : l'UI se prétend encore active (`finished=false`,
// donc le garde-fou ci-dessus est désarmé) alors que la réponse n'a plus bougé
// d'un caractère. On ne conclut pas « terminé » — un Stop réellement visible
// peut signifier que ChatGPT travaille — mais on rend la main en `incomplete`
// plutôt que de rester « running » indéfiniment.
// Volontairement large : une recherche approfondie marque de vraies pauses de
// plusieurs minutes sans écrire un caractère. Ce garde-fou vise la boucle
// infinie, jamais une génération lente encore en cours.
const ACTIVE_SIGNAL_STALL_MS = 300000;

let currentJob = null;
const claimedRequestIds = new Set();
let persistedRequestIdsPromise = chrome.storage.local
  .get("submittedRequestIds")
  .then(({ submittedRequestIds }) => new Set(submittedRequestIds || []));

async function claimPrompt(id) {
  if (claimedRequestIds.has(id)) return false;
  claimedRequestIds.add(id);
  const persisted = await persistedRequestIdsPromise;
  if (persisted.has(id)) return false;
  // Persister avant toute manipulation du DOM : après un arrêt du content
  // script, la sécurité at-most-once prime sur une resoumission implicite.
  persisted.add(id);
  const bounded = [...persisted].slice(-1000);
  persistedRequestIdsPromise = Promise.resolve(new Set(bounded));
  await chrome.storage.local.set({ submittedRequestIds: bounded });
  return true;
}

// --------------------------------------------------------------------------- //
// Utilitaires DOM
// --------------------------------------------------------------------------- //
/** Premier élément de `root` correspondant à l'un des sélecteurs, dans l'ordre. */
const $in = (root, list) => {
  for (const sel of list) {
    const el = root.querySelector(sel);
    if (el) return el;
  }
  return null;
};

const $ = (list) => $in(document, list);

/** Premier ancêtre de `el` correspondant à l'un des sélecteurs. */
const closestOf = (el, list) => {
  for (const sel of list) {
    const found = el.closest(sel);
    if (found) return found;
  }
  return null;
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, timeout, label) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = fn();
    if (value) return value;
    await sleep(100);
  }
  throw new Error(`Timeout : ${label}`);
}

/**
 * Écrit `text` dans le composer.
 * ProseMirror ignore les mutations directes du DOM : on passe par
 * `insertText`, qui produit la même séquence d'évènements qu'une vraie frappe.
 */
function typePrompt(el, text) {
  el.focus();
  if (el.tagName === "TEXTAREA") {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "value",
    ).set;
    setter.call(el, text);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  if (!document.execCommand("insertText", false, text)) {
    // Repli : évènement paste synthétique (accepté par ProseMirror).
    const dt = new DataTransfer();
    dt.setData("text/plain", text);
    el.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: dt,
        bubbles: true,
        cancelable: true,
      }),
    );
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

/**
 * Dépose des fichiers dans le composer.
 * ChatGPT écoute un `input[type=file]` caché : on lui injecte un FileList
 * fabriqué via DataTransfer, seule façon d'alimenter un input file par script.
 */
async function attachFiles(files) {
  const input = $(SELECTORS.fileInput);
  if (!input) throw new Error("champ d'upload introuvable sur la page");

  const dt = new DataTransfer();
  for (const f of files) {
    const bin = atob(f.data);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    dt.items.add(
      new File([bytes], f.name, { type: f.mime || "application/octet-stream" }),
    );
  }

  input.files = dt.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  // Repli : certaines versions de l'UI n'écoutent que le drop sur le composer.
  const composer = $(SELECTORS.composer);
  if (composer) {
    composer.dispatchEvent(
      new DragEvent("drop", {
        dataTransfer: dt,
        bubbles: true,
        cancelable: true,
      }),
    );
  }
}

/**
 * Sérialise une bulle de réponse en texte proche du Markdown source.
 * On ne peut pas se contenter de `innerText` : il embarque le libellé des
 * boutons « Copier » des blocs de code et perd les délimiteurs.
 */
const DOM_SERIALIZER = globalThis.ChatGPTBridgeSerializer;

/**
 * Conteneur portant la réponse finale.
 * Les blocs de réflexion (« Thinking ») sont rendus dans leur propre conteneur,
 * avant la réponse : on lit donc toujours le dernier, jamais le premier.
 */
/** Identifiant stable du tour (ex. « conversation-turn-10 »), survit aux re-rendus. */
function turnLocator(turn) {
  const container = closestOf(turn, SELECTORS.turnContainer);
  return container ? container.getAttribute("data-testid") : null;
}

/** Retrouve le tour courant à partir de son identifiant, jamais d'un nœud gardé. */
function findTurn(locator, before) {
  if (locator) {
    const container = document.querySelector(
      `[data-testid="${CSS.escape(locator)}"]`,
    );
    const turn = container && container.querySelector(SELECTORS.assistant);
    if (turn) return turn;
  }
  const turns = document.querySelectorAll(SELECTORS.assistant);
  return turns.length > before ? turns[turns.length - 1] : null;
}

function answerRoot(turn, fallbackOk) {
  const blocks = [...turn.querySelectorAll(SELECTORS.markdown)].filter(
    (b) => !SELECTORS.reasoning.some((sel) => b.closest(sel)),
  );
  if (blocks.length) return blocks[blocks.length - 1];

  // Aucun conteneur .markdown : mesuré sur l'UI réelle, c'est l'état de la phase
  // de réflexion. Le texte du tour vaut alors « Thinking » — surtout ne pas le
  // lire comme une réponse. Le repli sur le tour entier n'est autorisé qu'une
  // fois la réponse terminée (ou après un long délai), au cas où une réponse
  // n'utiliserait pas .markdown du tout.
  return fallbackOk ? turn : null;
}

/**
 * Dernier bloc de code de premier niveau. ChatGPT imbrique un `<pre>` dans un
 * autre (mesuré : `pre=2` pour un seul bloc) ; comme la branche PRE du
 * sérialiseur ne descend pas dans ses enfants, seul le `<pre>` extérieur est
 * réellement visité — c'est donc lui qu'il faut désigner comme « en cours ».
 */
function dernierPre(root) {
  const pres = [...root.querySelectorAll("pre")].filter(
    (p) => !(p.parentElement && p.parentElement.closest("pre")),
  );
  const pre = pres[pres.length - 1];
  if (!pre) return null;

  // Un bloc n'est « en cours d'écriture » que si rien ne le suit. Dès qu'un
  // paragraphe apparaît après lui il est terminé : le laisser ouvert ferait
  // arriver sa fermeture après du texte déjà transmis, et le diff par préfixe
  // réémettrait toute la suite.
  const suite = document.createRange();
  suite.setStartAfter(pre);
  suite.setEnd(root, root.childNodes.length);
  return suite.toString().trim() ? null : pre;
}

function readAnswer(root, streaming) {
  // Tant que ChatGPT écrit, le dernier bloc de code est celui en cours.
  const ouvert = streaming ? dernierPre(root) : null;
  return DOM_SERIALIZER.serializeResponse(root, ouvert);
}

/**
 * La réponse est-elle terminée ?  true / false / null quand aucun signal connu
 * n'est reconnaissable — ce dernier cas est capital : conclure « terminé » par
 * défaut tronquait la réponse pendant la phase de réflexion (« Thinking »).
 */
function completionState(turn) {
  const scope =
    closestOf(turn, SELECTORS.turnContainer) || turn.parentElement || turn;
  // Le Stop est un contrôle de la génération courante : il ne se cherche que
  // dans le composer. Un bouton portant le même libellé ailleurs dans la page
  // ne doit jamais maintenir ce tour en état « running ». Volontairement sans
  // `composerRoot()`, dont le repli sur document.body rendrait le scope inutile :
  // composer introuvable => pas de signal, plutôt qu'un signal de toute la page.
  const composer = $(SELECTORS.composer);
  const generationControls =
    composer && (closestOf(composer, ["form"]) || composer.parentElement);
  const visible = (element) => {
    if (!element || element.getAttribute?.("aria-hidden") === "true")
      return false;
    const style = globalThis.getComputedStyle?.(element);
    if (style?.display === "none" || style?.visibility === "hidden")
      return false;
    return (
      typeof element.getClientRects !== "function" ||
      element.getClientRects().length > 0
    );
  };
  const activeReasoning = (element) => {
    if (element?.tagName === "DETAILS" && !element.open) return false;
    if (["closed", "collapsed"].includes(element?.getAttribute?.("data-state")))
      return false;
    return visible(element);
  };
  return globalThis.ChatGPTBridgeCompletion.completionState({
    stopVisible: Boolean(
      generationControls &&
      SELECTORS.stop.some((selector) =>
        [...generationControls.querySelectorAll(selector)].some(visible),
      ),
    ),
    // Le streaming se lit dans le tour surveillé : un indicateur laissé par un
    // ancien tour ou par un widget latéral ne doit pas empêcher sa finalisation.
    streamingVisible: Boolean(
      [
        ...scope.querySelectorAll(
          ".streaming-animation, .result-streaming, [data-is-streaming='true']",
        ),
      ].some(visible),
    ),
    reasoningVisible: SELECTORS.reasoning.some((selector) =>
      [...scope.querySelectorAll(selector)].some(activeReasoning),
    ),
    actionsVisible: SELECTORS.turnActions.some((selector) =>
      [...scope.querySelectorAll(selector)].some(visible),
    ),
    // Conservé uniquement comme observation : le moteur pur l'ignore volontairement.
    sendVisible: Boolean($(SELECTORS.send)),
  });
}

// --------------------------------------------------------------------------- //
// Contrôles typés de l'interface : modèle, profil, recherche web
//
// Règle unique de cette section : agir, puis **relire l'état dans le DOM**.
// Rien n'est déclaré appliqué sans cette relecture. Quand elle est impossible
// (bouton absent, état non exposé par l'UI), le résultat porte `ok: false` /
// `verified: false` et une `reason` — que le serveur remonte au client, plutôt
// que de laisser croire qu'un réglage a pris. Un contrôle non vérifiable doit
// dégrader visiblement, jamais silencieusement.
// --------------------------------------------------------------------------- //

/** Minuscules, sans accents ni espaces multiples : base des comparaisons. */
const norm = (s) =>
  (s || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();

/** Identifiant comparable : « GPT-5 Thinking » -> « gpt-5-thinking ». */
const slug = (s) =>
  norm(s)
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");

/** Libellé d'un déclencheur (bouton portant l'état courant) : une seule ligne. */
const triggerLabel = (el) => norm(el.innerText || el.textContent || "");

/**
 * Libellé d'une entrée de menu : sa première ligne seulement. Les entrées de
 * modèle portent une description en dessous (« Réfléchit plus longtemps »),
 * qui n'appartient pas au nom.
 */
function itemLabel(el) {
  const lignes = (el.innerText || el.textContent || "").split("\n");
  for (const ligne of lignes) {
    const texte = ligne.replace(/\s+/g, " ").trim();
    if (texte) return texte;
  }
  return "";
}

/** Identifiant d'une entrée : son `data-testid` si l'UI en pose un, sinon son libellé. */
function itemId(el, label) {
  const testid = el.getAttribute("data-testid") || "";
  const m = testid.match(
    /^(?:model-switcher|model|account|workspace|profile)-(.+)$/,
  );
  return m ? slug(m[1]) : slug(label);
}

/**
 * État on/off d'un bouton, tel que l'UI l'expose. `null` = non exposé, et c'est
 * une information à part entière : un bouton dont l'état n'est pas lisible ne
 * permet aucune vérification, donc aucune promesse.
 */
function pressedState(el) {
  for (const attr of ["aria-pressed", "aria-checked", "aria-selected"]) {
    const v = el.getAttribute(attr);
    if (v === "true") return true;
    if (v === "false") return false;
  }
  // `data-state` sert aussi à Radix pour dire si un menu est ouvert : `open` et
  // `closed` ne disent rien d'un réglage, les lire comme on/off inventerait un
  // état vérifié qui n'existe pas.
  const state = norm(el.getAttribute("data-state") || "");
  if (["on", "checked", "active"].includes(state)) return true;
  if (["off", "unchecked", "inactive"].includes(state)) return false;
  return null;
}

/** Formulaire du composer : périmètre des boutons d'outils de l'envoi. */
function composerRoot() {
  const composer = $(SELECTORS.composer);
  return (
    (composer && (closestOf(composer, ["form"]) || composer.parentElement)) ||
    document.body
  );
}

/** Ouvre le menu d'un déclencheur et renvoie l'élément de menu, ou lève. */
async function openMenu(trigger, label) {
  if (trigger.getAttribute("aria-expanded") !== "true") trigger.click();
  return waitFor(
    () => {
      for (const sel of SELECTORS.menu) {
        for (const menu of document.querySelectorAll(sel)) {
          if ($in(menu, SELECTORS.menuItem)) return menu;
        }
      }
      return null;
    },
    4000,
    `menu « ${label} » jamais ouvert`,
  );
}

/** Referme un menu ouvert. Radix écoute Échap sur le document. */
function closeMenu(menu) {
  const evt = () =>
    new KeyboardEvent("keydown", {
      key: "Escape",
      code: "Escape",
      keyCode: 27,
      bubbles: true,
      cancelable: true,
    });
  if (menu) menu.dispatchEvent(evt());
  document.dispatchEvent(evt());
}

/** Entrées d'un menu, dédoublonnées, dans l'ordre du document. */
function menuItems(menu) {
  const vus = new Set();
  const items = [];
  for (const sel of SELECTORS.menuItem) {
    for (const el of menu.querySelectorAll(sel)) {
      if (vus.has(el)) continue;
      vus.add(el);
      const label = itemLabel(el);
      if (!label) continue;
      items.push({
        el,
        label,
        id: itemId(el, label),
        checked: pressedState(el),
      });
    }
  }
  return items;
}

/**
 * Entrée correspondant à `wanted`, par paliers : identifiant exact, libellé
 * exact, puis préfixe, puis inclusion. Les paliers évitent qu'un « gpt-5 »
 * demandé attrape « GPT-5 Thinking » alors que « GPT-5 » existe dans la liste.
 */
function pickItem(items, wanted) {
  const cible = slug(wanted);
  if (!cible) return null;
  const tests = [
    (i) => i.id === cible,
    (i) => slug(i.label) === cible,
    (i) => i.id.startsWith(cible) || slug(i.label).startsWith(cible),
    (i) => i.id.includes(cible) || slug(i.label).includes(cible),
  ];
  for (const test of tests) {
    const trouve = items.find(test);
    if (trouve) return trouve;
  }
  return null;
}

/** L'état relu correspond-il à ce qui a été demandé ? */
function labelMatches(wanted, label) {
  const a = slug(wanted);
  const b = slug(label);
  return Boolean(a && b && (a === b || b.includes(a) || a.includes(b)));
}

// --------------------------------------------------------------------------- //
// Lecture d'état (sans effet de bord)
// --------------------------------------------------------------------------- //

// Formes complètes des deux états lus : toutes les clés sont toujours présentes,
// pour que le serveur n'ait jamais à distinguer « absent » de « inconnu ».
const etatPicker = (extra) => ({
  supported: false,
  selected: null,
  selected_id: null,
  verified: false,
  reason: null,
  ...extra,
});

const etatRecherche = (extra) => ({
  supported: null,
  enabled: null,
  verified: false,
  via: null,
  reason: null,
  ...extra,
});

/** État d'un sélecteur à déclencheur (modèle, profil) : lecture seule. */
function readPicker(selectors, quoi) {
  const trigger = $(selectors);
  if (!trigger) return etatPicker({ reason: `${quoi} absent de la page` });

  const label = triggerLabel(trigger);
  if (!label)
    return etatPicker({
      supported: true,
      reason: `${quoi} sans libellé lisible`,
    });

  return etatPicker({
    supported: true,
    selected: label,
    selected_id: slug(label),
    verified: true,
  });
}

/**
 * État de la recherche web, lu sans ouvrir de menu.
 * `supported: null` = indéterminé : l'UI place parfois la recherche dans le
 * menu d'outils, qu'une simple lecture n'a pas le droit d'ouvrir.
 */
function readWebSearch() {
  const btn = $in(composerRoot(), SELECTORS.searchToggle);
  if (btn) {
    const pressed = pressedState(btn);
    return etatRecherche({
      supported: true,
      enabled: pressed,
      verified: pressed !== null,
      via: "composer_toggle",
      reason:
        pressed === null
          ? "bouton présent, mais son état on/off n'est pas exposé par l'UI"
          : null,
    });
  }
  const tools = $in(composerRoot(), SELECTORS.toolsTrigger);
  return etatRecherche({
    supported: tools ? null : false,
    via: tools ? "tools_menu" : null,
    reason: tools
      ? "aucun bouton dédié : état seulement lisible en ouvrant le menu d'outils (sonde)"
      : "ni bouton de recherche ni menu d'outils dans le composer",
  });
}

/** Énumère les entrées d'un menu, puis le referme. Effet de bord assumé (sonde). */
async function probeMenu(selectors, quoi) {
  const trigger = $(selectors);
  if (!trigger) return null;
  let menu = null;
  try {
    menu = await openMenu(trigger, quoi);
    return menuItems(menu).map((i) => ({
      id: i.id,
      label: i.label,
      checked: i.checked,
    }));
  } catch {
    return null;
  } finally {
    closeMenu(menu);
    await sleep(150);
  }
}

/** Cherche la recherche web dans le menu d'outils, puis referme. */
async function probeWebSearch() {
  const tools = $in(composerRoot(), SELECTORS.toolsTrigger);
  if (!tools) return null;
  let menu = null;
  try {
    menu = await openMenu(tools, "outils du composer");
    const item = menuItems(menu).find((i) =>
      MOTS_RECHERCHE.test(norm(i.label)),
    );
    if (!item) {
      return etatRecherche({
        supported: false,
        via: "tools_menu",
        reason: "aucune entrée de recherche web dans le menu d'outils",
      });
    }
    return etatRecherche({
      supported: true,
      enabled: item.checked,
      verified: item.checked !== null,
      via: "tools_menu",
      reason:
        item.checked === null
          ? "entrée trouvée, mais son état n'est pas exposé par l'UI"
          : null,
    });
  } catch (err) {
    return etatRecherche({ via: "tools_menu", reason: err.message });
  } finally {
    closeMenu(menu);
    await sleep(150);
  }
}

/**
 * Photographie de l'état pilotable de l'interface.
 * `probe` autorise l'ouverture des menus (nécessaire pour énumérer les modèles) :
 * c'est visible à l'écran, donc jamais fait pendant une génération.
 */
async function uiState(probe) {
  const state = {
    observed_at: Date.now() / 1000,
    url: location.href,
    content_script_version: VERSION,
    probed: Boolean(probe),
    model: readPicker(SELECTORS.modelTrigger, "sélecteur de modèle"),
    profile: readPicker(SELECTORS.profileTrigger, "sélecteur de profil"),
    web_search: readWebSearch(),
  };
  if (probe) {
    state.model.available = await probeMenu(SELECTORS.modelTrigger, "modèles");
    state.profile.available = await probeMenu(
      SELECTORS.profileTrigger,
      "profils",
    );
    if (state.web_search.supported !== true || !state.web_search.verified) {
      const sonde = await probeWebSearch();
      if (sonde) state.web_search = sonde;
    }
  }
  return state;
}

// --------------------------------------------------------------------------- //
// Application des contrôles (avec relecture obligatoire)
// --------------------------------------------------------------------------- //

/** Résultats typés d'un contrôle : jamais `ok` sans relecture concordante. */
const echec = (requested, reason, extra) => ({
  requested,
  applied: null,
  verified: false,
  ok: false,
  changed: false,
  reason,
  ...extra,
});

const succes = (requested, applied, changed, extra) => ({
  requested,
  applied,
  verified: true,
  ok: true,
  changed,
  reason: null,
  ...extra,
});

/**
 * Sélectionne une entrée dans un menu à déclencheur, puis vérifie que le
 * libellé du déclencheur reflète bien le choix. Sans cette concordance, le
 * contrôle est un échec — même si le clic a eu lieu.
 */
async function selectFromPicker(selectors, wanted, quoi) {
  const trigger = $(selectors);
  if (!trigger) return echec(wanted, `${quoi} absent de la page`);

  const avant = triggerLabel(trigger);
  if (labelMatches(wanted, avant)) return succes(wanted, avant, false);

  let menu = null;
  try {
    menu = await openMenu(trigger, quoi);
    const items = menuItems(menu);
    let item = pickItem(items, wanted);

    if (!item) {
      // Les modèles secondaires sont repliés dans un sous-menu.
      const plus = items.find(
        (i) =>
          MOTS_PLUS_MODELES.test(norm(i.label)) ||
          i.el.getAttribute("aria-haspopup") === "menu",
      );
      if (plus) {
        plus.el.dispatchEvent(
          new PointerEvent("pointermove", { bubbles: true }),
        );
        plus.el.click();
        await sleep(250);
        for (const sel of SELECTORS.menu) {
          for (const sous of document.querySelectorAll(sel)) {
            if (sous === menu) continue;
            const trouve = pickItem(menuItems(sous), wanted);
            if (trouve) {
              item = trouve;
              break;
            }
          }
          if (item) break;
        }
      }
    }

    if (!item) {
      return echec(wanted, `« ${wanted} » absent du ${quoi}`, {
        available: items.map((i) => ({ id: i.id, label: i.label })),
      });
    }

    item.el.click();
  } catch (err) {
    return echec(wanted, err.message);
  } finally {
    closeMenu(menu);
  }

  // Relecture : c'est elle, et elle seule, qui autorise `ok: true`.
  const applied = await waitFor(
    () => {
      const t = $(selectors);
      const label = t && triggerLabel(t);
      return label && labelMatches(wanted, label) ? label : null;
    },
    6000,
    "relecture du sélecteur",
  ).catch(() => null);

  if (!applied) {
    const t = $(selectors);
    const vu = t ? triggerLabel(t) : "?";
    return echec(
      wanted,
      `clic effectué mais ${quoi} affiche toujours « ${vu} »`,
    );
  }
  return succes(wanted, applied, true);
}

/**
 * Active ou désactive la recherche web, en préférant le bouton dédié du
 * composer (dont l'état est lisible) au menu d'outils (dont l'effet ne se
 * vérifie qu'indirectement, par l'apparition du bouton).
 */
async function setWebSearch(want) {
  const avant = readWebSearch();

  if (avant.verified && avant.enabled === want)
    return succes(want, want, false, { via: avant.via });

  const btn = $in(composerRoot(), SELECTORS.searchToggle);
  if (btn && avant.enabled !== null) {
    btn.click();
    const apres = await waitFor(
      () => {
        const s = readWebSearch();
        return s.verified && s.enabled === want ? s : null;
      },
      3000,
      "relecture du bouton de recherche",
    ).catch(() => null);
    if (apres) return succes(want, want, true, { via: apres.via });
    return echec(want, "clic sans changement d'état observable", {
      via: "composer_toggle",
    });
  }

  // Repli : l'entrée du menu d'outils. Vérification indirecte — l'activation
  // fait apparaître le bouton dédié dans le composer, la désactivation le retire.
  const tools = $in(composerRoot(), SELECTORS.toolsTrigger);
  if (!tools) {
    const raison =
      avant.reason || "aucun contrôle de recherche web dans le composer";
    return echec(want, raison, { via: avant.via });
  }
  let menu = null;
  try {
    menu = await openMenu(tools, "outils du composer");
    const item = menuItems(menu).find((i) =>
      MOTS_RECHERCHE.test(norm(i.label)),
    );
    if (!item)
      return echec(want, "aucune entrée de recherche web dans ce menu", {
        via: "tools_menu",
      });
    if (item.checked === want)
      return succes(want, want, false, { via: "tools_menu" });
    item.el.click();
  } catch (err) {
    return echec(want, err.message, { via: "tools_menu" });
  } finally {
    closeMenu(menu);
  }

  const apres = await waitFor(
    () => {
      const present = Boolean($in(composerRoot(), SELECTORS.searchToggle));
      return present === want ? { present } : null;
    },
    4000,
    "relecture du composer",
  ).catch(() => null);

  if (!apres) {
    return echec(want, "entrée cliquée mais le composer ne la reflète pas", {
      via: "tools_menu",
    });
  }
  return succes(want, want, true, { via: "tools_menu" });
}

/** Applique les contrôles demandés (les clés absentes ou nulles ne sont pas touchées). */
async function applyControls(controls) {
  const resultats = {};
  // Le profil d'abord : changer d'espace de travail recharge la liste des modèles.
  if (typeof controls.profile === "string" && controls.profile) {
    const quoi = "sélecteur de profil";
    resultats.profile = await selectFromPicker(
      SELECTORS.profileTrigger,
      controls.profile,
      quoi,
    );
  }
  if (typeof controls.model === "string" && controls.model) {
    resultats.model = await selectFromPicker(
      SELECTORS.modelTrigger,
      controls.model,
      "sélecteur de modèle",
    );
  }
  if (typeof controls.web_search === "boolean") {
    resultats.web_search = await setWebSearch(controls.web_search);
  }
  return resultats;
}

/** Requête de contrôle/lecture venue du serveur : toujours une réponse typée. */
async function handleUi(msg) {
  try {
    const applied =
      msg.type === "ui_control"
        ? await applyControls(msg.controls || {})
        : null;
    const state = await uiState(msg.probe);
    const ok = !applied || Object.values(applied).every((r) => r.ok);
    return { type: msg.type, id: msg.id, ok, applied, state, error: null };
  } catch (err) {
    return {
      type: msg.type,
      id: msg.id,
      ok: false,
      applied: null,
      state: null,
      error: err.message,
    };
  }
}

// --------------------------------------------------------------------------- //
// Cycle de vie d'une requête
// --------------------------------------------------------------------------- //
function reply(payload) {
  chrome.runtime.sendMessage(payload).catch(() => {});
}

/**
 * Suit la réponse dans le DOM sans transmettre les snapshots intermédiaires.
 * Chaque observation remplace la précédente, car le rendu n'est pas append-only.
 */
async function streamAnswer(job, locator, before) {
  const output = globalThis.ChatGPTBridgeFinalOutput.createAccumulator();
  let vu = ""; // relevé précédent, pour mesurer la stabilité
  let stableSince = null;
  let full = "";
  let debugSig = "";
  let completionSignature = "";
  const debut = Date.now();
  let lastHeartbeatAt = debut;
  let finalSerialized = null;
  let finalCompletion = {
    finished: null,
    signal: "unknown",
    confidence: "low",
  };
  let stableForMs = 0;

  // Progression persistante entre les itérations, indépendante de la présence du tour.
  // Le heartbeat est un signal de liveness, pas une preuve que le DOM est lisible.
  let lastProgress = {
    phase: "waiting_answer",
    output_chars: 0,
    stable_for_ms: 0,
    completion_signal: "unknown",
    completion_confidence: "low",
  };

  while (!job.aborted) {
    await sleep(POLL_MS);

    const now = Date.now();

    // Liveness indépendant du DOM : le heartbeat doit être émis même quand
    // ChatGPT remplace temporairement le tour assistant (recherche web, reasoning).
    if (now - lastHeartbeatAt >= HEARTBEAT_INTERVAL_MS) {
      reply({
        type: "heartbeat",
        id: job.id,
        progress: lastProgress,
      });
      lastHeartbeatAt = now;
    }

    // Re-recherche du tour à chaque itération, jamais de référence gardée :
    // React remplace le nœud du message entre la phase de réflexion et la
    // réponse, et un nœud détaché resterait figé sur « Thinking ».
    const turn = findTurn(locator, before);
    if (!turn) continue;

    // `finished === false` (ChatGPT écrit encore) interdit de sortir ; `null`
    // (aucun signal reconnu) exige une stabilité bien plus longue.
    const completion = completionState(turn);
    const finished = completion.finished;
    const nextCompletionSignature = `${finished}:${completion.signal}`;
    if (nextCompletionSignature !== completionSignature) {
      completionSignature = nextCompletionSignature;
      stableSince = null;
    }
    const root = answerRoot(
      turn,
      finished === true || Date.now() - debut > NO_MARKDOWN_FALLBACK_MS,
    );
    const snapshot = root ? readAnswer(root, finished !== true) : null;
    full = snapshot ? snapshot.text : "";
    output.observe(full);

    if (DEBUG) {
      const pres = root ? root.querySelectorAll("pre") : [];
      const sig = `fini=${finished} root=${root ? root.tagName + "." + (root.className || "-").slice(0, 24) : "null"} pre=${pres.length}`;
      if (sig !== debugSig) {
        debugSig = sig;
        console.log(
          `[bridge] ${sig} | queue=${JSON.stringify(full.slice(-40))}`,
        );
      }
    }

    if (full !== vu) {
      vu = full;
      stableSince = null;
    } else if (stableSince === null) {
      stableSince = Date.now();
    }

    const need =
      finished === true && full.length === 0
        ? EMPTY_FINAL_SETTLE_MS
        : finished === null
          ? SETTLE_UNKNOWN_MS
          : SETTLE_MS;
    stableForMs = stableSince === null ? 0 : Date.now() - stableSince;
    const stable = stableForMs >= need;

    // Mettre à jour l'état courant pour le prochain heartbeat.
    // Ce calcul n'envoie rien : le heartbeat lui-même est émis plus haut,
    // indépendamment de la présence du tour.
    const phase =
      completion.signal === "reasoning"
        ? "reasoning"
        : completion.signal === "stop_button" ||
            completion.signal === "streaming"
          ? "generating"
          : full.length === 0
            ? "waiting_answer"
            : stableForMs > 0
              ? "stabilizing"
              : "answering";

    lastProgress = {
      phase,
      output_chars:
        globalThis.ChatGPTBridgeFinalOutput.outputChars(full),
      stable_for_ms: stableForMs,
      completion_signal: completion.signal,
      completion_confidence: completion.confidence,
    };
    const outcome = globalThis.ChatGPTBridgeFinalOutput.settledOutcome({
      completion,
      text: full,
      stableForMs,
      emptySettleMs: EMPTY_FINAL_SETTLE_MS,
    });
    if (outcome === "incomplete") {
      return {
        text: "",
        visible_citations: [],
        serializer_version: DOM_SERIALIZER.SERIALIZER_VERSION,
        completion_signal: completion.signal,
        completion_confidence: completion.confidence,
        stable_for_ms: stableForMs,
        incomplete: true,
        incomplete_reason: "no_final_answer",
        turn_locator: turnLocator(turn),
      };
    }

    if (
      full.length > 0 &&
      finished !== false &&
      stableForMs >= FINALIZATION_STALL_MS
    ) {
      return {
        text: full,
        visible_citations: snapshot?.visible_citations || [],
        serializer_version: DOM_SERIALIZER.SERIALIZER_VERSION,
        completion_signal: completion.signal,
        completion_confidence: completion.confidence,
        stable_for_ms: stableForMs,
        incomplete: true,
        incomplete_reason: "finalization_stalled",
        turn_locator: turnLocator(turn),
      };
    }

    if (
      full.length > 0 &&
      finished === false &&
      stableForMs >= ACTIVE_SIGNAL_STALL_MS
    ) {
      return {
        text: full,
        visible_citations: snapshot?.visible_citations || [],
        serializer_version: DOM_SERIALIZER.SERIALIZER_VERSION,
        completion_signal: completion.signal,
        completion_confidence: completion.confidence,
        stable_for_ms: stableForMs,
        incomplete: true,
        incomplete_reason: "active_signal_stalled",
        turn_locator: turnLocator(turn),
      };
    }
    //if (stable && finished !== false && full.length > 0) {
      //const verificationRoot = answerRoot(turn, true);
      //const verification = verificationRoot
        //? readAnswer(verificationRoot, false)
        //: null;
      //const citationsIdentical =
        //verification &&
        //JSON.stringify(verification.visible_citations) ===
          //JSON.stringify(snapshot.visible_citations);
      //if (verification && verification.text === full && citationsIdentical) {
        //output.observe(verification.text);
        //finalSerialized = verification;
        //finalCompletion = completion;
        //break;
      //}
      //vu = verification ? verification.text : "";
      //stableSince = null;
    //}
    if (stable && finished !== false && full.length > 0) {
      const verificationRoot = answerRoot(turn, true);
      const verification = verificationRoot
        ? readAnswer(verificationRoot, false)
        : null;

      // La décision de fin porte uniquement sur le contenu textuel.
      // Les citations restent des métadonnées et peuvent encore être
      // réordonnées/enrichies par l'UI après la fin visible de la réponse.
      if (verification && verification.text === full) {
        output.observe(verification.text);
        finalSerialized = verification;
        finalCompletion = completion;
        break;
      }

      // Le texte a réellement changé entre les deux lectures :
      // on recommence la fenêtre de stabilisation.
      vu = verification ? verification.text : "";
      stableSince = null;
    }
  }

  const serialized = finalSerialized || {
    text: output.final(),
    visible_citations: [],
    serializer_version: DOM_SERIALIZER.SERIALIZER_VERSION,
  };
  return {
    ...serialized,
    completion_signal: finalCompletion.signal,
    completion_confidence: finalCompletion.confidence,
    stable_for_ms: stableForMs,
  };
}

function verifiedLocator() {
  const url = new URL(window.location.href);
  if (
    url.protocol !== "https:" ||
    !["chatgpt.com", "chat.openai.com"].includes(url.hostname) ||
    url.username ||
    url.password ||
    url.hash ||
    url.pathname === "/"
  ) {
    return null;
  }
  return url.toString();
}

async function handlePrompt({
  id,
  prompt,
  new_chat: newChat,
  files,
  conversation,
}) {
  if (!(await claimPrompt(id))) {
    reply({ type: "ack", id, state: "duplicate", duplicate: true });
    return;
  }
  if (currentJob) currentJob.aborted = true;
  const job = { id, aborted: false };
  currentJob = job;

  try {
    if (
      conversation?.mode === "continue" &&
      window.location.href !== conversation.external_locator
    ) {
      throw new Error("la page ne correspond pas au locator demandé");
    }
    if (newChat) {
      const link = $(SELECTORS.newChat);
      if (link) {
        link.click();
      } else {
        throw new Error("bouton nouveau chat introuvable");
      }
      await sleep(1200);
    }

    const composer = await waitFor(
      () => $(SELECTORS.composer),
      15000,
      "composer introuvable",
    );
    const before = document.querySelectorAll(SELECTORS.assistant).length;
    const baselineTurn = before
      ? document.querySelectorAll(SELECTORS.assistant)[before - 1]
      : null;

    if (files && files.length) await attachFiles(files);
    if (prompt) typePrompt(composer, prompt);

    // Le bouton d'envoi ne devient actif qu'après le rendu de la saisie — et,
    // s'il y a des pièces jointes, qu'une fois leur upload terminé (bien plus long).
    const sendBtn = await waitFor(
      () => {
        const b = $(SELECTORS.send);
        return b && !b.disabled ? b : null;
      },
      files && files.length ? UPLOAD_TIMEOUT_MS : 8000,
      files && files.length
        ? "upload des pièces jointes non terminé"
        : "bouton d'envoi jamais actif",
    );
    sendBtn.click();

    const externalLocator = conversation
      ? await waitFor(
          () => verifiedLocator(),
          15000,
          "locator de conversation non attribué",
        )
      : null;

    if (conversation) {
      reply({
        type: "conversation_bound",
        id,
        conversation: {
          id: conversation.id,
          external_locator: externalLocator,
          assistant_turns_before: before,
          initial_assistant_turn_id: baselineTurn
            ? turnLocator(baselineTurn) ||
              baselineTurn.getAttribute("data-message-id")
            : null,
          verified: true,
          verified_at: new Date().toISOString(),
        },
      });
    }

    // Attendre la bulle de réponse *nouvelle* (pas la précédente).
    const premier = await waitFor(
      () => {
        const turns = document.querySelectorAll(SELECTORS.assistant);
        return turns.length > before ? turns[turns.length - 1] : null;
      },
      APPEAR_TIMEOUT_MS,
      "pas de réponse de ChatGPT",
    );
    const serialized = await streamAnswer(job, turnLocator(premier), before);

    if (!job.aborted) {
      const container = closestOf(premier, SELECTORS.turnContainer);
      const externalTurnId =
        container?.getAttribute("data-testid") ||
        premier.getAttribute("data-message-id") ||
        null;
      reply({
        type: serialized.incomplete ? "incomplete" : "done",
        id,
        reason: serialized.incomplete_reason,
        text: serialized.text,
        metadata: {
          visible_citations: serialized.visible_citations,
          serializer_version: serialized.serializer_version,
          completion_signal: serialized.completion_signal,
          completion_confidence: serialized.completion_confidence,
          stable_for_ms: serialized.stable_for_ms,
          output_chars:
            globalThis.ChatGPTBridgeFinalOutput.outputChars(serialized.text),
          visible_citation_count: serialized.visible_citations.length,
          content_script_version: VERSION,
          initial_turn_id: externalTurnId || serialized.turn_locator || null,
        },
        conversation: conversation
          ? {
              id: conversation.id,
              // Read again at completion: right after submission ChatGPT still
              // exposes an optimistic `/c/WEB:<id>` URL, which it replaces with
              // the canonical one once the turn is persisted. Storing the early
              // value made every follow-up turn open a new tab and fail.
              external_locator: verifiedLocator() || externalLocator,
              turn_id: externalTurnId || `bridge-${id}`,
              mode: conversation.mode,
              verified: true,
            }
          : null,
      });
    }
  } catch (err) {
    if (!job.aborted) reply({ type: "error", id, message: err.message });
  } finally {
    if (currentJob === job) currentJob = null;
  }
}

async function captureLaterResponse(msg) {
  const expected = Number(msg.conversation?.assistant_turns_before || 0);
  const turns = [...document.querySelectorAll(SELECTORS.assistant)];
  const later = turns.slice(expected);
  for (let index = later.length - 1; index >= 0; index -= 1) {
    const turn = later[index];
    //const completion = completionState(turn);
    //if (completion.finished !== true) continue;
    //const root = answerRoot(turn, true);
    const completion = completionState(turn);

    // On refuse seulement une réponse explicitement encore active.
    // Un état DOM "unknown" est acceptable pour une PREVIEW humaine.
    if (completion.finished === false) continue;

    const root = answerRoot(turn, true);
    const serialized = root ? readAnswer(root, false) : null;
    if (!serialized?.text?.trim()) continue;
    const container = closestOf(turn, SELECTORS.turnContainer);
    return {
      type: "recovery_preview",
      id: msg.id,
      text: serialized.text,
      conversation_id: msg.conversation.id,
      external_locator: verifiedLocator(),
      turn_id:
        container?.getAttribute("data-testid") ||
        turn.getAttribute("data-message-id") ||
        null,
      metadata: {
        visible_citations: serialized.visible_citations,
        serializer_version: serialized.serializer_version,
        output_chars: globalThis.ChatGPTBridgeFinalOutput.outputChars(
          serialized.text,
        ),
        completion_signal: completion.signal,
        completion_confidence: completion.confidence,
        content_script_version: VERSION,
        capture_confidence:
          completion.finished === true
          ? "verified_final"
          : "visible_unknown",
      },
    };
  }
  return {
    type: "recovery_preview",
    id: msg.id,
    error: "aucune réponse finale postérieure au tour initial",
  };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "ui_state" || msg?.type === "ui_control") {
    // Requête/réponse : le service worker attend la valeur, d'où le `return true`
    // sans acquittement immédiat (un seul `sendResponse` est autorisé).
    handleUi(msg).then(sendResponse);
    return true;
  }
  if (msg?.type === "recovery_capture") {
    captureLaterResponse(msg).then(sendResponse);
    return true;
  }
  if (msg?.type === "prompt") {
    handlePrompt(msg);
  } else if (msg?.type === "abort") {
    if (currentJob && currentJob.id === msg.id) currentJob.aborted = true;
    const stop = $(SELECTORS.stop);
    if (stop) stop.click();
  }
  sendResponse({ ok: true });
  return true;
});

console.log(
  `🔌 ChatGPT Mini-Bridge : content script prêt — version ${VERSION}`,
);

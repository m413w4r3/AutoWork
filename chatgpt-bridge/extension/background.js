/**
 * Service worker : détient l'unique WebSocket vers le serveur local et relaie
 * les prompts vers le content script de l'onglet chatgpt.com.
 *
 * Le WebSocket vit ici plutôt que dans le content script pour deux raisons :
 * il survit aux navigations SPA, et il n'est pas soumis à la CSP de la page.
 */

const DEFAULT_URL = "ws://127.0.0.1:8001/ws";
const RECONNECT_MIN = 1000;
const RECONNECT_MAX = 30000;

const REPLACED_BACKOFF = 60000; // après un remplacement, on laisse la place

// Toute conversation fraîche ouverte par le bridge est un Temporary Chat :
// jamais écrite dans l'historique ChatGPT, donc jamais à en supprimer après
// coup. C'est l'URL canonique — on ne dépend jamais d'une navigation
// ultérieure vers /c/... pour obtenir une identité.
const TEMPORARY_CHAT_URL = "https://chatgpt.com/?temporary-chat=true";
const ALLOWED_CHAT_ORIGINS = ["https://chatgpt.com", "https://chat.openai.com"];

let socket = null;
let reconnectDelay = RECONNECT_MIN;
let reconnectTimer = null;
let suppressUntil = 0;
let status = { connected: false, lastError: null, url: DEFAULT_URL };
/** id de requête -> id de l'onglet qui la traite */
const inflight = new Map();
/** Deuxième barrière persistante : un id reçu n'est jamais retransmis deux fois au DOM. */
const requestStates = new Map();
const eventCounters = new Map();
/**
 * Registre des sessions live : conversation.id (UUID applicatif) -> onglet
 * Chrome exact. C'est l'identité de routage — jamais l'URL. Vit uniquement
 * dans chrome.storage.session : un redémarrage du service worker le
 * recharge, mais un redémarrage du navigateur / rechargement de l'extension
 * le perd volontairement (Temporary Chat n'est alors plus reconstructible).
 */
const conversationRegistry = new Map();
const busyTabs = new Set();
const requestConversationResults = new Map();
const requestExtensionMetadata = new Map();
const requestFinalOutputs = new Map();
const requestConversationBindings = new Map();
/** target_id de run stateless -> onglet Temporary Chat exact. */
const browserTargetRegistry = new Map();
/** id de requête -> binding de routage observé (diagnostics same-tab). */
const requestRoutes = new Map();
/** Sérialise la réservation initiale d'une même target dans un service worker. */
const browserTargetReservations = new Map();

// --------------------------------------------------------------------------- //
// Autonomie de l'onglet d'arrière-plan
//
// L'onglet de génération est créé volontairement inactif et ne doit jamais
// avoir besoin d'être focalisé. Deux protections indépendantes vivent ici :
//   - `autoDiscardable = false` pendant un run lié, pour que Chrome ne décharge
//     pas l'onglet exact sous pression mémoire (jamais d'activation) ;
//   - un `observe_tick` cadencé par le ping du serveur, horloge que le
//     throttling des minuteries de page n'atteint pas. Le tick ne fait que
//     réveiller la boucle du content script : il n'émet ni heartbeat ni `done`
//     et ne peut donc jamais prétendre que l'observateur DOM est vivant.
// --------------------------------------------------------------------------- //

/** Marque/démarque l'onglet exact comme non déchargeable. Jamais d'activation. */
async function setTabAutoDiscardable(tabId, autoDiscardable) {
  if (typeof tabId !== "number") return;
  try {
    await chrome.tabs.update(tabId, { autoDiscardable });
  } catch (_) {
    // Un onglet disparu ou une API indisponible ne doit jamais faire échouer
    // un run : c'est une protection opportuniste, pas une garantie.
  }
}

/** L'onglet est-il encore lié à une conversation live ou à une target ? */
function tabIsStillBound(tabId) {
  for (const entry of conversationRegistry.values()) {
    if (entry.tab_id === tabId) return true;
  }
  for (const entry of browserTargetRegistry.values()) {
    if (entry.tab_id === tabId) return true;
  }
  return false;
}

/**
 * Rend l'onglet déchargeable à nouveau — sauf s'il reste lié (KEEP), auquel
 * cas un déchargement casserait le CONTINUE de cette conversation exacte.
 */
async function releaseTabAutoDiscardable(tabId) {
  if (typeof tabId !== "number" || tabIsStillBound(tabId)) return;
  await setTabAutoDiscardable(tabId, true);
}

/**
 * État de l'onglet exact d'un run, sans contenu : uniquement les champs de
 * cycle de vie que Chrome expose. Jamais d'autre onglet que celui-là.
 */
async function boundTabState(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    let windowFocused = null;
    try {
      const window = await chrome.windows?.get(tab.windowId);
      windowFocused = window?.focused ?? null;
    } catch (_) {
      windowFocused = null;
    }
    return {
      tab_id: tab.id ?? tabId,
      exists: true,
      active: tab.active ?? null,
      discarded: tab.discarded ?? null,
      auto_discardable: tab.autoDiscardable ?? null,
      status: tab.status ?? null,
      window_focused: windowFocused,
    };
  } catch (_) {
    return { tab_id: tabId, exists: false };
  }
}

async function logBoundTabState(phase, requestId, tabId) {
  console.log("bridge_run_phase", {
    phase,
    bridge_run_id: requestId,
    tab_state: await boundTabState(tabId),
  });
}

/**
 * Réveille la boucle d'observation des onglets exacts encore en vol.
 * Déclenché par le ping serveur (20 s), donc par une horloge extérieure à la
 * page : c'est ce qui rend la détection de fin indépendante du throttling
 * d'arrière-plan sans jamais activer ni focaliser l'onglet.
 */
function pumpObservationTicks() {
  for (const [requestId, tabId] of inflight.entries()) {
    chrome.tabs
      .sendMessage(tabId, { type: "observe_tick", id: requestId })
      .catch(() => {});
  }
}

/** Erreur de routage typée : `.code` est ce que le serveur doit voir, jamais aplati. */
class BridgeRoutingError extends Error {
  constructor(code, message) {
    super(message || code);
    this.code = code;
  }
}

const requestStatesReady = chrome.storage.local.get("bridgeRequestStates").then(({ bridgeRequestStates }) => {
  for (const [id, state] of Object.entries(bridgeRequestStates || {})) requestStates.set(id, state);
});

// Métadonnées de rejeu final : utiles après coup (recovery, idempotence),
// jamais des bindings d'onglet vivants. Restent en chrome.storage.local.
const replayMetadataReady = chrome.storage.local
  .get([
    "bridgeRequestConversationResults",
    "bridgeRequestExtensionMetadata",
    "bridgeRequestFinalOutputs",
    "bridgeRequestConversationBindings",
  ])
  .then(
    ({
      bridgeRequestConversationResults,
      bridgeRequestExtensionMetadata,
      bridgeRequestFinalOutputs,
      bridgeRequestConversationBindings,
    }) => {
      for (const [id, value] of Object.entries(bridgeRequestConversationResults || {})) {
        requestConversationResults.set(id, value);
      }
      for (const [id, value] of Object.entries(bridgeRequestExtensionMetadata || {})) {
        requestExtensionMetadata.set(id, value);
      }
      for (const [id, value] of Object.entries(bridgeRequestFinalOutputs || {})) {
        if (typeof value === "string") requestFinalOutputs.set(id, value);
      }
      for (const [id, value] of Object.entries(bridgeRequestConversationBindings || {})) {
        requestConversationBindings.set(id, value);
      }
    },
  );

// Le registre de sessions live vit exclusivement dans chrome.storage.session :
// il doit survivre à une suspension/relance du service worker, mais jamais à
// un redémarrage du navigateur ou un rechargement de l'extension.
const conversationRegistryReady = chrome.storage.session
  .get("bridgeConversationRegistry")
  .then(({ bridgeConversationRegistry }) => {
    for (const [id, entry] of Object.entries(bridgeConversationRegistry || {})) {
      conversationRegistry.set(id, entry);
    }
  });

const browserTargetRegistryReady = chrome.storage.session
  .get("bridgeBrowserTargetRegistry")
  .then(({ bridgeBrowserTargetRegistry }) => {
    for (const [id, entry] of Object.entries(bridgeBrowserTargetRegistry || {})) {
      browserTargetRegistry.set(id, entry);
    }
  });

function persistRequestStates() {
  const entries = [...requestStates.entries()].slice(-1000);
  chrome.storage.local.set({ bridgeRequestStates: Object.fromEntries(entries) });
}

function persistReplayMetadata() {
  chrome.storage.local.set({
    bridgeRequestConversationResults: Object.fromEntries([...requestConversationResults.entries()].slice(-1000)),
    bridgeRequestExtensionMetadata: Object.fromEntries([...requestExtensionMetadata.entries()].slice(-1000)),
    bridgeRequestFinalOutputs: Object.fromEntries([...requestFinalOutputs.entries()].slice(-50)),
    bridgeRequestConversationBindings: Object.fromEntries(
      [...requestConversationBindings.entries()].slice(-1000),
    ),
  });
}

function persistConversationRegistry() {
  chrome.storage.session.set({
    bridgeConversationRegistry: Object.fromEntries(conversationRegistry.entries()),
  });
}

function persistBrowserTargetRegistry() {
  chrome.storage.session.set({
    bridgeBrowserTargetRegistry: Object.fromEntries(browserTargetRegistry.entries()),
  });
}

async function serverUrl() {
  const { serverUrl } = await chrome.storage.local.get("serverUrl");
  return serverUrl || DEFAULT_URL;
}

async function authenticatedServerUrl() {
  const [url, stored] = await Promise.all([serverUrl(), chrome.storage.local.get("wsToken")]);
  const parsed = new URL(url);
  if (stored.wsToken) parsed.searchParams.set("token", stored.wsToken);
  return parsed.toString();
}

function setStatus(patch) {
  status = { ...status, ...patch };
  chrome.storage.local.set({ status });
}

async function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  // Un autre client détient volontairement le pont : ne pas le lui reprendre
  // en boucle (sinon les deux se volent la connexion indéfiniment).
  if (Date.now() < suppressUntil) return;
  clearTimeout(reconnectTimer);
  const displayUrl = await serverUrl();
  const url = await authenticatedServerUrl();
  setStatus({ url: displayUrl });

  try {
    socket = new WebSocket(url);
  } catch (err) {
    scheduleReconnect(String(err));
    return;
  }

  socket.onopen = () => {
    reconnectDelay = RECONNECT_MIN;
    setStatus({ connected: true, lastError: null });
    send({ type: "hello", client: "extension-chrome" });
    flush(); // rejoue ce qui a été produit pendant la coupure
    console.log("🤖 Connecté au Mini-Bridge", displayUrl, enAttente.length ? "(file non vidée)" : "");
  };

  socket.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (msg.type === "ping") {
      send({ type: "pong" }); // maintient aussi le service worker éveillé
      pumpObservationTicks();
      return;
    }
    if (msg.type === "prompt") {
      handlePrompt(msg);
    } else if (msg.type === "ui_state" || msg.type === "ui_control") {
      handleUiRequest(msg);
    } else if (msg.type === "conversation_archive") {
      handleConversationArchive(msg);
    } else if (msg.type === "recovery_capture") {
      handleRecoveryCapture(msg);
    } else if (msg.type === "browser_target_retain") {
      handleBrowserTargetRetain(msg);
    } else if (msg.type === "browser_target_release") {
      handleBrowserTargetRelease(msg);
    } else if (msg.type === "abort") {
      const tabId = inflight.get(msg.id);
      inflight.delete(msg.id);
      if (tabId !== undefined) {
        chrome.tabs.sendMessage(tabId, { type: "abort", id: msg.id }).catch(() => {});
      }
    }
  };

  socket.onclose = (event) => {
    if (event.code === 4000) {
      // Le serveur nous a remplacés par un autre client (fake_extension.py,
      // un second profil Chrome…). On s'efface au lieu de reprendre la main.
      suppressUntil = Date.now() + REPLACED_BACKOFF;
      scheduleReconnect("remplacé par un autre client du pont");
      return;
    }
    scheduleReconnect(null);
  };
  socket.onerror = () => setStatus({ lastError: "serveur injoignable" });
}

async function handleRecoveryCapture(msg) {
  if (msg.browser_target) {
    let tab;
    try {
      tab = await resolveRecoverableBrowserTarget(msg.browser_target, msg.bridge_run_id);
    } catch (err) {
      send({
        type: "recovery_preview",
        id: msg.id,
        code: err.code || "recovery_unavailable",
        error: err.message,
      });
      return;
    }
    try {
      const result = await sendToTab(tab.id, msg);
      send({
        ...result,
        type: "recovery_preview",
        id: msg.id,
        target_id: msg.browser_target.id,
        bridge_run_id: msg.bridge_run_id,
        tab_id: tab.id,
      });
    } catch (err) {
      send({
        type: "recovery_preview",
        id: msg.id,
        code: "recovery_unavailable",
        error: err.message,
        target_id: msg.browser_target.id,
        bridge_run_id: msg.bridge_run_id,
        tab_id: tab.id,
      });
    }
    return;
  }
  await conversationRegistryReady;
  const known = conversationRegistry.get(msg.conversation.id);
  if (!known) {
    send({ type: "recovery_preview", id: msg.id, error: "conversation_unavailable" });
    return;
  }
  let tab;
  try {
    tab = await chrome.tabs.get(known.tab_id);
  } catch {
    // L'onglet n'existe plus : la session live est perdue, jamais reconstruite.
    conversationRegistry.delete(msg.conversation.id);
    persistConversationRegistry();
    send({ type: "recovery_preview", id: msg.id, error: "conversation_unavailable" });
    return;
  }
  if (!isAllowedChatOrigin(tab.url)) {
    send({ type: "recovery_preview", id: msg.id, error: "conversation_unavailable" });
    return;
  }
  try {
    const result = await sendToTab(tab.id, msg);
    send({ ...result, type: "recovery_preview", id: msg.id });
  } catch (err) {
    send({ type: "recovery_preview", id: msg.id, error: err.message });
  }
}

async function handleConversationArchive(msg) {
  await conversationRegistryReady;
  const known = conversationRegistry.get(msg.conversation_id);
  console.log(
    "🗂️ conversation_archive reçu — ferme la session Temporary Chat exacte (jamais " +
      "écrite dans l'historique ChatGPT, donc rien à y supprimer)",
    { conversation_id: msg.conversation_id, tab_id: known?.tab_id ?? null },
  );
  if (known?.tab_id) await chrome.tabs.remove(known.tab_id).catch(() => {});
  conversationRegistry.delete(msg.conversation_id);
  persistConversationRegistry();
  send({ type: "conversation_archive", id: msg.id, ok: true });
}

function scheduleReconnect(error) {
  socket = null;
  setStatus({ connected: false, lastError: error || status.lastError });
  clearTimeout(reconnectTimer);
  const delay = Math.max(reconnectDelay, suppressUntil - Date.now());
  reconnectTimer = setTimeout(connect, delay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX);
}

const MAX_EN_ATTENTE = 500;
/** Messages produits alors que le socket était fermé, rejoués à la reconnexion. */
const enAttente = [];

/**
 * Un service worker MV3 peut être arrêté puis relancé à tout moment ; son
 * WebSocket est alors refermé et rouvert. Jeter les messages produits pendant
 * cet intervalle faisait perdre des réponses entières (le client HTTP restait
 * bloqué jusqu'au timeout). On les met donc en file d'attente.
 */
function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
    return;
  }
  if (enAttente.length < MAX_EN_ATTENTE) enAttente.push(payload);
  connect();
}

function flush() {
  while (enAttente.length && socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(enAttente.shift()));
  }
}

/** Onglet ChatGPT le plus pertinent : actif en priorité, sinon le plus récent.
 *  Réservé aux opérations sans conversation (lecture/pilotage d'UI). Ne
 *  participe jamais au routage d'une conversation identifiée. */
async function findChatTab() {
  const tabs = await chrome.tabs.query({
    url: ["https://chatgpt.com/*", "https://chat.openai.com/*"],
  });
  if (tabs.length === 0) return null;
  return tabs.find((t) => t.active) || tabs[tabs.length - 1];
}

/** Un onglet appartient-il à une origine ChatGPT autorisée ? Jamais une identité. */
function isAllowedChatOrigin(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && ALLOWED_CHAT_ORIGINS.includes(url.origin);
  } catch {
    return false;
  }
}

async function waitForTab(tabId) {
  for (let attempt = 0; attempt < 100; attempt++) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return tab;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("chargement de la conversation expiré");
}

function isBrowserTarget(value) {
  if (!value || typeof value !== "object") return false;
  const keys = Object.keys(value).sort();
  return (
    keys.length === 2 &&
    keys[0] === "id" &&
    keys[1] === "kind" &&
    value.kind === "temporary_chat_run" &&
    typeof value.id === "string" &&
    value.id.length > 0 &&
    value.id.length <= 255 &&
    /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value.id)
  );
}

async function reserveBrowserTarget(browserTarget) {
  const targetId = browserTarget.id;
  const created = await chrome.tabs.create({ url: TEMPORARY_CHAT_URL, active: false });
  try {
    const loaded = await waitForTab(created.id);
    if (!isAllowedChatOrigin(loaded.url)) {
      throw new BridgeRoutingError(
        "conversation_unavailable",
        "l'onglet Temporary Chat créé n'est pas sur une origine ChatGPT",
      );
    }
    await setTabAutoDiscardable(loaded.id, false);
    browserTargetRegistry.set(targetId, {
      target_id: targetId,
      tab_id: loaded.id,
      window_id: loaded.windowId,
      state: "reserved",
      last_verified_at: Date.now(),
    });
    persistBrowserTargetRegistry();
    console.log("bridge_run_phase", {
      phase: "browser_target_reserved",
      target_id: targetId,
      tab_id: loaded.id,
    });
    return loaded;
  } catch (err) {
    await chrome.tabs.remove(created.id).catch(() => {});
    if (err instanceof BridgeRoutingError) throw err;
    throw new BridgeRoutingError(
      "conversation_unavailable",
      `réservation Temporary Chat impossible : ${err.message}`,
    );
  }
}

/** Résout exclusivement le binding exact d'une cible stateless request-scoped. */
async function resolveBrowserTarget(browserTarget) {
  if (!isBrowserTarget(browserTarget)) {
    throw new BridgeRoutingError("bridge_browser_target_required", "browser_target invalide");
  }
  await browserTargetRegistryReady;
  const targetId = browserTarget.id;
  const known = browserTargetRegistry.get(targetId);
  if (known) {
    if (known.state === "recoverable" || known.recoverable === true) {
      throw new BridgeRoutingError(
        "recovery_unavailable",
        "la browser_target est conservée pour un recovery explicite",
      );
    }
    try {
      const tab = await chrome.tabs.get(known.tab_id);
      if (!isAllowedChatOrigin(tab.url)) throw new Error("origine invalide");
      return tab;
    } catch {
      // Une target connue mais perdue ne doit jamais être réparée par une
      // recherche d'URL ou par la création d'un second onglet sous la même id.
      browserTargetRegistry.delete(targetId);
      persistBrowserTargetRegistry();
      throw new BridgeRoutingError(
        "conversation_unavailable",
        "l'onglet exact de la browser_target n'existe plus",
      );
    }
  }
  const pending = browserTargetReservations.get(targetId);
  if (pending) return pending;
  const reservation = reserveBrowserTarget(browserTarget);
  browserTargetReservations.set(targetId, reservation);
  try {
    return await reservation;
  } finally {
    browserTargetReservations.delete(targetId);
  }
}

/**
 * Résout uniquement une target déjà conservée après une fin ambiguë.
 * Cette fonction ne partage volontairement pas le chemin de réservation : un
 * recovery ne peut jamais créer un nouvel onglet ou réanimer une identité.
 */
async function resolveRecoverableBrowserTarget(browserTarget, bridgeRunId) {
  if (!isBrowserTarget(browserTarget) || typeof bridgeRunId !== "string" || !bridgeRunId) {
    throw new BridgeRoutingError("recovery_unavailable", "binding de recovery invalide");
  }
  await browserTargetRegistryReady;
  const targetId = browserTarget.id;
  const known = browserTargetRegistry.get(targetId);
  if (
    !known ||
    known.target_id !== targetId ||
    known.bridge_run_id !== bridgeRunId ||
    (known.state !== "recoverable" && known.recoverable !== true)
  ) {
    throw new BridgeRoutingError(
      "recovery_unavailable",
      "aucun binding exact de recovery n'est disponible",
    );
  }
  try {
    const tab = await chrome.tabs.get(known.tab_id);
    if (!isAllowedChatOrigin(tab.url)) throw new Error("origine invalide");
    return tab;
  } catch {
    // La disparition de l'onglet est une perte de session, jamais une raison
    // de créer une autre target sous le même identifiant.
    browserTargetRegistry.delete(targetId);
    persistBrowserTargetRegistry();
    throw new BridgeRoutingError(
      "recovery_unavailable",
      "l'onglet exact de recovery n'existe plus",
    );
  }
}

/**
 * Résout l'onglet exact d'une conversation identifiée par conversation.id.
 * L'URL n'est jamais une identité : `fresh` ouvre toujours un Temporary Chat
 * neuf, `continue` ne fait jamais que retrouver l'onglet déjà lié.
 */
async function resolveConversationTab(conversation) {
  if (!conversation?.id || !["fresh", "continue"].includes(conversation.mode)) {
    throw new BridgeRoutingError("conversation_unavailable", "cible de conversation invalide");
  }
  await conversationRegistryReady;

  if (conversation.mode === "fresh") {
    const existing = conversationRegistry.get(conversation.id);
    if (existing) {
      if (existing.state !== "reserved") {
        throw new BridgeRoutingError(
          "conversation_unavailable",
          "une session live existe déjà pour cette conversation : fresh refusé",
        );
      }
      try {
        const reserved = await chrome.tabs.get(existing.tab_id);
        if (!isAllowedChatOrigin(reserved.url)) throw new Error("origine invalide");
        return reserved;
      } catch {
        conversationRegistry.delete(conversation.id);
        persistConversationRegistry();
      }
    }
    const tab = await chrome.tabs.create({ url: TEMPORARY_CHAT_URL, active: false });
    const loaded = await waitForTab(tab.id);
    if (!isAllowedChatOrigin(loaded.url)) {
      throw new BridgeRoutingError("conversation_unavailable", "l'onglet créé n'est pas sur une origine ChatGPT");
    }
    await setTabAutoDiscardable(loaded.id, false);
    conversationRegistry.set(conversation.id, {
      tab_id: loaded.id,
      window_id: loaded.windowId,
      head_turn_id: null,
      state: "reserved",
      external_locator: null,
      last_verified_at: Date.now(),
    });
    persistConversationRegistry();
    return loaded;
  }

  // mode === "continue"
  if (!conversation.expected_turn_id) {
    throw new BridgeRoutingError("conversation_unavailable", "expected_turn_id requis pour continuer");
  }
  const known = conversationRegistry.get(conversation.id);
  if (!known) {
    throw new BridgeRoutingError("conversation_unavailable", "aucune session live pour cette conversation");
  }
  if (known.head_turn_id !== conversation.expected_turn_id) {
    throw new BridgeRoutingError(
      "conversation_unavailable",
      "expected_turn_id ne correspond pas au dernier tour connu de la session live",
    );
  }
  let tab;
  try {
    tab = await chrome.tabs.get(known.tab_id);
  } catch {
    // Onglet disparu : la session live est perdue, jamais reconstruite à
    // partir d'une URL ou de l'historique ChatGPT.
    conversationRegistry.delete(conversation.id);
    persistConversationRegistry();
    throw new BridgeRoutingError("conversation_unavailable", "l'onglet de la session live n'existe plus");
  }
  if (!isAllowedChatOrigin(tab.url)) {
    conversationRegistry.delete(conversation.id);
    persistConversationRegistry();
    throw new BridgeRoutingError("conversation_unavailable", "l'onglet a quitté l'origine ChatGPT");
  }
  if (busyTabs.has(tab.id)) {
    throw new BridgeRoutingError("conversation_busy", "l'onglet de cette conversation traite déjà une requête");
  }
  return tab;
}

async function routeTab(msg) {
  if (msg.conversation) return resolveConversationTab(msg.conversation);
  if (msg.browser_target) return resolveBrowserTarget(msg.browser_target);
  // Un prompt est toujours un run et ne bénéficie d'aucun fallback vers un
  // onglet choisi par URL/activité. `findChatTab` reste réservé aux sondes UI.
  if (msg.type === "prompt") {
    throw new BridgeRoutingError(
      "bridge_browser_target_required",
      "un run stateless exige une browser_target dédiée",
    );
  }
  return findChatTab();
}

/** Envoie une seule fois au content script déjà installé par le manifest. */
async function sendToTab(tabId, msg) {
  // Une erreur de réponse après livraison est ambiguë : réinjecter puis
  // renvoyer le prompt pourrait provoquer un second clic UI. Le manifest
  // installe déjà le content script sur les onglets ChatGPT ; une nouvelle
  // tentative passe par la clé d'idempotence du même run, jamais par un
  // deuxième POST DOM implicite.
  return await chrome.tabs.sendMessage(tabId, msg);
}

async function cleanupReservationAfterDeliveryFailure(msg, route) {
  if (msg.conversation?.mode === "fresh") {
    const binding = conversationRegistry.get(msg.conversation.id);
    if (binding?.state === "submitted" && binding.bridge_run_id === msg.id) {
      await chrome.tabs.remove(binding.tab_id).catch(() => {});
      conversationRegistry.delete(msg.conversation.id);
      persistConversationRegistry();
    }
  }
  await cleanupBrowserTargetForRequest(msg.id, route);
}

async function cleanupBrowserTargetForRequest(requestId, route = requestRoutes.get(requestId)) {
  const targetId = route?.target_id;
  if (!targetId) return;
  await browserTargetRegistryReady;
  const binding = browserTargetRegistry.get(targetId);
  if (!binding) return;
  if (binding.bridge_run_id && binding.bridge_run_id !== requestId) return;
  await chrome.tabs.remove(binding.tab_id).catch(() => {});
  browserTargetRegistry.delete(targetId);
  persistBrowserTargetRegistry();
  console.log("bridge_run_phase", {
    phase: "browser_target_released",
    target_id: targetId,
    tab_id: binding.tab_id,
  });
}

async function retainBrowserTargetForRecovery(requestId, route = requestRoutes.get(requestId)) {
  const targetId = route?.target_id;
  if (!targetId) return;
  await browserTargetRegistryReady;
  const binding = browserTargetRegistry.get(targetId);
  if (!binding || binding.bridge_run_id !== requestId) return;
  browserTargetRegistry.set(targetId, {
    ...binding,
    target_id: targetId,
    state: "recoverable",
    recoverable: true,
    bridge_run_id: requestId,
    last_verified_at: Date.now(),
  });
  persistBrowserTargetRegistry();
  console.log("bridge_run_phase", {
    phase: "browser_target_recoverable",
    target_id: targetId,
    bridge_run_id: requestId,
    tab_id: binding.tab_id,
  });
}

function resultSubmissionState(msg) {
  const reported = msg.submission_state || msg.metadata?.submission_state;
  if (
    reported === "pre_submission" ||
    reported === "submission_attempted" ||
    reported === "post_submission"
  ) {
    return reported;
  }
  // An incomplete snapshot is emitted only after the confirmed send path.
  return msg.type === "incomplete" ? "post_submission" : null;
}

function isAmbiguousTargetOutcome(msg) {
  return (
    (msg.type === "incomplete" || msg.type === "error") &&
    ["submission_attempted", "post_submission"].includes(resultSubmissionState(msg))
  );
}

async function settleBrowserTargetForResult(requestId, route, msg) {
  if (isAmbiguousTargetOutcome(msg)) {
    await retainBrowserTargetForRecovery(requestId, route);
  } else {
    await cleanupBrowserTargetForRequest(requestId, route);
  }
}

async function handleBrowserTargetRetain(msg) {
  if (!isBrowserTarget(msg.browser_target) || typeof msg.run_id !== "string" || !msg.run_id) {
    return;
  }
  await retainBrowserTargetForRecovery(msg.run_id, {
    target_id: msg.browser_target.id,
  });
}

async function handleBrowserTargetRelease(msg) {
  if (!isBrowserTarget(msg.browser_target)) {
    return;
  }
  await cleanupBrowserTargetForRequest(msg.run_id || msg.id, {
    target_id: msg.browser_target.id,
  });
}

async function handlePrompt(msg) {
  await Promise.all([
    requestStatesReady,
    replayMetadataReady,
    conversationRegistryReady,
    browserTargetRegistryReady,
  ]);
  const known = requestStates.get(msg.id);
  if (known) {
    send({ type: "ack", id: msg.id, state: known, duplicate: true });
    if (known === "completed") {
      send({
        type: "done",
        id: msg.id,
        replayed: true,
        text: requestFinalOutputs.get(msg.id) || "",
        conversation: requestConversationResults.get(msg.id) || null,
        metadata: requestExtensionMetadata.get(msg.id) || null,
      });
    } else if (known === "needs_review") {
      send({
        type: "incomplete",
        id: msg.id,
        replayed: true,
        reason: "no_final_answer",
        text: "",
        conversation: requestConversationResults.get(msg.id) || null,
        metadata: requestExtensionMetadata.get(msg.id) || null,
      });
    }
    return;
  }
  // Réserver synchroniquement avant tout await ferme la course de deux paquets.
  requestStates.set(msg.id, "received");
  persistRequestStates();
  send({
    type: "ack",
    id: msg.id,
    state: "received",
    duplicate: false,
    target_id: msg.browser_target?.id || null,
  });
  let tab;
  try {
    tab = await routeTab(msg);
  } catch (err) {
    requestStates.set(msg.id, "failed");
    persistRequestStates();
    send({
      type: "error",
      id: msg.id,
      code: err.code || "bridge_server_error",
      message: err.message,
      phase: "pre_submission",
      submission_state: "pre_submission",
      target_id: msg.browser_target?.id || null,
      tab_id: null,
    });
    return;
  }
  if (!tab) {
    requestStates.set(msg.id, "failed");
    persistRequestStates();
    send({
      type: "error",
      id: msg.id,
      code: "bridge_extension_disconnected",
      message: "Aucun onglet chatgpt.com ouvert",
      phase: "pre_submission",
      submission_state: "pre_submission",
      target_id: msg.browser_target?.id || null,
      tab_id: null,
    });
    return;
  }
  if (busyTabs.has(tab.id)) {
    requestStates.set(msg.id, "failed");
    persistRequestStates();
    send({
      type: "error",
      id: msg.id,
      code: "conversation_busy",
      message: "l'onglet ChatGPT traite déjà une requête",
      phase: "pre_submission",
      submission_state: "pre_submission",
      target_id: msg.browser_target?.id || null,
      tab_id: null,
    });
    return;
  }
  inflight.set(msg.id, tab.id);
  requestRoutes.set(msg.id, {
    target_id: msg.browser_target?.id || null,
    tab_id: tab.id,
  });
  busyTabs.add(tab.id);
  // Pendant tout le run lié, Chrome ne doit pas décharger cet onglet exact :
  // un déchargement tue le content script et donc l'observation en cours.
  // C'est une protection contre le *discard*, jamais une activation.
  await setTabAutoDiscardable(tab.id, false);
  void logBoundTabState("bound_tab_state", msg.id, tab.id);
  if (msg.conversation?.mode === "fresh") {
    const binding = conversationRegistry.get(msg.conversation.id);
    if (binding?.state === "reserved") {
      conversationRegistry.set(msg.conversation.id, { ...binding, state: "submitted", bridge_run_id: msg.id });
      persistConversationRegistry();
      console.log("bridge_run_phase", { phase: "conversation_reserved", conversation_id: msg.conversation.id, tab_id: tab.id });
      console.log("bridge_run_phase", { phase: "conversation_submitted", conversation_id: msg.conversation.id, tab_id: tab.id });
    }
  }
  if (msg.browser_target) {
    const binding = browserTargetRegistry.get(msg.browser_target.id);
    if (binding) {
      browserTargetRegistry.set(msg.browser_target.id, {
        ...binding,
        state: "submitted",
        bridge_run_id: msg.id,
      });
      persistBrowserTargetRegistry();
    }
  }
  console.log("bridge_run_phase", {
    phase: "prompt_routed",
    target_id: msg.browser_target?.id || null,
    tab_id: tab.id,
  });
  send({
    type: "ack",
    id: msg.id,
    state: "running",
    duplicate: false,
    target_id: msg.browser_target?.id || null,
    tab_id: tab.id,
  });
  requestStates.set(msg.id, "running");
  persistRequestStates();
  try {
    await sendToTab(tab.id, msg);
  } catch (err) {
    const route = requestRoutes.get(msg.id);
    inflight.delete(msg.id);
    busyTabs.delete(tab.id);
    // chrome.tabs.sendMessage does not tell us whether the content script
    // received the packet before the delivery error.  Treat the handoff as
    // ambiguous: deleting the exact target here could lose a submitted
    // Temporary Chat, while retrying it could click Send twice.
    if (msg.browser_target) {
      await retainBrowserTargetForRecovery(msg.id, route);
    }
    requestStates.set(msg.id, "failed");
    persistRequestStates();
    send({
      type: "error",
      id: msg.id,
      code: "bridge_extension_disconnected",
      message: `Onglet injoignable : ${err.message}`,
      phase: "submission_confirmation",
      submission_state: "submission_attempted",
      target_id: msg.browser_target?.id || null,
      tab_id: tab.id,
    });
    requestRoutes.delete(msg.id);
  }
}

/**
 * Lecture ou pilotage de l'interface. Contrairement aux prompts, c'est un
 * échange requête/réponse : la réponse du content script est renvoyée telle
 * quelle au serveur, y compris quand elle décrit un échec — le serveur doit
 * pouvoir dire au client *pourquoi* un contrôle n'a pas pris.
 */
async function handleUiRequest(msg) {
  let tab;
  try {
    tab = await routeTab(msg);
  } catch (err) {
    send({
      type: msg.type,
      id: msg.id,
      ok: false,
      state: null,
      error: err.message,
      target_id: msg.browser_target?.id || null,
      tab_id: null,
    });
    return;
  }
  if (!tab) {
    send({
      type: msg.type,
      id: msg.id,
      ok: false,
      state: null,
      error: "Aucun onglet chatgpt.com ouvert",
      target_id: msg.browser_target?.id || null,
      tab_id: null,
    });
    return;
  }
  const route = {
    target_id: msg.browser_target?.id || null,
    tab_id: tab.id,
  };
  requestRoutes.set(msg.id, route);
  try {
    const answer = await sendToTab(tab.id, msg);
    if (!answer) throw new Error("aucune réponse du content script");
    send({ ...answer, type: msg.type, id: msg.id, ...route });
  } catch (err) {
    send({
      type: msg.type,
      id: msg.id,
      ok: false,
      state: null,
      error: `Onglet injoignable : ${err.message}`,
      ...route,
    });
  } finally {
    requestRoutes.delete(msg.id);
  }
}

// Remontée des paquets du content script vers le serveur.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "status") {
    sendResponse(status);
    return true;
  }
  if (msg?.type === "reconnect") {
    // Reconnexion manuelle : reprend le pont même si on vient d'être remplacé.
    reconnectDelay = RECONNECT_MIN;
    suppressUntil = 0;
    connect();
    sendResponse({ ok: true });
    return true;
  }
  if (
    [
      "ack",
      "heartbeat",
      "conversation_bound",
      "incomplete",
      "done",
      "error",
    ].includes(msg?.type)
  ) {
    const route = requestRoutes.get(msg.id);
    const senderTabId = sender.tab?.id ?? null;
    if (route?.target_id && senderTabId !== route.tab_id) {
      const mismatch = {
        type: "error",
        id: msg.id,
        code: "bridge_tab_mismatch",
        message: "un événement stateless provient d'un autre onglet que le prompt",
        phase: msg.phase || "generation",
        submission_state: msg.submission_state || "post_submission",
        target_id: route.target_id,
        tab_id: senderTabId,
      };
      const inflightTabId = inflight.get(msg.id);
      inflight.delete(msg.id);
      if (inflightTabId !== undefined) busyTabs.delete(inflightTabId);
      requestStates.set(msg.id, "failed");
      persistRequestStates();
      void settleBrowserTargetForResult(msg.id, route, mismatch);
      requestRoutes.delete(msg.id);
      send(mismatch);
      sendResponse({ ok: true });
      return true;
    }
    const sequence = (eventCounters.get(msg.id) || 0) + 1;
    eventCounters.set(msg.id, sequence);
    if (msg.type === "conversation_bound" && msg.conversation?.id) {
      const serverBinding = { ...msg.conversation };
      requestConversationBindings.set(msg.id, serverBinding);
      const existing = conversationRegistry.get(msg.conversation.id);
      conversationRegistry.set(msg.conversation.id, {
        tab_id: sender.tab?.id || null,
        window_id: sender.tab?.windowId || null,
        head_turn_id: existing ? existing.head_turn_id : null,
        state: "submitted",
        // Diagnostic uniquement : jamais utilisé pour router ou rouvrir un onglet.
        external_locator: msg.conversation.external_locator ?? existing?.external_locator ?? null,
        last_verified_at: Date.now(),
      });
      persistConversationRegistry();
      console.log("bridge_run_phase", { phase: "conversation_bound", conversation_id: msg.conversation.id, tab_id: sender.tab?.id || null });
    }
    if (["done", "incomplete", "error"].includes(msg.type)) {
      const tabId = inflight.get(msg.id);
      inflight.delete(msg.id);
      if (tabId !== undefined) busyTabs.delete(tabId);
      // Le run lié est fini : l'onglet redevient déchargeable, sauf s'il reste
      // la session live d'une conversation (KEEP) ou d'une target conservée.
      if (tabId !== undefined) {
        void logBoundTabState("bound_tab_settled", msg.id, tabId);
      }
      requestStates.set(
        msg.id,
        msg.type === "done"
          ? "completed"
          : msg.type === "incomplete"
            ? "needs_review"
            : "failed",
      );
      if (msg.type === "done" && msg.conversation?.id) {
        requestConversationResults.set(msg.id, msg.conversation);
        const existing = conversationRegistry.get(msg.conversation.id);
        conversationRegistry.set(msg.conversation.id, {
          tab_id: sender.tab?.id || existing?.tab_id || null,
          window_id: sender.tab?.windowId || existing?.window_id || null,
          // Le tour externe vérifié devient la nouvelle tête : c'est ce qui
          // autorise un futur CONTINUE (KEEP) sur exactement cet onglet.
          head_turn_id: msg.conversation.turn_id || existing?.head_turn_id || null,
          state: "live",
          external_locator: msg.conversation.external_locator ?? existing?.external_locator ?? null,
          last_verified_at: Date.now(),
        });
        persistConversationRegistry();
      }
      if (msg.type === "incomplete") {
        const binding = requestConversationBindings.get(msg.id);
        const live = binding && conversationRegistry.get(binding.id);
        const initialTurnId = msg.metadata?.initial_turn_id;
        if (live && typeof initialTurnId === "string" && initialTurnId) {
          conversationRegistry.set(binding.id, {
            ...live,
            state: "live",
            head_turn_id: initialTurnId,
            last_verified_at: Date.now(),
          });
          persistConversationRegistry();
        }
        if (binding) {
          const completedBinding = {
            ...binding,
            initial_assistant_turn_id: initialTurnId || binding.initial_assistant_turn_id || null,
          };
          requestConversationBindings.set(msg.id, completedBinding);
          requestConversationResults.set(msg.id, completedBinding);
        }
        if (msg.metadata) requestExtensionMetadata.set(msg.id, msg.metadata);
        persistReplayMetadata();
      }
      if (msg.type === "done" && msg.metadata) {
        requestExtensionMetadata.set(msg.id, msg.metadata);
      }
      if (msg.type === "done" && typeof msg.text === "string")
        requestFinalOutputs.set(msg.id, msg.text);
      if (msg.type === "done") persistReplayMetadata();
      if (msg.type === "error" && msg.conversation?.id && msg.submission_state === "pre_submission") {
        const binding = conversationRegistry.get(msg.conversation.id);
        if (binding?.state === "submitted" && binding.bridge_run_id === msg.id) {
          chrome.tabs.remove(binding.tab_id).catch(() => {});
          conversationRegistry.delete(msg.conversation.id);
          persistConversationRegistry();
        }
      }
      const targetRoute = route;
      persistRequestStates();
      if (targetRoute?.target_id) {
        void settleBrowserTargetForResult(msg.id, targetRoute, msg);
      }
      if (tabId !== undefined) void releaseTabAutoDiscardable(tabId);
      requestRoutes.delete(msg.id);
    }
    send({
      ...msg,
      target_id: route?.target_id ?? msg.target_id ?? null,
      tab_id: senderTabId ?? route?.tab_id ?? null,
      event_id: `${msg.id}:${sequence}`,
    });
  }
  sendResponse({ ok: true });
  return true;
});

// Nettoyage proactif : un onglet fermé ou parti hors origine ChatGPT ne doit
// jamais laisser un binding vivant pointer dans le vide.
chrome.tabs.onRemoved.addListener((tabId) => {
  let changed = false;
  for (const [id, entry] of conversationRegistry.entries()) {
    if (entry.tab_id === tabId) {
      conversationRegistry.delete(id);
      changed = true;
    }
  }
  if (changed) persistConversationRegistry();
  let browserChanged = false;
  for (const [targetId, entry] of browserTargetRegistry.entries()) {
    if (entry.tab_id === tabId) {
      browserTargetRegistry.delete(targetId);
      browserChanged = true;
    }
  }
  if (browserChanged) persistBrowserTargetRegistry();
});

/**
 * Onglet déchargé par Chrome pendant un run lié.
 *
 * Le content script est mort avec la page : plus aucun heartbeat n'arrivera et
 * la génération observée est perdue. On le dit tout de suite, de façon typée et
 * *fermée* — le run échoue en `post_submission` (donc ambigu : sa target exacte
 * est conservée pour une recovery explicite). Jamais de resoumission, jamais
 * d'onglet de remplacement, jamais d'activation pour « réveiller » l'onglet.
 */
async function failRunOnDiscardedTab(requestId, tabId) {
  const route = requestRoutes.get(requestId);
  inflight.delete(requestId);
  busyTabs.delete(tabId);
  requestStates.set(requestId, "failed");
  persistRequestStates();
  const packet = {
    type: "error",
    id: requestId,
    code: "bridge_extension_disconnected",
    message:
      "l'onglet ChatGPT lié a été déchargé par le navigateur (tab discarded)",
    phase: "generation",
    submission_state: "post_submission",
    retryable: false,
    diagnostics: { tab_state: await boundTabState(tabId) },
    target_id: route?.target_id ?? null,
    tab_id: tabId,
  };
  if (route) await settleBrowserTargetForResult(requestId, route, packet);
  requestRoutes.delete(requestId);
  send(packet);
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.discarded !== true) return;
  for (const [requestId, boundTabId] of [...inflight.entries()]) {
    if (boundTabId === tabId) void failRunOnDiscardedTab(requestId, tabId);
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  // Seul un changement d'origine invalide le binding : une navigation à
  // l'intérieur de chatgpt.com (query/path) ne le fait jamais, l'URL n'étant
  // pas l'identité de la conversation.
  if (!changeInfo.url || isAllowedChatOrigin(changeInfo.url)) return;
  let changed = false;
  for (const [id, entry] of conversationRegistry.entries()) {
    if (entry.tab_id === tabId) {
      conversationRegistry.delete(id);
      changed = true;
    }
  }
  if (changed) persistConversationRegistry();
  let browserChanged = false;
  for (const [targetId, entry] of browserTargetRegistry.entries()) {
    if (entry.tab_id === tabId) {
      browserTargetRegistry.delete(targetId);
      browserChanged = true;
    }
  }
  if (browserChanged) persistBrowserTargetRegistry();
  // Plus aucun binding : l'onglet n'a plus à être protégé du déchargement.
  if (changed || browserChanged) void setTabAutoDiscardable(tabId, true);
});

// Réveils : le service worker MV3 peut être arrêté quand il est inactif.
chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);

chrome.runtime.onInstalled.addListener(async () => {
  connect();
  // Chrome ne remplace pas le content script des onglets déjà ouverts quand on
  // recharge l'extension : sans ça, l'ancien code continue de tourner et les
  // corrections passent inaperçues. On recharge donc les onglets concernés.
  const tabs = await chrome.tabs.query({
    url: ["https://chatgpt.com/*", "https://chat.openai.com/*"],
  });
  for (const tab of tabs) {
    chrome.tabs.reload(tab.id).catch(() => {});
  }
});

connect();

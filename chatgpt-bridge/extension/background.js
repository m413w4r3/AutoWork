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
  return msg.conversation ? resolveConversationTab(msg.conversation) : findChatTab();
}

/** Envoie au content script, en l'injectant si l'onglet a été chargé avant l'extension. */
async function sendToTab(tabId, msg) {
  try {
    return await chrome.tabs.sendMessage(tabId, msg);
  } catch {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["serializer.js", "completion.js", "final-output.js", "content.js"],
    });
    return await chrome.tabs.sendMessage(tabId, msg);
  }
}

async function cleanupFreshReservationAfterDeliveryFailure(msg) {
  if (msg.conversation?.mode !== "fresh") return;
  const binding = conversationRegistry.get(msg.conversation.id);
  if (binding?.state !== "submitted" || binding.bridge_run_id !== msg.id) return;
  await chrome.tabs.remove(binding.tab_id).catch(() => {});
  conversationRegistry.delete(msg.conversation.id);
  persistConversationRegistry();
}

async function handlePrompt(msg) {
  await Promise.all([requestStatesReady, replayMetadataReady, conversationRegistryReady]);
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
  send({ type: "ack", id: msg.id, state: "received", duplicate: false });
  let tab;
  try {
    tab = await routeTab(msg);
  } catch (err) {
    requestStates.set(msg.id, "failed");
    persistRequestStates();
    send({ type: "error", id: msg.id, code: err.code || "bridge_server_error", message: err.message });
    return;
  }
  if (!tab) {
    requestStates.set(msg.id, "failed");
    persistRequestStates();
    send({ type: "error", id: msg.id, message: "Aucun onglet chatgpt.com ouvert" });
    return;
  }
  inflight.set(msg.id, tab.id);
  busyTabs.add(tab.id);
  if (msg.conversation?.mode === "fresh") {
    const binding = conversationRegistry.get(msg.conversation.id);
    if (binding?.state === "reserved") {
      conversationRegistry.set(msg.conversation.id, { ...binding, state: "submitted", bridge_run_id: msg.id });
      persistConversationRegistry();
    }
  }
  requestStates.set(msg.id, "running");
  persistRequestStates();
  try {
    await sendToTab(tab.id, msg);
  } catch (err) {
    inflight.delete(msg.id);
    busyTabs.delete(tab.id);
    await cleanupFreshReservationAfterDeliveryFailure(msg);
    requestStates.set(msg.id, "failed");
    persistRequestStates();
    send({
      type: "error",
      id: msg.id,
      code: "bridge_server_error",
      message: `Onglet injoignable : ${err.message}`,
    });
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
    send({ type: msg.type, id: msg.id, ok: false, state: null, error: err.message });
    return;
  }
  if (!tab) {
    send({ type: msg.type, id: msg.id, ok: false, state: null, error: "Aucun onglet chatgpt.com ouvert" });
    return;
  }
  try {
    const answer = await sendToTab(tab.id, msg);
    if (!answer) throw new Error("aucune réponse du content script");
    send({ ...answer, type: msg.type, id: msg.id });
  } catch (err) {
    send({ type: msg.type, id: msg.id, ok: false, state: null, error: `Onglet injoignable : ${err.message}` });
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
    }
    if (["done", "incomplete", "error"].includes(msg.type)) {
      const tabId = inflight.get(msg.id);
      inflight.delete(msg.id);
      if (tabId !== undefined) busyTabs.delete(tabId);
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
      persistRequestStates();
    }
    send({ ...msg, event_id: `${msg.id}:${sequence}` });
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

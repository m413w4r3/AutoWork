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
const conversationRegistry = new Map();
const busyTabs = new Set();
const requestConversationResults = new Map();
const requestExtensionMetadata = new Map();

const requestStatesReady = chrome.storage.local.get("bridgeRequestStates").then(({ bridgeRequestStates }) => {
  for (const [id, state] of Object.entries(bridgeRequestStates || {})) requestStates.set(id, state);
});
const conversationRegistryReady = chrome.storage.local
  .get([
    "bridgeConversationRegistry",
    "bridgeRequestConversationResults",
    "bridgeRequestExtensionMetadata",
  ])
  .then(
    ({ bridgeConversationRegistry, bridgeRequestConversationResults, bridgeRequestExtensionMetadata }) => {
    for (const [id, entry] of Object.entries(bridgeConversationRegistry || {})) {
      conversationRegistry.set(id, { ...entry, tab_id: null, window_id: null });
    }
    for (const [id, value] of Object.entries(bridgeRequestConversationResults || {})) {
      requestConversationResults.set(id, value);
    }
    for (const [id, value] of Object.entries(bridgeRequestExtensionMetadata || {})) {
      requestExtensionMetadata.set(id, value);
    }
  },
  );

function persistRequestStates() {
  const entries = [...requestStates.entries()].slice(-1000);
  chrome.storage.local.set({ bridgeRequestStates: Object.fromEntries(entries) });
}

function persistConversationRegistry() {
  const durable = Object.fromEntries(
    [...conversationRegistry.entries()].map(([id, entry]) => [id, { ...entry, tab_id: null, window_id: null }]),
  );
  chrome.storage.local.set({
    bridgeConversationRegistry: durable,
    bridgeRequestConversationResults: Object.fromEntries([...requestConversationResults.entries()].slice(-1000)),
    bridgeRequestExtensionMetadata: Object.fromEntries([...requestExtensionMetadata.entries()].slice(-1000)),
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

async function handleConversationArchive(msg) {
  await conversationRegistryReady;
  const known = conversationRegistry.get(msg.conversation_id);
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

/** Onglet ChatGPT le plus pertinent : actif en priorité, sinon le plus récent. */
async function findChatTab() {
  const tabs = await chrome.tabs.query({
    url: ["https://chatgpt.com/*", "https://chat.openai.com/*"],
  });
  if (tabs.length === 0) return null;
  return tabs.find((t) => t.active) || tabs[tabs.length - 1];
}

function validChatLocator(value) {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      ["chatgpt.com", "chat.openai.com"].includes(url.hostname) &&
      !url.username &&
      !url.password &&
      !url.hash &&
      url.pathname !== "/"
    );
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

async function resolveConversationTab(conversation) {
  if (!conversation?.id || !["fresh", "continue"].includes(conversation.mode)) {
    throw new Error("cible de conversation invalide");
  }
  await conversationRegistryReady;
  const known = conversationRegistry.get(conversation.id);
  if (conversation.mode === "fresh") {
    if (known?.tab_id) {
      const cached = await chrome.tabs.get(known.tab_id).catch(() => null);
      if (cached && !busyTabs.has(cached.id)) return cached;
    }
    const tab = await chrome.tabs.create({ url: "https://chatgpt.com/", active: false });
    const loaded = await waitForTab(tab.id);
    conversationRegistry.set(conversation.id, {
      external_locator: null,
      tab_id: loaded.id,
      window_id: loaded.windowId,
      last_verified_at: Date.now(),
    });
    persistConversationRegistry();
    return loaded;
  }
  if (!validChatLocator(conversation.external_locator)) {
    throw new Error("locator de conversation absent ou invalide");
  }
  if (known?.external_locator && known.external_locator !== conversation.external_locator) {
    throw new Error("locator incohérent avec le registre applicatif");
  }
  const tabs = await chrome.tabs.query({
    url: ["https://chatgpt.com/*", "https://chat.openai.com/*"],
  });
  let tab = tabs.find((candidate) => candidate.url === conversation.external_locator);
  if (!tab) {
    tab = await chrome.tabs.create({ url: conversation.external_locator, active: false });
  }
  if (busyTabs.has(tab.id)) throw new Error("onglet de conversation déjà occupé");
  const loaded = await waitForTab(tab.id);
  if (loaded.url !== conversation.external_locator) {
    throw new Error("la navigation ne correspond pas au locator demandé");
  }
  conversationRegistry.set(conversation.id, {
    external_locator: conversation.external_locator,
    tab_id: loaded.id,
    window_id: loaded.windowId,
    last_verified_at: Date.now(),
  });
  persistConversationRegistry();
  return loaded;
}

async function routeTab(msg) {
  return msg.conversation ? resolveConversationTab(msg.conversation) : findChatTab();
}

/** Envoie au content script, en l'injectant si l'onglet a été chargé avant l'extension. */
async function sendToTab(tabId, msg) {
  try {
    return await chrome.tabs.sendMessage(tabId, msg);
  } catch {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    return await chrome.tabs.sendMessage(tabId, msg);
  }
}

async function handlePrompt(msg) {
  await requestStatesReady;
  const known = requestStates.get(msg.id);
  if (known) {
    send({ type: "ack", id: msg.id, state: known, duplicate: true });
    if (known === "completed") {
      send({
        type: "done",
        id: msg.id,
        replayed: true,
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
    send({ type: "error", id: msg.id, code: "conversation_unavailable", message: err.message });
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
  requestStates.set(msg.id, "running");
  persistRequestStates();
  try {
    await sendToTab(tab.id, msg);
  } catch (err) {
    inflight.delete(msg.id);
    busyTabs.delete(tab.id);
    requestStates.set(msg.id, "failed");
    persistRequestStates();
    send({ type: "error", id: msg.id, message: `Onglet injoignable : ${err.message}` });
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
  if (["ack", "chunk", "done", "error"].includes(msg?.type)) {
    const sequence = (eventCounters.get(msg.id) || 0) + 1;
    eventCounters.set(msg.id, sequence);
    if (["done", "error"].includes(msg.type)) {
      const tabId = inflight.get(msg.id);
      inflight.delete(msg.id);
      if (tabId !== undefined) busyTabs.delete(tabId);
      requestStates.set(msg.id, msg.type === "done" ? "completed" : "failed");
      if (msg.type === "done" && msg.conversation?.id) {
        requestConversationResults.set(msg.id, msg.conversation);
        conversationRegistry.set(msg.conversation.id, {
          external_locator: msg.conversation.external_locator,
          tab_id: sender.tab?.id || null,
          window_id: sender.tab?.windowId || null,
          last_verified_at: Date.now(),
        });
        persistConversationRegistry();
      }
      if (msg.type === "done" && msg.metadata) {
        requestExtensionMetadata.set(msg.id, msg.metadata);
        persistConversationRegistry();
      }
      persistRequestStates();
    }
    send({ ...msg, event_id: `${msg.id}:${sequence}` });
  }
  sendResponse({ ok: true });
  return true;
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

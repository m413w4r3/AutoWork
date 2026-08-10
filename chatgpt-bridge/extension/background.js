/**
 * Service worker : détient l'unique WebSocket vers le serveur local et relaie
 * les prompts vers le content script de l'onglet chatgpt.com.
 *
 * Le WebSocket vit ici plutôt que dans le content script pour deux raisons :
 * il survit aux navigations SPA, et il n'est pas soumis à la CSP de la page.
 */

const DEFAULT_URL = "ws://127.0.0.1:8000/ws";
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

async function serverUrl() {
  const { serverUrl } = await chrome.storage.local.get("serverUrl");
  return serverUrl || DEFAULT_URL;
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
  const url = await serverUrl();
  setStatus({ url });

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
    console.log("🤖 Connecté au Mini-Bridge", url, enAttente.length ? "(file non vidée)" : "");
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
  const tab = await findChatTab();
  if (!tab) {
    send({ type: "error", id: msg.id, message: "Aucun onglet chatgpt.com ouvert" });
    return;
  }
  inflight.set(msg.id, tab.id);
  try {
    await sendToTab(tab.id, msg);
  } catch (err) {
    inflight.delete(msg.id);
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
  const tab = await findChatTab();
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
  if (["chunk", "done", "error"].includes(msg?.type)) {
    if (msg.type !== "chunk") inflight.delete(msg.id);
    send(msg);
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

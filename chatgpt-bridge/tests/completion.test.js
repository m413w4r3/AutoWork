const assert = require("node:assert/strict");

require("../extension/completion.js");

const { completionState } = globalThis.ChatGPTBridgeCompletion;

assert.deepEqual(
  completionState({
    stopVisible: false,
    streamingVisible: false,
    reasoningVisible: false,
    actionsVisible: false,
    sendVisible: true,
  }),
  { finished: null, signal: "unknown", confidence: "low" },
  "le bouton Envoyer seul ne prouve pas la fin",
);

assert.deepEqual(
  completionState({
    stopVisible: false,
    streamingVisible: false,
    reasoningVisible: false,
    actionsVisible: true,
  }),
  { finished: true, signal: "assistant_actions", confidence: "high" },
  "les actions visibles constituent un signal positif",
);

assert.equal(
  completionState({
    stopVisible: true,
    streamingVisible: false,
    reasoningVisible: false,
    actionsVisible: true,
  }).finished,
  false,
);

console.log("completion contract: ok");

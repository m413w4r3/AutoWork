/** Détection pure et testable de la fin d'un tour assistant. */
(function exposeCompletion(root) {
  function completionState(signals) {
    if (signals.stopVisible) {
      return { finished: false, signal: "stop_button", confidence: "high" };
    }
    if (signals.streamingVisible) {
      return { finished: false, signal: "streaming", confidence: "high" };
    }
    if (signals.actionsVisible) {
      return {
        finished: true,
        signal: "assistant_actions",
        confidence: "high",
      };
    }
    if (signals.reasoningVisible) {
      return { finished: false, signal: "reasoning", confidence: "high" };
    }
    return { finished: null, signal: "unknown", confidence: "low" };
  }

  root.ChatGPTBridgeCompletion = { completionState };
})(globalThis);

# Request to orchestrator — rule workbench capability

Investigation `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`; operator B; canonical state v113, SHA-256 `7fff517f192405b10e92fc3190bd25510f15dd603aea7114956ef3a8d8ded200`.

The full `available_tools` section (60 tools, all three pages read with server cursors) exposes `rule.propose`, `rule.propose_bridge`, `rule.revise`, `rule.compare`, and `rule.test` v1. It does not expose `rule.workbench`. The complete `unavailable_tools` section has 10 entries and contains no `rule.workbench` entry, so the current context supplies no exact unavailability reason.

Please identify the intended workbench capability and its exact status/reason, and expose it if the redeployment was expected to make it available. The operator can continue with the published rule tools in the meantime. No CTI indicators or sample data are included in this request.

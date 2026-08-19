# AutoWork Core

Preserve backend boundaries: API -> Application -> Domain.
Infrastructure and Integrations implement adapters/external concerns.

PostgreSQL is canonical transactional state.
LLM/model outputs, Redis queues, caches and external responses are not
canonical business state unless explicitly designed otherwise.

Treat model output and collected remote content as untrusted.

Never weaken existing CTI/security controls unless explicitly required.

Never bypass `.rooignore`.

Never run destructive Git commands or discard unrelated user changes.

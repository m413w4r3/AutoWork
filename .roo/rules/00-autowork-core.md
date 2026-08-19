# AutoWork Core

Layering: API -> Application -> Domain. Infrastructure and Integrations
provide adapters only. An inner layer never imports an outer one.

PostgreSQL holds canonical state. Model output, Redis queues, caches and
external responses are untrusted input, never canonical business state
unless the design says otherwise explicitly.

Do not weaken existing security or CTI controls.
Do not run destructive Git commands or revert changes you did not make.

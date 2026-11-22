"""Host-side workspace agent for the M24 investigation workshop.

Standalone stdlib-only service (no pydantic/httpx/watcher) that owns the host
workspace tree and exposes it to the backend over an AF_UNIX HTTP/1.1 API.
Normative design: ``docs/design/investigation-workshop/WORKSPACE-AGENT.md``.

The package opens no network listener and no outbound connection; PostgreSQL
and MinIO remain canonical, and local files are observations, never evidence.
"""

# ChatGPT Bridge operations

## Standalone Docker deployment

From `chatgpt-bridge/`:

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f chatgpt-bridge
```

Stop the service with:

```bash
docker compose down
```

The named `bridge_data` volume stores `/data/bridge-runs.sqlite3`. `docker
compose down` keeps that volume, so the SQLite registry is available after the
next `docker compose up`. Do not add `-v` when stopping if the data must be
preserved.

## Health and readiness

- `/health` means that the HTTP server is alive and responding.
- `/ready` means that the Bridge is usable: its SQLite registry and
  configuration are available and the Chrome extension is connected.

The Compose healthcheck probes `/health`; operational checks that require the
extension should use `/ready`.

## Configuration

The server only consumes Bridge variables such as `BRIDGE_HOST`,
`BRIDGE_PORT`, `BRIDGE_API_KEY`, `BRIDGE_WS_TOKEN`, `BRIDGE_RUN_DB`, and the
Bridge timeout settings. Client-side AutoWork variables are not server
configuration and do not belong in this deployment.

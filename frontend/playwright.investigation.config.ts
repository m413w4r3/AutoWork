// M24 investigation-workshop E2E uses a port derived from its worktree path;
// INVESTIGATION_E2E_PORT overrides it. The server always starts with
// --strictPort and is never reused across runs.
//
// This suite only targets the real-backend lite launcher
// (`backend/tests/workflow_emulator/m24_browser_server.py`): loopback Vite,
// loopback API, real PostgreSQL and fake providers. The operator token is
// injected by the Vite proxy (`OPERATOR_API_TOKEN`), never by the browser.
import { defineConfig, devices } from "@playwright/test";
import { createHash } from "node:crypto";
import { isIP } from "node:net";
import { resolve } from "node:path";

const PORT_RANGE_START = 40_000;
const PORT_RANGE_SIZE = 20_000;

function investigationPortForWorktree(
  worktreePath: string,
  requestedPort: string | null | undefined = process.env.INVESTIGATION_E2E_PORT,
): number {
  if (requestedPort !== undefined && requestedPort !== null) {
    const port = Number(requestedPort);
    if (
      !/^\d+$/.test(requestedPort) ||
      !Number.isInteger(port) ||
      port < 1024 ||
      port > 65_535
    ) {
      throw new Error(
        "INVESTIGATION_E2E_PORT must be an integer between 1024 and 65535",
      );
    }
    return port;
  }

  const digest = createHash("sha256").update(resolve(worktreePath)).digest();
  return PORT_RANGE_START + (digest.readUInt32BE(0) % PORT_RANGE_SIZE);
}

const port = investigationPortForWorktree(process.cwd());
const appOrigin = `http://127.0.0.1:${port}`;
const apiTarget =
  process.env.VITE_API_PROXY_TARGET ??
  `http://127.0.0.1:${process.env.WORKFLOW_BROWSER_API_PORT ?? "18768"}`;

const target = new URL(apiTarget);
const apiHostname = target.hostname.replace(/^\[|\]$/g, "");
const addressFamily = isIP(apiHostname);
const isLoopback =
  (addressFamily === 4 && apiHostname.startsWith("127.")) ||
  (addressFamily === 6 && apiHostname === "::1");
if (target.protocol !== "http:" || !isLoopback) {
  throw new Error(
    "The investigation workshop suite requires an HTTP API target on a loopback IP address.",
  );
}

export default defineConfig({
  testDir: "./e2e-real/investigation-workshop",
  outputDir:
    process.env.WORKFLOW_BROWSER_OUTPUT_DIR ??
    "./test-results/investigation-workshop",
  timeout: 600_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  expect: { timeout: 10_000 },
  use: {
    baseURL: appOrigin,
    // Bounded like `expect`: a wrong locator must fail the journey instead of
    // waiting until the whole-test timeout.
    actionTimeout: 15_000,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `corepack pnpm exec vite --host 127.0.0.1 --port ${port} --strictPort`,
    url: `${appOrigin}/editions`,
    reuseExistingServer: false,
    env: {
      VITE_API_PROXY_TARGET: apiTarget,
      ...(process.env.OPERATOR_API_TOKEN === undefined
        ? {}
        : { OPERATOR_API_TOKEN: process.env.OPERATOR_API_TOKEN }),
      ...(process.env.CHOKIDAR_USEPOLLING === undefined
        ? {}
        : { CHOKIDAR_USEPOLLING: process.env.CHOKIDAR_USEPOLLING }),
    },
  },
});

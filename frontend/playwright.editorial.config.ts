// Editorial E2E uses a port derived from its worktree path; EDITORIAL_E2E_PORT overrides it.
// The server always starts with --strictPort and is never reused across runs.
// Playwright baseURL and the Vite readiness URL share this computed origin.
import { defineConfig, devices } from "@playwright/test";
import { isIP } from "node:net";

import { editorialPortForWorktree } from "./e2e/editorial/editorialOrigin";

// real-backend-lite expects an operator-started PostgreSQL test backend with a fake provider.
// The operator must attest no live provider keys are available to that backend.
// Set EDITORIAL_REAL_LITE_ATTESTED_FAKE_PROVIDER=1 only after checking those conditions.
// This config starts Vite only; backend bootstrapping belongs to a later ticket.
const realBackendLite =
  process.env.EDITORIAL_PLAYWRIGHT_MODE === "real-backend-lite";
const port = editorialPortForWorktree(process.cwd());
const appOrigin = `http://127.0.0.1:${port}`;
const apiTarget =
  process.env.VITE_API_PROXY_TARGET ??
  `http://127.0.0.1:${process.env.WORKFLOW_BROWSER_API_PORT ?? "18765"}`;

if (realBackendLite) {
  const target = new URL(apiTarget);
  const apiHostname = target.hostname.replace(/^\[|\]$/g, "");
  const addressFamily = isIP(apiHostname);
  const isLoopback =
    (addressFamily === 4 && apiHostname.startsWith("127.")) ||
    (addressFamily === 6 && apiHostname === "::1");
  if (target.protocol !== "http:" || !isLoopback) {
    throw new Error(
      "Editorial real-backend-lite requires an HTTP API target on a loopback IP address.",
    );
  }
}

export default defineConfig({
  testDir: realBackendLite ? "./e2e-real/editorial" : "./e2e/editorial",
  outputDir: "./test-results/editorial",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  expect: { timeout: 5_000 },
  use: {
    baseURL: appOrigin,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `corepack pnpm exec vite --host 127.0.0.1 --port ${port} --strictPort`,
    url: `${appOrigin}/editions`,
    reuseExistingServer: false,
    ...(realBackendLite ? { env: { VITE_API_PROXY_TARGET: apiTarget } } : {}),
  },
});

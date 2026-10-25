import { defineConfig, devices } from "@playwright/test";

const apiTarget =
  process.env.VITE_API_PROXY_TARGET ??
  `http://127.0.0.1:${process.env.WORKFLOW_BROWSER_API_PORT ?? "18765"}`;

export default defineConfig({
  testDir: "./e2e-real",
  timeout: 300_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  outputDir:
    process.env.WORKFLOW_BROWSER_OUTPUT_DIR ??
    "../var/workflow-results/browser/playwright-results",
  reporter: [
    ["list"],
    [
      "html",
      {
        outputFolder: "../var/workflow-results/browser/playwright-report",
        open: "never",
      },
    ],
  ],
  expect: { timeout: 10_000 },
  use: {
    baseURL: "http://127.0.0.1:4174",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command:
      "corepack pnpm exec vite --host 127.0.0.1 --port 4174 --strictPort",
    url: "http://127.0.0.1:4174/editions",
    reuseExistingServer: false,
    env: { VITE_API_PROXY_TARGET: apiTarget },
  },
});

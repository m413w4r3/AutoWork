import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 10_000,
  fullyParallel: true,
  workers: process.env.CI ? 2 : undefined,
  expect: {
    timeout: process.env.CI ? 10_000 : 5_000,
  },
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    screenshot: "only-on-failure",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "corepack pnpm exec vite --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173/editions",
    reuseExistingServer: !process.env.CI,
  },
});

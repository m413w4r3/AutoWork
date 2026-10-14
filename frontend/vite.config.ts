import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

import { createApiProxyOptions } from "./src/viteProxyConfig";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    fs: { allow: [".."] },
    proxy: {
      "/api": {
        ...createApiProxyOptions(
          process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000",
          process.env.OPERATOR_API_TOKEN,
        ),
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
    exclude: [...configDefaults.exclude, "e2e/**", "e2e-real/**"],
  },
});

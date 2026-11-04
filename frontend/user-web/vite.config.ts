import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const hmrHost = process.env.VITE_HMR_HOST;
const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || "0") || undefined;

function requireLegacySsoBuildConfig(command: "build" | "serve"): void {
  if (command !== "build") return;
  // OAuth2 connection details are private Backend configuration. This build
  // does not use the legacy AES login URL or application name.
  if (process.env.VITE_AUTH_MODE === "oauth2") return;
  const missing = ["VITE_LEGACY_SSO_LOGIN_BASE_URL", "VITE_LEGACY_SSO_APP_NAME"]
    .filter((name) => !process.env[name]?.trim());
  if (missing.length > 0) {
    throw new Error(`Enterprise SSO build configuration missing: ${missing.join(", ")}`);
  }
}

export default defineConfig(({ command }) => {
  requireLegacySsoBuildConfig(command);
  return {
    plugins: [react()],
    server: {
      port: 5173,
      hmr: hmrHost ? {
        protocol: process.env.VITE_HMR_PROTOCOL === "wss" ? "wss" : "ws",
        host: hmrHost,
        clientPort: hmrClientPort,
        path: process.env.VITE_HMR_PATH || undefined
      } : undefined,
      proxy: {
        "/api": {
          target: process.env.VITE_BACKEND_PROXY_TARGET || "http://127.0.0.1:8000",
          changeOrigin: true
        }
      }
    },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      globals: true,
      testTimeout: 15_000,
      exclude: ["e2e/**", "node_modules/**", "dist/**"]
    }
  };
});

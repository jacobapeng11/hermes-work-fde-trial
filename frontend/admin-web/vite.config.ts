import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const hmrHost = process.env.VITE_HMR_HOST;
const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || "0") || undefined;

function requireLegacySsoBuildConfig(command: "build" | "serve"): void {
  if (command !== "build") return;
  // Backend /auth/config selects the enterprise protocol at login time.
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
    base: "/admin/",
    plugins: [react()],
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      testTimeout: 15_000
    },
    server: {
      port: 5174,
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
    }
  };
});

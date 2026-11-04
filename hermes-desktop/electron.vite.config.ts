import { defineConfig } from "electron-vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { loadEnv } from "vite";
import { serverOrigin } from "./src/main/desktop-routing";

// Keep the public Enterprise SSO login settings in the server's existing .env. Vite
// only exposes VITE_* variables to the renderer, so backend secrets are never
// bundled into the desktop client.
const backendEnvDir = resolve(__dirname, "../backend");
// Only this non-sensitive value is exposed to the main-process bundle.
const oauth2Origins = process.env.OAUTH2_DESKTOP_ALLOWED_ORIGINS
  || loadEnv("", backendEnvDir, "OAUTH2_DESKTOP_ALLOWED_ORIGINS").OAUTH2_DESKTOP_ALLOWED_ORIGINS || "[]";
const configuredServerOrigin = process.env.COWORK_SERVER_ORIGIN?.trim();

export default defineConfig({
  main: {
    define: {
      "process.env.OAUTH2_DESKTOP_ALLOWED_ORIGINS": JSON.stringify(oauth2Origins),
      ...(configuredServerOrigin ? {
        "process.env.COWORK_SERVER_ORIGIN": JSON.stringify(serverOrigin(configuredServerOrigin)),
      } : {}),
    },
    build: {
      // Playwright resolves its own package.json and driver files at runtime.
      // Keep it external so those paths remain valid inside the packaged app.
      rollupOptions: {
        input: resolve(__dirname, "src/main/index.ts"),
        external: ["playwright-core", "chromium-bidi"],
      },
    },
  },
  preload: {
    build: {
      rollupOptions: {
        input: resolve(__dirname, "src/preload/index.ts"),
        output: { format: "cjs", entryFileNames: "index.js" },
      },
    },
  },
  renderer: {
    root: resolve(__dirname),
    envDir: backendEnvDir,
    plugins: [react()],
    base: "/",
    server: { hmr: { host: "127.0.0.1" } },
    build: { rollupOptions: { input: resolve(__dirname, "index.html") } }
  }
});

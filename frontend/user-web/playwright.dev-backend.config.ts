import { defineConfig, devices } from "@playwright/test";

const backendPort = Number(process.env.PLAYWRIGHT_BACKEND_PORT || "18011");
const webPort = Number(process.env.PLAYWRIGHT_WEB_PORT || "15177");
const canaryUserId = Number(process.env.PLAYWRIGHT_CANARY_USER_ID || "990095");
const backendURL = `http://127.0.0.1:${backendPort}`;
const baseURL = `http://127.0.0.1:${webPort}`;

if (!Number.isSafeInteger(canaryUserId) || canaryUserId <= 0) {
  throw new Error("PLAYWRIGHT_CANARY_USER_ID must be a positive integer");
}

export default defineConfig({
  testDir: "./e2e/integration",
  outputDir: "./test-results/playwright-dev-backend",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: [["line"]],
  expect: { timeout: 10_000 },
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    colorScheme: "light",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: `env APP_ENV=development HERMES_AUTH_MODE=dev HERMES_ADMIN_AUTH_MODE=inherit DEV_PRINCIPAL_USER_ID=${canaryUserId} BACKEND_PORT=${backendPort} PORT_BACKEND=${backendPort} .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port ${backendPort} --timeout-graceful-shutdown 10`,
      cwd: "../../backend",
      url: `${backendURL}/api/v1/health`,
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command: `VITE_AUTH_MODE=dev VITE_BACKEND_PROXY_TARGET=${backendURL} npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort`,
      url: `${baseURL}/user/chat`,
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});

import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.PLAYWRIGHT_WEB_PORT || "15176");
const baseURL = `http://127.0.0.1:${port}`;
const useDevServer = process.env.PLAYWRIGHT_USE_DEV_SERVER === "1";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "integration/**",
  outputDir: "./test-results/playwright",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ["line"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
  ],
  expect: {
    timeout: 8_000,
    toHaveScreenshot: {
      animations: "disabled",
      caret: "hide",
    },
  },
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    colorScheme: "light",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: {
    command: useDevServer
      ? `VITE_AUTH_MODE=enterprise_idp npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`
      : `VITE_AUTH_MODE=enterprise_idp npm run build && npm run preview -- --host 127.0.0.1 --port ${port} --strictPort`,
    url: `${baseURL}/user/chat`,
    timeout: 180_000,
    reuseExistingServer: false,
  },
});

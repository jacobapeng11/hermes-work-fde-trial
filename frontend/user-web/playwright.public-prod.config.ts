import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.PLAYWRIGHT_PUBLIC_PROD_URL;
if (!baseURL) {
  throw new Error("PLAYWRIGHT_PUBLIC_PROD_URL must be supplied by the deployment environment");
}

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "integration/**",
  outputDir: "./test-results/playwright-public-prod",
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  reporter: [["line"]],
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
    launchOptions: { args: ["--no-proxy-server"] },
    colorScheme: "light",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
});

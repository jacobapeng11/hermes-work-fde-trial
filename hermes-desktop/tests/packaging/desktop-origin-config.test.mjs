import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("packaging entries derive the desktop server origin from PUBLIC_WEB_BASE", () => {
  const windowsScript = readFileSync("scripts/build-win-from-backend-env.ps1", "utf8");
  const macScript = readFileSync("scripts/build-mac.sh", "utf8");

  assert.match(windowsScript, /PUBLIC_WEB_BASE/);
  assert.match(macScript, /PUBLIC_WEB_BASE/);
  assert.doesNotMatch(windowsScript, /agent-hermes-(uat|v2)\.legacy_sso\.demo\.com/);
  assert.doesNotMatch(macScript, /agent-hermes-(uat|v2)\.legacy_sso\.demo\.com/);
});

test("electron main replaces the build-time server origin with a validated literal", () => {
  const viteConfig = readFileSync("electron.vite.config.ts", "utf8");

  assert.match(viteConfig, /serverOrigin/);
  assert.match(viteConfig, /"process\.env\.COWORK_SERVER_ORIGIN"/);
});

test("Enterprise SSO navigation and cookie checks follow the selected service origin", () => {
  const mainWindow = readFileSync("src/main/index.ts", "utf8");

  assert.doesNotMatch(mainWindow, /legacy_ssoServiceOrigin/);
  assert.doesNotMatch(mainWindow, /agent-hermes-(uat|v2)\.legacy_sso\.demo\.com/);
  assert.match(mainWindow, /target\.origin === origin/);
  assert.match(mainWindow, /normalized === originHost/);
});

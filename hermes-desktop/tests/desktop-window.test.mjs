import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { defaultWindowBounds } from "../src/main/desktop-window.ts";

test("default normal-window bounds center inside the primary work area", () => {
  assert.deepEqual(defaultWindowBounds({ x: 0, y: 0, width: 1920, height: 1040 }), {
    x: 240,
    y: 60,
    width: 1440,
    height: 920,
  });
});

test("default normal-window bounds follow an offset work area", () => {
  assert.deepEqual(defaultWindowBounds({ x: 100, y: 40, width: 1920, height: 1040 }), {
    x: 340,
    y: 100,
    width: 1440,
    height: 920,
  });
});

test("default normal-window bounds shrink on small work areas but keep minimums", () => {
  assert.deepEqual(defaultWindowBounds({ x: 0, y: 0, width: 1280, height: 720 }), {
    x: 0,
    y: 0,
    width: 1280,
    height: 720,
  });
  assert.deepEqual(defaultWindowBounds({ x: 0, y: 0, width: 900, height: 600 }), {
    x: 0,
    y: 0,
    width: 1000,
    height: 680,
  });
});

test("the desktop header is draggable while interactive descendants remain clickable", () => {
  const styles = readFileSync("src/styles.css", "utf8");
  const appHeader = styles.slice(styles.indexOf(".appHeader {"), styles.indexOf(".headerIdentity"));

  assert.match(appHeader, /-webkit-app-region:\s*drag/);
  assert.match(styles, /\.appHeader button[\s\S]*-webkit-app-region:\s*no-drag/);
  assert.match(styles, /\.appHeader a[\s\S]*-webkit-app-region:\s*no-drag/);
  assert.match(styles, /\.appHeader input[\s\S]*-webkit-app-region:\s*no-drag/);
  assert.match(styles, /\.appHeader select[\s\S]*-webkit-app-region:\s*no-drag/);
  assert.match(styles, /\.brand \{[\s\S]*-webkit-app-region:\s*drag/);
});

test("module pages keep a desktop title strip separated from page content", () => {
  const styles = readFileSync("src/styles.css", "utf8");
  const desktopTitleStrip = styles.slice(styles.indexOf("/* Desktop keeps a compact native title strip"));
  const gridRule = desktopTitleStrip.slice(0, desktopTitleStrip.indexOf("/* 微调"));

  assert.match(gridRule, /html:is\(\s*\[data-desktop-platform="win32"\],\s*\[data-desktop-platform="linux"\],\s*\[data-desktop-platform="darwin"\]/);
  assert.match(gridRule, /\.mainArea:has\(\s*> \.appHeader:is\(/);
  assert.match(gridRule, /grid-template-rows:\s*40px\s+minmax\(0,\s*1fr\)/);
  assert.match(gridRule, /\.appHeader:is\(/);
  assert.match(gridRule, /height:\s*40px/);
  assert.match(gridRule, /overflow:\s*visible/);
  for (const view of ["skills", "knowledge", "app-center", "experts", "schedules", "mcps", "im"]) {
    assert.ok(gridRule.includes(`[data-view="${view}"]`));
  }
});

test("macOS integrates native traffic lights without covering sidebar content", () => {
  const mainWindow = readFileSync("src/main/index.ts", "utf8");
  const desktopLayer = readFileSync("src/components/DesktopLayer.tsx", "utf8");
  const styles = readFileSync("src/styles.css", "utf8");
  const macBranch = mainWindow.match(/process\.platform === "darwin"\s*\?\s*\{[\s\S]*?\}/);

  assert.ok(macBranch);
  assert.match(macBranch[0], /titleBarStyle:\s*"hiddenInset"/);
  assert.match(macBranch[0], /trafficLightPosition:\s*\{ x: 20, y: 20 \}/);
  assert.match(desktopLayer, /dataset\.desktopPlatform\s*=\s*window\.coworkDesktop\?\.platform \?\? "web"/);
  assert.match(styles, /html\[data-desktop-platform="darwin"\] \.primarySidebar \{[\s\S]*?padding-top:\s*54px/);
  assert.match(styles, /html\[data-desktop-platform="darwin"\] \.authLoadingSidebar \{[\s\S]*?padding-top:\s*54px/);
});

test("window maximize happens before renderer load and does not fight OS snapping", () => {
  const mainWindow = readFileSync("src/main/index.ts", "utf8");
  const maximizeIndex = mainWindow.indexOf("window.maximize()");
  const loadIndex = mainWindow.indexOf('window.loadURL(');

  assert.notEqual(maximizeIndex, -1);
  assert.notEqual(loadIndex, -1);
  assert.ok(maximizeIndex < loadIndex);
  assert.doesNotMatch(mainWindow, /window\.on\("unmaximize"/);
  assert.doesNotMatch(mainWindow, /window\.setBounds\(\{\s*width:\s*1440/);
});

test("native window controls keep a compact hit target", () => {
  const mainWindow = readFileSync("src/main/index.ts", "utf8");
  const overlay = mainWindow.match(/titleBarOverlay:\s*\{[^}]+\}/);

  assert.ok(overlay);
  assert.match(overlay[0], /height:\s*40/);
  assert.doesNotMatch(overlay[0], /height:\s*64/);
});

import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const synchronizedDirectories = [
  "src/components/skills",
  "src/components/knowledge",
  "src/components/mcp",
  "src/components/app_center",
  "src/components/experts",
  "src/components/welcome",
  "src/components/schedules",
  "src/components/profile",
];

const synchronizedFiles = [
  "src/main.tsx",
  "src/pages/chatRunProjection.ts",
  "src/pages/chatRunProjection.test.ts",
  "src/pages/initial_runtime_allocation/README.md",
  "src/pages/initial_runtime_allocation/UserChatPage.test.tsx",
  "src/components/WelcomeTaskPanel.tsx",
  "src/components/WelcomeTaskPanel.test.tsx",
  "src/components/WelcomeTaskPanel.css",
  "src/components/AvatarPicker.tsx",
  "src/pages/ApplicationAgentWorkspace.test.tsx",
  "src/pages/ApplicationAgentConfigurationPanel.tsx",
  "src/components/SessionLifecycle.test.tsx",
  "src/theme/antd-module.tsx",
  "src/theme/antd-theme.ts",
  "src/theme/motion.css",
  "src/theme/platform-theme.tsx",
];

function assertFileMatches(relativePath) {
  const userWeb = readFileSync(`../frontend/user-web/${relativePath}`, "utf8");
  const desktop = readFileSync(relativePath, "utf8");
  assert.equal(
    desktop,
    userWeb,
    `Desktop module is out of sync with User Web: ${relativePath}`,
  );
}

test("desktop Skill, Knowledge, and App Center modules stay synchronized with User Web", () => {
  for (const directory of synchronizedDirectories) {
    const files = readdirSync(directory, { recursive: true })
      .filter((name) => name.endsWith(".tsx") || name.endsWith(".ts") || name.endsWith(".css"))
      // AppCenterPage has a deliberate desktop one-time browser-launch adapter.
      .filter((name) => join(directory, String(name)).replaceAll("\\", "/") !== "src/components/app_center/AppCenterPage.tsx")
      .map(String);

    assert.ok(files.length > 0, `No synchronized files found in ${directory}`);
    for (const name of files) assertFileMatches(join(directory, name));
  }
});

test("desktop shared module entries stay synchronized with User Web", () => {
  for (const relativePath of synchronizedFiles) assertFileMatches(relativePath);
});

test("desktop App Center retains the one-time browser launch adapter", () => {
  const source = readFileSync(
    "src/components/app_center/AppCenterPage.tsx",
    "utf8",
  );
  assert.match(source, /createAppBrowserLaunch\(app\.id\)/);
  assert.match(source, /desktop\.openBrowser\(launch\.launchUrl\)/);
  assert.match(source, /window\.open\(app\.accessUrl/);
});

test("desktop workspace keeps its Electron adapters and User Web runtime features", () => {
  const source = readFileSync("src/pages/AgentWorkspace.tsx", "utf8");
  assert.match(source, /getDesktopHtmlPreviewBridge/);
  assert.match(
    source,
    /\{view === "chat" && !isApplication \? <DesktopWorkspaceBar \/> : null\}/,
  );
  assert.match(source, /Boolean\(window\.coworkDesktop\)/);
  assert.match(source, /runtimeStartupContext/);
  assert.match(source, /startCreateAppFromTemplate/);
  assert.match(source, /chat\.transport\.reconnecting/);
});

test("desktop module title strip stays focused on task and header content", () => {
  const source = readFileSync("src/pages/AgentWorkspace.tsx", "utf8");
  assert.match(
    source,
    /view !== "experts" && view !== "schedules"/,
    "Experts and schedules must use their module headers without a duplicated title",
  );
});

test("desktop schedules layout comes only from the synchronized module CSS", () => {
  const globalStyles = readFileSync("src/styles.css", "utf8");
  const moduleStyles = readFileSync(
    "src/components/schedules/schedules.css",
    "utf8",
  );

  for (const selector of [
    "schedulesPage",
    "schedulesHeader",
    "scheduledTaskList",
    "scheduledTaskDialogBackdrop",
    "scheduleFields",
  ]) {
    assert.doesNotMatch(
      globalStyles,
      new RegExp(`\\.${selector}(?:\\s|,|:)`),
      `.${selector} must not be defined in desktop global styles`,
    );
  }

  assert.match(moduleStyles, /\.schedulesPanel\s*\{[^}]*width:\s*100%/s);
  assert.match(moduleStyles, /\.scheduledTaskList\s*\{/);
});

test("desktop workspace keeps the User Web header layout", () => {
  const styles = readFileSync("src/styles.css", "utf8");
  assert.match(styles, /\.chatTitle\s*\{[^}]*display:\s*flex/s);
  assert.match(styles, /\.appShell\[data-theme="dark"\]\s+\.chatTitle strong/);
  for (const view of ["experts", "schedules", "mcps", "im"]) {
    assert.match(
      styles,
      new RegExp(`\\.appHeader\\[data-view="${view}"\\]`),
      `Missing workspace-header collapse rule for ${view}`,
    );
  }
});

test("desktop retains the App Center error metadata and capability adapters", () => {
  const apiClient = readFileSync("src/api/client.ts", "utf8");
  assert.match(apiClient, /responseErrorMetadata\(errorBody\)\.code/);
  assert.match(apiClient, /createAppBrowserLaunch: \(appId: number\)/);
  assert.match(apiClient, /resolveLocalCapability\(requestId/);
  assert.match(apiClient, /"X-Hermes-Client-Type": "desktop"/);
});

test("desktop shell uses the synchronized platform theme provider", () => {
  assert.match(readFileSync("src/main.tsx", "utf8"), /PlatformThemeProvider/);
  for (const relativePath of [
    "src/components/AuthGate.tsx",
    "src/components/SessionLifecycle.tsx",
    "src/pages/AgentWorkspace.tsx",
    "src/pages/ApplicationAgentPage.tsx",
    "src/pages/WeComBindPage.tsx",
  ]) {
    assert.match(
      readFileSync(relativePath, "utf8"),
      /usePlatformTheme/,
      `${relativePath} must consume the synchronized platform theme context`,
    );
  }
});

test("desktop module dependencies include the User Web antd base", () => {
  const packageJson = JSON.parse(readFileSync("package.json", "utf8"));
  assert.ok(packageJson.dependencies.antd);
  assert.ok(packageJson.dependencies["@ant-design/icons"]);
});

test("desktop retains User Web theme base and Inter fonts", () => {
  const tokens = readFileSync("src/theme/tokens.css", "utf8");
  assert.match(tokens, /font-family: "Inter"/);
  assert.match(tokens, /\.desktopLayer\[data-theme="dark"\]/);
  assert.match(tokens, /\.desktopLayer\[data-theme="light"\]/);

  for (const name of [
    "Inter-Regular.woff2",
    "Inter-Medium.woff2",
    "Inter-SemiBold.woff2",
    "Inter-Bold.woff2",
  ]) {
    assert.ok(existsSync(`public/fonts/${name}`), `Missing font: ${name}`);
  }
});

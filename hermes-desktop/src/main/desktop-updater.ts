import { app, BrowserWindow, net, session, type Session } from "electron";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { autoUpdater, type UpdateInfo, type ProgressInfo } from "electron-updater";

export type DesktopUpdateStatus = {
  state: "disabled" | "checking" | "available" | "not-available" | "downloading" | "downloaded" | "error";
  currentVersion: string;
  version?: string;
  releaseDate?: string;
  percent?: number;
  transferred?: number;
  total?: number;
  error?: string;
};

type UpdateConfig = { updateUrl?: unknown };
type Log = (message: string, error?: unknown) => Promise<void> | void;

let status: DesktopUpdateStatus = { state: "disabled", currentVersion: "0.0.0" };
let configured = false;
let getWindow: (() => BrowserWindow | null) | null = null;
let getSession: (() => Session | null) | null = null;
let feedUrl = "";
let log: Log = () => undefined;

function publish(next: Omit<DesktopUpdateStatus, "currentVersion">): void {
  status = { currentVersion: app.getVersion(), ...next };
  const window = getWindow?.();
  if (window && !window.isDestroyed()) window.webContents.send("desktop.update-status", status);
}

function updateFromInfo(state: DesktopUpdateStatus["state"], info: UpdateInfo): void {
  publish({
    state,
    version: info.version,
    releaseDate: info.releaseDate,
  });
}

function updateFromProgress(info: ProgressInfo): void {
  publish({
    state: "downloading",
    version: status.version,
    releaseDate: status.releaseDate,
    percent: info.percent,
    transferred: info.transferred,
    total: info.total,
  });
}

async function readUpdateUrl(): Promise<string | null> {
  if (!app.isPackaged || (process.platform !== "win32" && process.platform !== "darwin")) return null;
  try {
    const raw = await readFile(join(process.resourcesPath, "update-config.json"), "utf8");
    const config = JSON.parse(raw) as UpdateConfig;
    if (typeof config.updateUrl !== "string" || !config.updateUrl.trim()) return null;
    const url = new URL(config.updateUrl.trim());
    if (url.protocol !== "https:" && url.hostname !== "localhost" && url.hostname !== "127.0.0.1") return null;
    return url.toString().replace(/\/$/, "");
  } catch (error) {
    await log("desktop updater config unavailable", error);
    return null;
  }
}

export async function initializeDesktopUpdater(
  windowGetter: () => BrowserWindow | null,
  logger: Log,
  sessionGetter?: () => Session | null,
): Promise<void> {
  getWindow = windowGetter;
  getSession = sessionGetter ?? null;
  log = logger;
  status = { state: "disabled", currentVersion: app.getVersion() };
  const updateUrl = await readUpdateUrl();
  if (!updateUrl) return;
  feedUrl = updateUrl;

  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.on("checking-for-update", () => publish({ state: "checking" }));
  autoUpdater.on("update-available", (info) => updateFromInfo("available", info));
  autoUpdater.on("update-not-available", (info) => updateFromInfo("not-available", info));
  autoUpdater.on("download-progress", updateFromProgress);
  autoUpdater.on("update-downloaded", (info) => updateFromInfo("downloaded", info));
  autoUpdater.on("error", (error) => {
    // A failed background check is expected when the update directory is
    // temporarily unavailable. Keep this out of the product UI.
    publish({ state: "not-available", version: status.version });
    void log("desktop updater error", error);
  });
  autoUpdater.setFeedURL({ provider: "generic", url: updateUrl });
  bridgeUpdaterSessionToPlatform();
  configured = true;
  await log(`desktop updater configured source=${updateUrl}`);
}

export function currentDesktopUpdateStatus(): DesktopUpdateStatus {
  return { ...status, currentVersion: app.getVersion() };
}

// electron-updater issues every request (feed check, full download,
// differential Range downloads) through its own partition
// ("electron-updater", cache:false). The update directory sits behind the
// Enterprise SSO gateway, whose session cookie only lives in the RUNNING platform
// partition's in-memory cookie jar — a fresh session just gets the gateway
// login page and the check dies with "no valid semver version". Register a
// protocol bridge on the updater partition: its feed requests are re-issued
// through the platform session, inheriting the exact authenticated network
// context (cookies + auto gateway dance) of every other API call. Streams
// and Range headers pass through untouched; requestHeaders stay empty so
// they cannot override the session cookie jar.
function bridgeUpdaterSessionToPlatform(): void {
  const platformSession = getSession?.();
  if (!platformSession || !feedUrl) return;
  const feedOrigin = new URL(feedUrl).origin;
  const updaterSession = session.fromPartition("electron-updater", { cache: false });
  updaterSession.protocol.handle("https", async (request) => {
    try {
      const url = new URL(request.url);
      if (url.origin !== feedOrigin) return net.fetch(request);
      await log(`desktop updater bridge ${request.method} ${url.pathname}`);
      return platformSession.fetch(request, { bypassCustomProtocolHandlers: true });
    } catch (error) {
      await log("desktop updater bridge failed", error);
      return net.fetch(request);
    }
  });
}

export async function checkDesktopForUpdates(interactive = false): Promise<DesktopUpdateStatus> {
  if (!configured) return currentDesktopUpdateStatus();
  try {
    await autoUpdater.checkForUpdates();
  } catch (error) {
    publish({
      state: interactive ? "error" : "not-available",
      version: status.version,
      error: interactive ? error instanceof Error ? error.message : String(error) : undefined,
    });
    await log("desktop update check failed", error);
  }
  return currentDesktopUpdateStatus();
}

export async function downloadDesktopUpdate(): Promise<DesktopUpdateStatus> {
  if (!configured || status.state !== "available") return currentDesktopUpdateStatus();
  try {
    await autoUpdater.downloadUpdate();
  } catch (error) {
    publish({ state: "error", version: status.version, error: error instanceof Error ? error.message : String(error) });
    await log("desktop update download failed", error);
  }
  return currentDesktopUpdateStatus();
}

export function installDesktopUpdate(): DesktopUpdateStatus {
  if (configured && status.state === "downloaded") {
    // Silent install: an upgrade must not walk the user through the initial
    // setup wizard again (per-user choice, install path) — NSIS detects the
    // existing installation and overwrites it in place. forceRunAfter brings
    // the app back up once the installer finishes.
    autoUpdater.quitAndInstall(true, true);
  }
  return currentDesktopUpdateStatus();
}

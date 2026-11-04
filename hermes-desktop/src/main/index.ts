import { app, BrowserWindow, dialog, ipcMain, Menu, net, powerMonitor, screen, session, shell } from "electron";
import { homedir, tmpdir } from "node:os";
import { randomBytes, randomUUID } from "node:crypto";
import { allowedOAuth2Navigation, isOAuth2Login, parseOAuth2Origins } from "./oauth2-navigation";
import { allowedGatewayNavigation, isPlatformLoginConfiguration, recoverGatewayConfiguration, transferableGatewayCookie } from "./gateway-auth";
import { join } from "node:path";
import { appendFile, mkdir, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { serverOrigin, isLocalDocument, localAssetPath } from "./desktop-routing";
import { defaultWindowBounds } from "./desktop-window";
import { openBrowser } from "./browser-window";
import { BROWSER_CAPABILITY_HANDLERS, closeAllBrowserSessions } from "./browser-automation";
import { runWorkspaceCommand } from "./command-runner";
import { appendCapabilityAudit } from "./capability-audit";
import { getRuntimeStatus } from "./runtime-status";
import { validateCapabilityRequest, type LocalCapabilityApproval, type LocalCapabilityRequest, type LocalCapabilityResult } from "./capability-protocol";
import {
  classifyLocalPath,
  createLocalDirectory,
  createWorkspaceDirectory,
  listLocalDirectory,
  listWorkspaceDirectory,
  readLocalText,
  readWorkspaceText,
  resolveLocalPath,
  resolveWorkspacePath,
  writeLocalText,
  writeWorkspaceText,
} from "./workspace-files";
import { findExternalCommandPath } from "./command-runner";
import {
  checkDesktopForUpdates,
  currentDesktopUpdateStatus,
  downloadDesktopUpdate,
  initializeDesktopUpdater,
  installDesktopUpdate,
} from "./desktop-updater";

const origin = serverOrigin(process.env.COWORK_SERVER_ORIGIN);
const originHost = new URL(origin).hostname;
const partition = "persist:cowork-platform";
const legacy_ssoOrigin = "https://login.example.com";
const legacy_ssoVerificationOrigin = "https://gateway.example.com:6440";
const legacy_ssoIdentityOrigin = "https://identity.example.com";
let workspaceRoot: string | null = null;
// Where the folder picker opens next time; persisted so users do not restart
// from the Downloads folder on every click.
let lastPickerDir: string | null = null;
let mainWindow: BrowserWindow | null = null;
let legacy_ssoWindow: BrowserWindow | null = null;
let oauth2Window: BrowserWindow | null = null;
let gatewayAuthentication: Promise<void> | null = null;
const pendingCapabilities = new Map<string, LocalCapabilityRequest>();
const recentHtmlWriteTargets = new Map<string, number>();
const htmlPreviewTempFiles = new Set<string>();
const RECENT_HTML_PREVIEW_WINDOW_MS = 15 * 60 * 1000;
const workspaceStateFile = () => join(app.getPath("userData"), "desktop-workspace.json");
const logFile = () => join(app.getPath("userData"), "logs", "main.log");
const desktopIconPath = () => join(
  app.isPackaged ? process.resourcesPath : join(__dirname, "..", ".."),
  "resources/icon.png"
);

async function log(message: string, error?: unknown): Promise<void> {
  const detail = error instanceof Error ? `${error.stack ?? error.message}` : error == null ? "" : ` ${String(error)}`;
  const line = `[${new Date().toISOString()}] ${redactLogText(message)}${redactLogText(detail)}\n`;
  try {
    await mkdir(join(app.getPath("userData"), "logs"), { recursive: true });
    await appendFile(logFile(), line, "utf8");
  } catch {
    // Logging must never prevent the desktop client from starting.
  }
}

function contentTypeFor(filePath: string): string {
  const lower = filePath.toLowerCase();
  if (lower.endsWith(".html")) return "text/html; charset=utf-8";
  if (lower.endsWith(".js")) return "text/javascript; charset=utf-8";
  if (lower.endsWith(".css")) return "text/css; charset=utf-8";
  if (lower.endsWith(".json")) return "application/json; charset=utf-8";
  if (lower.endsWith(".svg")) return "image/svg+xml";
  if (lower.endsWith(".woff2")) return "font/woff2";
  if (lower.endsWith(".woff")) return "font/woff";
  if (lower.endsWith(".png")) return "image/png";
  if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
  if (lower.endsWith(".webp")) return "image/webp";
  return "application/octet-stream";
}

function redactUrl(rawUrl: string): string {
  try {
    const url = new URL(rawUrl);
    return `${url.origin}${url.pathname}`;
  } catch {
    return "[invalid-url]";
  }
}

function redactLogText(value: string): string {
  return value
    .replace(/https?:\/\/[^\s"'<>]+/g, (candidate) => redactUrl(candidate))
    .replace(/([?&](?:token|code|state|desktop_flow|access_token|refresh_token|t|sidTicket|ticket|csrfToken|csrf)=)[^&\s)]+/gi, "$1[redacted]")
    .replace(/\b(sidTicket|csrfToken|csrf)\s*[:=]\s*[^\s,;]+/gi, "$1:[redacted]");
}

async function localResponse(filePath: string): Promise<Response> {
  const body = await readFile(filePath);
  return new Response(body, {
    status: 200,
    headers: {
      "content-type": contentTypeFor(filePath),
      "cache-control": "no-cache",
    },
  });
}

async function loadWorkspaceState(): Promise<void> {
  try {
    const parsed = JSON.parse(await readFile(workspaceStateFile(), "utf8")) as { path?: unknown; lastPickerDir?: unknown };
    if (typeof parsed.path === "string") workspaceRoot = await realpath(parsed.path);
    if (typeof parsed.lastPickerDir === "string") lastPickerDir = parsed.lastPickerDir;
  } catch {
    workspaceRoot = null;
  }
}

async function saveWorkspaceState(): Promise<void> {
  await writeFile(workspaceStateFile(), JSON.stringify({ path: workspaceRoot, lastPickerDir }, null, 2), "utf8");
}

function assertDesktopRenderer(event: Electron.IpcMainInvokeEvent): void {
  try {
    if (!event.senderFrame || new URL(event.senderFrame.url).origin !== origin) throw new Error("桌面请求来源无效");
  } catch {
    throw new Error("桌面请求来源无效");
  }
}

function requireWorkspace(): string {
  if (!workspaceRoot) throw new Error("请先选择工作文件夹");
  return workspaceRoot;
}

async function approvalFor(request: LocalCapabilityRequest, workspace: string): Promise<LocalCapabilityApproval | undefined> {
  if (request.capability.startsWith("workspace.")) {
    const rawPath = request.args.path ?? "";
    const scope = await classifyLocalPath(workspace, rawPath);
    if (scope === "external") return {
      scope: "external",
      target: String(rawPath).slice(0, 600),
      question: `Agent 请求访问所选工作文件夹之外的路径：${String(rawPath).slice(0, 300)}`,
    };
  }
  if (request.capability === "command.run") {
    const target = await findExternalCommandPath(request.args.command, workspace);
    if (target) return {
      scope: "external",
      target: target.slice(0, 600),
      question: `命令引用了工作文件夹之外的路径：${target.slice(0, 300)}`,
    };
  }
  if (request.capability === "browser.upload") {
    // Uploading sends a local file to a remote site; unlike workspace reads,
    // every upload — even inside the workspace — requires an explicit card.
    const rawPath = typeof request.args.path === "string" ? request.args.path : "";
    return {
      scope: "external",
      target: rawPath.slice(0, 600),
      question: `Agent 请求把本地文件上传到网页：${rawPath.slice(0, 300)}`,
    };
  }
  if (request.capability.startsWith("browser.") && request.args.confirm === true) return {
    scope: "external",
    target: String(request.args.url ?? request.args.sessionId ?? request.capability).slice(0, 600),
    question: `Agent 请求在本机浏览器中执行操作：${request.capability}。确认后继续吗？`,
  };
  return undefined;
}

function auditEntryFor(request: LocalCapabilityRequest): { capability: "workspace" | "browser" | "command"; operation: string; target?: string } {
  if (request.capability.startsWith("workspace.")) {
    return { capability: "workspace", operation: request.capability.slice("workspace.".length), target: String(request.args.path ?? "").slice(0, 300) };
  }
  if (request.capability.startsWith("browser.")) {
    return { capability: "browser", operation: request.capability.slice("browser.".length), target: String(request.args.url ?? request.args.path ?? request.args.sessionId ?? "").slice(0, 300) };
  }
  return { capability: "command", operation: "run", target: String(request.args.command ?? "").slice(0, 300) };
}

function commandOutcomeDetail(value: unknown): string | undefined {
  if (value && typeof value === "object" && "exitCode" in value) {
    const command = value as { exitCode?: unknown; timedOut?: unknown };
    return command.timedOut === true ? "timeout" : `exit:${command.exitCode}`;
  }
  return undefined;
}

function isHtmlPreviewPath(value: string): boolean {
  return /\.(?:html|htm)$/i.test(value.trim());
}

function pruneRecentHtmlWriteTargets(): void {
  const now = Date.now();
  for (const [target, writtenAt] of recentHtmlWriteTargets) {
    if (now - writtenAt > RECENT_HTML_PREVIEW_WINDOW_MS) recentHtmlWriteTargets.delete(target);
  }
}

// Terminal-state audit for remote agent capability calls.  Browser commands
// previously produced no audit trail at all, which made offline diagnosis of
// automation failures impossible; every request now records exactly one line.
function auditCapabilityOutcome(request: LocalCapabilityRequest, outcome: "completed" | "failed" | "pending_approval" | "denied", detail?: string): void {
  void appendCapabilityAudit(app.getPath("userData"), { ...auditEntryFor(request), outcome, detail: detail?.slice(0, 300) });
}

function logCapabilityOutcome(request: LocalCapabilityRequest, outcome: string, detail?: string): void {
  void log(`capability ${request.capability} id=${request.requestId} outcome=${outcome}${detail ? ` detail=${detail.slice(0, 300)}` : ""}`);
}

async function executeCapability(request: LocalCapabilityRequest): Promise<LocalCapabilityResult> {
  const workspace = request.capability.startsWith("workspace.") || request.capability === "command.run" ? requireWorkspace() : null;
  if (request.capability === "workspace.list") {
    // The agent sees only relative paths; root lets it report the full local
    // location of deliverables (equivalent to `echo %CD%`, no extra exposure).
    const entries = await listLocalDirectory(workspace!, request.args.path ?? "");
    return { requestId: request.requestId, ok: true, value: { root: workspace, entries } };
  }
  if (request.capability === "workspace.read") {
    return { requestId: request.requestId, ok: true, value: await readLocalText(workspace!, request.args.path) };
  }
  if (request.capability === "workspace.write") {
    const { target } = await resolveLocalPath(workspace!, request.args.path, { missing: true });
    await writeLocalText(workspace!, request.args.path, request.args.content);
    if (isHtmlPreviewPath(target)) recentHtmlWriteTargets.set(target, Date.now());
    return { requestId: request.requestId, ok: true, value: true };
  }
  if (request.capability === "workspace.mkdir") {
    await createLocalDirectory(workspace!, request.args.path);
    return { requestId: request.requestId, ok: true, value: true };
  }
  if (request.capability === "workspace.trash") {
    const { target } = await resolveLocalPath(workspace!, request.args.path);
    await shell.trashItem(target);
    return { requestId: request.requestId, ok: true, value: true };
  }
  if (request.capability === "browser.open") {
    const value = await openBrowser(request.args.url);
    return { requestId: request.requestId, ok: value, value };
  }
  if (request.capability.startsWith("browser.")) {
    const handler = BROWSER_CAPABILITY_HANDLERS[request.capability];
    if (!handler) throw new Error("本地能力不受支持");
    return { requestId: request.requestId, ok: true, value: await handler(request.args, { workspace: workspaceRoot }) };
  }
  if (request.capability !== "command.run") throw new Error("本地能力不受支持");
  const value = await runWorkspaceCommand(request.args.command, workspace!);
  return { requestId: request.requestId, ok: value.exitCode === 0 && !value.timedOut, value };
}

async function registerDesktopTransport(): Promise<void> {
  const desktopSession = session.fromPartition(partition);
  const rendererRoot = join(__dirname, "../renderer");
  const devOrigin = process.env.ELECTRON_RENDERER_URL;
  await log(`register transport origin=${origin} rendererRoot=${rendererRoot} devOrigin=${devOrigin ?? ""}`);
  desktopSession.webRequest.onCompleted({ urls: [`${origin}/*`] }, (details) => {
    const path = new URL(details.url).pathname;
    if (path.startsWith("/api/v1/") || path.startsWith("/runtime-edge/")) {
      const contentType = details.responseHeaders?.["content-type"]?.[0]
        ?? details.responseHeaders?.["Content-Type"]?.[0]
        ?? "";
      void log(`remote response method=${details.method} status=${details.statusCode} type=${contentType} url=${redactUrl(details.url)}`);
    }
  });
  desktopSession.webRequest.onErrorOccurred({ urls: [`${origin}/*`] }, (details) => {
    const path = new URL(details.url).pathname;
    if (path.startsWith("/api/v1/") || path.startsWith("/runtime-edge/")) {
      void log(`remote request error method=${details.method} error=${details.error} url=${redactUrl(details.url)}`);
    }
  });
  desktopSession.protocol.handle("https", async (request) => {
    try {
      const url = new URL(request.url);
      if (url.origin === origin && request.method === "GET") {
        if (isLocalDocument(url.pathname)) {
          if (devOrigin) return net.fetch(devOrigin);
          const filePath = join(rendererRoot, "index.html");
          await log(`serve local document ${url.pathname} -> ${filePath}`);
          return localResponse(filePath);
        }
        if (devOrigin && (url.pathname.startsWith("/src/") || url.pathname.startsWith("/@")
          || url.pathname.startsWith("/node_modules/") || url.pathname.startsWith("/fonts/"))) {
          return net.fetch(new URL(url.pathname + url.search, devOrigin).toString());
        }
        const asset = localAssetPath(url.pathname, rendererRoot);
        if (asset && !devOrigin) {
          await log(`serve local asset ${url.pathname} -> ${asset}`);
          return localResponse(asset);
        }
      }
      const isBackendRequest = url.origin === origin
        && (url.pathname.startsWith("/api/v1/") || url.pathname.startsWith("/runtime-edge/"));
      let response = await desktopSession.fetch(request, { bypassCustomProtocolHandlers: true });
      response = await recoverGatewayConfiguration(request.url, request.method, origin, response,
        ensureGatewayAuthentication,
        () => desktopSession.fetch(request, { bypassCustomProtocolHandlers: true }));
      if (isBackendRequest) {
        const contentType = response.headers.get("content-type") ?? "";
        void log(`proxy response method=${request.method} status=${response.status} type=${contentType} url=${redactUrl(request.url)} final=${redactUrl(response.url)}`);
        if (contentType.toLowerCase().includes("text/html")) {
          void response.clone().text().then((body) => log(`backend HTML response preview=${body.slice(0, 180).replace(/\s+/g, " ")}`));
        }
      }
      return response;
    } catch (error) {
      await log(`protocol request failed ${redactUrl(request.url)}`, error);
      return new Response("Desktop resource failed", { status: 500 });
    }
  });
}

function ensureGatewayAuthentication(): Promise<void> {
  if (gatewayAuthentication) return gatewayAuthentication;
  gatewayAuthentication = authenticateGateway().finally(() => { gatewayAuthentication = null; });
  return gatewayAuthentication;
}

async function authenticateGateway(): Promise<void> {
  const gatewaySession = session.fromPartition("persist:cowork-gateway");
  const loginWindow = new BrowserWindow({
    width: 1120, height: 760, title: "Enterprise SSO 网关认证", show: false,
    backgroundColor: "#f7f8fb", icon: desktopIconPath(),
    webPreferences: { session: gatewaySession, nodeIntegration: false, contextIsolation: true,
      sandbox: true, webSecurity: true, allowRunningInsecureContent: false, webviewTag: false },
  });
  loginWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  await log("Enterprise SSO gateway admission required before platform login");
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    let checking = false;
    const finish = (error?: Error): void => {
      if (settled) return;
      settled = true;
      clearInterval(poll);
      clearTimeout(timeout);
      clearTimeout(reveal);
      if (!loginWindow.isDestroyed()) loginWindow.close();
      if (error) reject(error); else resolve();
    };
    const check = async (): Promise<void> => {
      if (settled || checking) return;
      checking = true;
      try {
        const response = await gatewaySession.fetch(`${origin}/api/v1/auth/config`, {
          bypassCustomProtocolHandlers: true, cache: "no-store", signal: AbortSignal.timeout(8_000),
        });
        if (!await isPlatformLoginConfiguration(response) || settled) return;
        const cookies = await gatewaySession.cookies.get({ url: `${origin}/api/v1/auth/config` });
        for (const target of [session.fromPartition(partition), session.fromPartition("persist:cowork-oauth2")]) {
          for (const cookie of cookies.filter((value) => transferableGatewayCookie(value.name))) {
            if (settled) return;
            await target.cookies.set({ url: `${origin}${cookie.path || "/"}`, name: cookie.name, value: cookie.value,
              ...(cookie.hostOnly ? {} : { domain: cookie.domain }), path: cookie.path, secure: cookie.secure,
              httpOnly: cookie.httpOnly, sameSite: cookie.sameSite, expirationDate: cookie.expirationDate });
          }
        }
        await log("Enterprise SSO gateway admission verified; resume platform login");
        finish();
      } catch {
        // A transient gateway/network error does not prove admission. Keep
        // waiting within this bounded login attempt; never log its contents.
      } finally { checking = false; }
    };
    const poll = setInterval(() => { void check(); }, 1_500);
    const timeout = setTimeout(() => finish(new Error("网关认证超时，请重新登录")), 180_000);
    const reveal = setTimeout(() => { if (!settled && !loginWindow.isDestroyed()) loginWindow.show(); }, 3_500);
    loginWindow.on("closed", () => finish(new Error("已取消网关认证")));
    const guard = (event: Electron.Event, target: string): void => {
      // Do not let the remote web UI start a platform OAuth2 transaction:
      // the local renderer will start it with its own desktop flow nonce.
      if (isOAuth2Login(target, origin) || !allowedGatewayNavigation(target, origin)) event.preventDefault();
      void check();
    };
    loginWindow.webContents.on("will-navigate", guard);
    loginWindow.webContents.on("will-redirect", guard);
    loginWindow.webContents.on("did-finish-load", () => { void check(); });
    // Load the real root in an unintercepted session, preserving redirect
    // bases and relative gateway assets. Never render an API HTML response.
    void loginWindow.loadURL(`${origin}/`).catch(() => {
      if (!settled && !loginWindow.isDestroyed()) loginWindow.show();
    });
    void check();
  });
}

// The Enterprise SSO gateway proxies external origins into subdomains such as
// open-work-weixin-qq-com-s.gateway.example.com — the WeCom OAuth QR page the
// login portal embeds. Besides the fixed portal origins, allow any HTTPS
// *.gateway.example.com subdomain the gateway produces inside the login
// window; otherwise the QR navigation is blocked and the panel stays blank
// no matter how many times the user clicks refresh.
function isLegacySsoProxyHost(hostname: string): boolean {
  return hostname.toLowerCase().endsWith(".gateway.example.com");
}

function isLegacySsoUrl(rawUrl: string): boolean {
  try {
    const target = new URL(rawUrl);
    return target.origin === legacy_ssoOrigin
      || target.origin === legacy_ssoVerificationOrigin
      || target.origin === legacy_ssoIdentityOrigin
      || target.origin === origin
      || (target.protocol === "https:" && isLegacySsoProxyHost(target.hostname));
  } catch { return false; }
}

function isDesktopCallback(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    return url.origin === origin && url.searchParams.has("token");
  } catch {
    return false;
  }
}

async function openOAuth2Login(rawUrl: string): Promise<void> {
  try {
    const allowedOrigins = parseOAuth2Origins(process.env.OAUTH2_DESKTOP_ALLOWED_ORIGINS || "[]");
    if (!allowedOrigins.length || !isOAuth2Login(rawUrl, origin)) throw new Error("OAuth2 桌面登录配置缺失");
    if (oauth2Window && !oauth2Window.isDestroyed()) { oauth2Window.focus(); return; }
    const flow = randomBytes(32).toString("base64url");
    const loginUrl = new URL(rawUrl);
    loginUrl.searchParams.set("desktop_flow", flow);
    const loginSession = session.fromPartition("persist:cowork-oauth2");
    const loginWindow = new BrowserWindow({
      width: 1120, height: 760, title: "OAuth2 登录", icon: desktopIconPath(),
      webPreferences: { session: loginSession, nodeIntegration: false, contextIsolation: true,
        sandbox: true, webSecurity: true, webviewTag: false },
    });
    oauth2Window = loginWindow;
    let finishing = false;
    loginWindow.on("closed", () => { if (oauth2Window === loginWindow) oauth2Window = null; });
    const guard = (event: Electron.Event, target: string): void => {
      if (isOAuth2Login(target, origin) && new URL(target).searchParams.get("desktop_flow") !== flow) {
        event.preventDefault();
        const retried = new URL(target);
        retried.searchParams.set("desktop_flow", flow);
        void loginWindow.loadURL(retried.toString());
        return;
      }
      if (!allowedOAuth2Navigation(target, origin, allowedOrigins)) event.preventDefault();
    };
    loginWindow.webContents.on("will-navigate", guard);
    loginWindow.webContents.on("will-redirect", guard);
    loginWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    loginWindow.webContents.on("did-navigate", (_event, raw) => {
      const url = new URL(raw);
      if (url.origin !== origin || url.pathname !== "/auth/desktop-complete" || finishing) return;
      finishing = true;
      void (async () => {
        // Receipt is one-use and bound to this flow and the newly issued session.
        const completion = await loginSession.fetch(`${origin}/api/v1/auth/oauth2/complete`, {
          method: "POST", headers: { Origin: origin, "X-Hermes-Desktop-Flow": flow },
        });
        if (!completion.ok) throw new Error("OAuth2 登录完成验证失败");
        if (loginWindow.isDestroyed()) return;
        const targetSession = session.fromPartition(partition);
        const cookies = await loginSession.cookies.get({ url: `${origin}/api/v1/auth/refresh` });
        if (!cookies.some((value) => value.name === "hermes_platform_session")) throw new Error("OAuth2 平台会话缺失");
        // Copy only cookies applicable to the platform API; provider-only cookies stay isolated.
        for (const value of cookies) {
          if (loginWindow.isDestroyed()) return;
          await targetSession.cookies.set({ url: `${origin}${value.path || "/"}`, name: value.name,
            value: value.value, ...(value.hostOnly ? {} : { domain: value.domain }), path: value.path, secure: value.secure,
            httpOnly: value.httpOnly, sameSite: value.sameSite, expirationDate: value.expirationDate });
        }
        const verified = await targetSession.fetch(`${origin}/api/v1/auth/refresh`, {
          method: "POST", headers: { Origin: origin, "X-Hermes-Requested-With": "desktop" },
        });
        if (!verified.ok) throw new Error("OAuth2 会话恢复失败");
        if (!mainWindow || mainWindow.isDestroyed()) return;
        // The renderer discards its old JWT and obtains the new one via refresh.
        await mainWindow.loadURL(`${origin}/user/chat?auth_refresh=1`);
        if (!loginWindow.isDestroyed()) loginWindow.close();
      })().catch(() => {
        void log("OAuth2 completion failed");
        if (!loginWindow.isDestroyed()) void loginWindow.loadURL(`${origin}/auth/error?reason=oauth2_transaction_invalid`);
      });
    });
    await loginWindow.loadURL(loginUrl.toString());
  } catch {
    await log("OAuth2 login could not start");
    await dialog.showMessageBox({ type: "error", title: "OAuth2 登录", message: "无法启动 OAuth2 登录，请检查桌面白名单和部署配置。" });
  }
}

function isLegacySsoCookieDomain(domain: string | undefined): boolean {
  const normalized = (domain ?? "").replace(/^\./, "").toLowerCase();
  return normalized === "login.example.com"
    || normalized === "gateway.example.com"
    || normalized === "identity.example.com"
    || normalized === originHost
    || isLegacySsoProxyHost(normalized);
}

async function copyLegacySsoCookiesToPlatform(): Promise<void> {
  const source = session.fromPartition("persist:cowork-legacy_sso");
  const target = session.fromPartition(partition);
  const cookies = await source.cookies.get({});
  for (const cookie of cookies) {
    if (!isLegacySsoCookieDomain(cookie.domain)) continue;
    const host = (cookie.domain ?? "").replace(/^\./, "");
    try {
      await target.cookies.set({
        url: `https://${host}${cookie.path || "/"}`,
        name: cookie.name,
        value: cookie.value,
        domain: cookie.domain,
        path: cookie.path,
        secure: cookie.secure,
        httpOnly: cookie.httpOnly,
        expirationDate: cookie.expirationDate,
        sameSite: cookie.sameSite,
      });
    } catch (error) {
      await log(`Enterprise SSO cookie transfer failed domain=${host} name=${cookie.name}`, error);
    }
  }
  await log(`Enterprise SSO cookies transferred count=${cookies.filter((cookie) => isLegacySsoCookieDomain(cookie.domain)).length}`);
}

async function finishLegacySsoLogin(callbackUrl: string): Promise<void> {
  // The callback first passes through the Enterprise SSO gateway.  Copy the resulting
  // gateway cookies before switching to the local renderer, whose API calls
  // use the platform session partition.
  await copyLegacySsoCookiesToPlatform();
  const loginWindow = legacy_ssoWindow;
  legacy_ssoWindow = null;
  if (loginWindow && !loginWindow.isDestroyed()) loginWindow.close();
  if (mainWindow && !mainWindow.isDestroyed()) {
    void mainWindow.loadURL(callbackUrl).then(() => log("Enterprise SSO callback returned to desktop"), (error) => log("Enterprise SSO callback load failed", error));
  }
}

// Silent-first Enterprise SSO authentication. The login window starts hidden and the
// gateway gets a grace period to complete the automatic redirect with the
// persisted Enterprise SSO cookies: while they are valid the callback arrives inside
// the grace period and the window is never shown at all. When interaction is
// required the window is shown after the grace period with the login page
// already loaded (it has been loading since t=0), which also removes the old
// blank-white flash during slow gateway round-trips.
const LEGACY_SSO_SILENT_GRACE_MS = 3_500;

function openLegacySsoLogin(loginUrl: string): void {
  if (!isLegacySsoUrl(loginUrl)) {
    void log(`blocked unexpected Enterprise SSO URL ${redactUrl(loginUrl)}`);
    return;
  }
  if (legacy_ssoWindow && !legacy_ssoWindow.isDestroyed()) {
    legacy_ssoWindow.show();
    legacy_ssoWindow.focus();
    void legacy_ssoWindow.loadURL(loginUrl);
    return;
  }
  const loginWindow = new BrowserWindow({
    width: 1120,
    height: 760,
    minWidth: 860,
    minHeight: 620,
    title: "Enterprise SSO 认证",
    show: false,
    backgroundColor: "#f7f8fb",
    icon: desktopIconPath(),
    webPreferences: {
      partition: "persist:cowork-legacy_sso",
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
    },
  });
  legacy_ssoWindow = loginWindow;
  const startedAt = Date.now();
  let silentGrace: NodeJS.Timeout | null = setTimeout(() => {
    silentGrace = null;
    if (!loginWindow.isDestroyed()) loginWindow.show();
  }, LEGACY_SSO_SILENT_GRACE_MS);
  const clearGrace = (): void => {
    if (silentGrace) { clearTimeout(silentGrace); silentGrace = null; }
  };
  loginWindow.on("closed", () => {
    clearGrace();
    if (legacy_ssoWindow === loginWindow) legacy_ssoWindow = null;
  });
  let pendingCallbackUrl: string | null = null;
  const handleNavigation = (_event: Electron.Event, targetUrl: string): void => {
    if (isDesktopCallback(targetUrl)) pendingCallbackUrl = targetUrl;
    if (!isLegacySsoUrl(targetUrl)) {
      _event.preventDefault();
      void log(`blocked Enterprise SSO navigation ${redactUrl(targetUrl)}`);
    }
  };
  loginWindow.webContents.on("will-navigate", handleNavigation);
  loginWindow.webContents.on("will-redirect", handleNavigation);
  loginWindow.webContents.on("did-navigate", (_event, targetUrl) => {
    let reachedServiceOrigin = false;
    try { reachedServiceOrigin = new URL(targetUrl).origin === origin; } catch { /* ignore malformed navigation */ }
    const callbackUrl = isDesktopCallback(targetUrl)
      ? targetUrl
      : reachedServiceOrigin ? pendingCallbackUrl : null;
    if (callbackUrl) {
      pendingCallbackUrl = null;
      if (silentGrace) void log(`Enterprise SSO silent login completed in ${Date.now() - startedAt}ms`);
      clearGrace();
      void finishLegacySsoLogin(callbackUrl).catch((error) => log("Enterprise SSO callback finalization failed", error));
    }
  });
  loginWindow.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    void log(`Enterprise SSO console level=${level} ${sourceId}:${line} ${redactLogText(message)}`);
  });
  void loginWindow.loadURL(loginUrl).catch((error) => {
    void log("Enterprise SSO window load failed", error);
    clearGrace();
    if (!loginWindow.isDestroyed()) loginWindow.show();
  });
}

function createWindow(): void {
  const { workArea } = screen.getPrimaryDisplay();
  const normalBounds = defaultWindowBounds(workArea);
  const window = new BrowserWindow({
    // Keep browser-like normal-state bounds for restore and OS snapping. The
    // hidden window is maximized below before the renderer starts, so the
    // first paint still uses the final maximized size and cannot regress to
    // the auth-phase right-side strip fixed in 6fe9f5cc.
    ...normalBounds,
    minWidth: 1000,
    minHeight: 680,
    resizable: true,
    maximizable: true,
    // Hidden until the first frame is painted (see reveal below). Showing at
    // creation left the native window-control overlay alone on an unpainted
    // page for the 1-2s auth phase, which read as a stray dark box instead of
    // three clean buttons. The first frame comes from the local asar
    // interception, so the wait is near-instant. The background matches the
    // light product default (and the html background in styles.css) in case
    // paint and show ever race.
    show: false,
    backgroundColor: "#f7f8fb",
    // The taskbar/alt-tab name stays; the frame itself goes borderless on
    // Windows so no icon/title strip remains — the overlay keeps the native
    // min/max/close controls floating over .appHeader, which becomes the drag
    // region (see -webkit-app-region in styles.css). macOS keeps native
    // traffic lights while integrating them into the application shell.
    title: "Hermes Work",
    // Taskbar icon on Windows/Linux; the file ships in extraResources so the
    // packaged build can reach it without a source-tree path. In dev the
    // main process runs from out/main/, so we climb up to the resources/
    // folder at the project root instead of relying on __dirname.
    icon: desktopIconPath(),
    ...(process.platform === "darwin"
      ? {
          titleBarStyle: "hiddenInset" as const,
          trafficLightPosition: { x: 20, y: 20 },
        }
      : process.platform === "win32" || process.platform === "linux"
      ? {
          titleBarStyle: "hidden" as const,
          titleBarOverlay: { color: "#00000000", symbolColor: "#9aa0a6", height: 40 },
        }
      : {}),
    webPreferences: {
      preload: join(__dirname, "../preload/index.js"),
      partition,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
  });
  mainWindow = window;
  // Maximize while the window is still hidden and before loadURL(): the
  // renderer therefore paints first at the maximized size, while Windows keeps
  // normalBounds as the restore bounds. Do not reset bounds on unmaximize;
  // that would overwrite Win+Left/Right and drag-to-snap layouts.
  window.maximize();
  // Reveal exactly once: the painted first frame wins, and the timer only
  // guards against a stalled load leaving no window at all.
  let revealed = false;
  const reveal = (): void => {
    if (revealed || window.isDestroyed()) return;
    revealed = true;
    window.show();
  };
  window.once("ready-to-show", reveal);
  setTimeout(reveal, 10_000).unref?.();
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, raw) => {
    try {
      const target = new URL(raw);
      if (isOAuth2Login(raw, origin)) {
        event.preventDefault();
        void openOAuth2Login(raw);
        return;
      }
      if (target.origin === origin) return;
      if (target.origin === legacy_ssoOrigin) {
        event.preventDefault();
        openLegacySsoLogin(raw);
        return;
      }
      event.preventDefault();
    } catch {
      event.preventDefault();
    }
  });
  window.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    void log(`did-fail-load code=${errorCode} description=${errorDescription} url=${validatedURL} mainFrame=${isMainFrame}`);
  });
  window.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    void log(`console level=${level} ${sourceId}:${line} ${redactLogText(message)}`);
  });
  window.webContents.on("render-process-gone", (_event, details) => {
    void log(`render-process-gone reason=${details.reason} exitCode=${details.exitCode}`);
  });
  void window.loadURL(`${origin}/user/chat`).then(() => log("window loaded"), (error) => log("window load failed", error));
}

function registerCapabilityHandlers(): void {
  ipcMain.handle("desktop.select-folder", async (event) => {
    assertDesktopRenderer(event);
    const result = await dialog.showOpenDialog({
      defaultPath: lastPickerDir ?? homedir(),
      properties: ["openDirectory", "createDirectory"],
    });
    if (result.canceled || !result.filePaths[0]) return null;
    workspaceRoot = await realpath(result.filePaths[0]);
    lastPickerDir = workspaceRoot;
    await saveWorkspaceState();
    return workspaceRoot;
  });
  ipcMain.handle("desktop.get-workspace", (event) => {
    assertDesktopRenderer(event);
    return { path: workspaceRoot };
  });
  ipcMain.handle("desktop.runtime-status", async (event) => {
    assertDesktopRenderer(event);
    return getRuntimeStatus();
  });
  ipcMain.handle("desktop.update-status", (event) => {
    assertDesktopRenderer(event);
    return currentDesktopUpdateStatus();
  });
  ipcMain.handle("desktop.update-check", async (event, interactive: unknown) => {
    assertDesktopRenderer(event);
    // Default stays interactive (manual retry shows errors); the automatic
    // post-login check passes false so an empty/unreachable update directory
    // stays silent instead of nagging with an error card.
    return checkDesktopForUpdates(interactive !== false);
  });
  ipcMain.handle("desktop.update-download", async (event) => {
    assertDesktopRenderer(event);
    return downloadDesktopUpdate();
  });
  ipcMain.handle("desktop.update-install", (event) => {
    assertDesktopRenderer(event);
    return installDesktopUpdate();
  });
  ipcMain.handle("desktop.request-capability", async (event, rawRequest: unknown): Promise<LocalCapabilityResult> => {
    assertDesktopRenderer(event);
    const request = validateCapabilityRequest(rawRequest);
    try {
      const needsWorkspace = request.capability.startsWith("workspace.") || request.capability === "command.run";
      const workspace = needsWorkspace ? requireWorkspace() : workspaceRoot;
      const approval = workspace || request.capability.startsWith("browser.") ? await approvalFor(request, workspace ?? "") : undefined;
      if (approval) {
        if (pendingCapabilities.has(request.requestId)) throw new Error("该本地请求已等待确认");
        pendingCapabilities.set(request.requestId, request);
        setTimeout(() => pendingCapabilities.delete(request.requestId), 10 * 60 * 1000).unref?.();
        auditCapabilityOutcome(request, "pending_approval", approval.target);
        logCapabilityOutcome(request, "pending-approval", approval.target);
        return { requestId: request.requestId, ok: false, requiresApproval: true, approval };
      }
      const result = await executeCapability(request);
      const detail = result.error ?? commandOutcomeDetail(result.value);
      auditCapabilityOutcome(request, result.ok ? "completed" : "failed", detail);
      logCapabilityOutcome(request, result.ok ? "completed" : "failed", detail);
      return result;
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "本地能力执行失败";
      auditCapabilityOutcome(request, "failed", message);
      logCapabilityOutcome(request, "failed", message);
      return { requestId: request.requestId, ok: false, error: message };
    }
  });
  ipcMain.handle("desktop.resolve-capability", async (event, raw: unknown): Promise<LocalCapabilityResult> => {
    assertDesktopRenderer(event);
    if (!raw || typeof raw !== "object") throw new Error("确认请求无效");
    const value = raw as { requestId?: unknown; approved?: unknown };
    if (typeof value.requestId !== "string" || typeof value.approved !== "boolean") throw new Error("确认请求无效");
    // Entry log for the approval-resolution path: a previous incident showed
    // the renderer invoke never reaching this handler while its promise hung
    // forever (button stuck on "正在继续…"); this line proves arrival.
    void log(`capability resolve id=${value.requestId} approved=${value.approved}`);
    const request = pendingCapabilities.get(value.requestId);
    if (!request) {
      void appendCapabilityAudit(app.getPath("userData"), { capability: "platform", operation: "capability_resolve", target: value.requestId.slice(0, 300), outcome: "expired" });
      void log(`capability resolve id=${value.requestId} outcome=expired`);
      return { requestId: value.requestId, ok: false, error: "本地请求已过期或已处理", canceled: true };
    }
    pendingCapabilities.delete(value.requestId);
    if (!value.approved) {
      auditCapabilityOutcome(request, "denied");
      logCapabilityOutcome(request, "denied");
      return { requestId: value.requestId, ok: false, error: "用户拒绝了目录外访问", canceled: true };
    }
    try {
      const needsWorkspace = request.capability.startsWith("workspace.") || request.capability === "command.run";
      const workspace = needsWorkspace ? requireWorkspace() : (workspaceRoot ?? "");
      // Reclassify at approval time so a changed symlink or path cannot reuse
      // the original card without a fresh check.
      await approvalFor(request, workspace);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "本地能力执行失败";
      auditCapabilityOutcome(request, "failed", message);
      logCapabilityOutcome(request, "failed", message);
      return { requestId: value.requestId, ok: false, error: message };
    }
    // Acknowledge immediately so the chat card closes; the capability then
    // runs in the background and its result is pushed back via the
    // `desktop.capability-result` event. The invoke previously waited for
    // the full execution (up to the 120s command timeout), leaving the card
    // stuck on "正在继续…" for the whole command runtime.
    void (async () => {
      try {
        const result = await executeCapability(request);
        const detail = result.error ?? commandOutcomeDetail(result.value);
        auditCapabilityOutcome(request, result.ok ? "completed" : "failed", detail);
        logCapabilityOutcome(request, result.ok ? "completed" : "failed", detail);
        event.sender.send("desktop.capability-result", result);
      } catch (reason) {
        const message = reason instanceof Error ? reason.message : "本地能力执行失败";
        auditCapabilityOutcome(request, "failed", message);
        logCapabilityOutcome(request, "failed", message);
        event.sender.send("desktop.capability-result", { requestId: request.requestId, ok: false, error: message });
      }
    })();
    return { requestId: request.requestId, ok: true, acknowledged: true };
  });
  ipcMain.handle("desktop.clear-workspace", async (event) => {
    assertDesktopRenderer(event);
    workspaceRoot = null;
    recentHtmlWriteTargets.clear();
    await saveWorkspaceState();
    return true;
  });
  ipcMain.handle("desktop.open-browser", async (event, rawUrl: unknown) => {
    assertDesktopRenderer(event);
    let target: string | undefined;
    try { target = typeof rawUrl === "string" ? new URL(rawUrl).origin : undefined; } catch { target = undefined; }
    void appendCapabilityAudit(app.getPath("userData"), { capability: "browser", operation: "open", target, outcome: "started" });
    return openBrowser(rawUrl);
  });
  ipcMain.handle("desktop.run-command", async (event, command: unknown) => {
    assertDesktopRenderer(event);
    const workspace = requireWorkspace();
    void appendCapabilityAudit(app.getPath("userData"), { capability: "command", operation: "run", target: String(command).slice(0, 300), outcome: "started" });
    const result = await runWorkspaceCommand(command, workspace);
    void appendCapabilityAudit(app.getPath("userData"), { capability: "command", operation: "run", target: String(command).slice(0, 300), outcome: result.timedOut || result.exitCode !== 0 ? "failed" : "completed", detail: result.timedOut ? "timeout" : `exit:${result.exitCode}` });
    return { ...result, canceled: false };
  });
  ipcMain.handle("desktop.list-directory", async (event, relativePath: unknown) => {
    assertDesktopRenderer(event);
    return listWorkspaceDirectory(requireWorkspace(), relativePath);
  });
  ipcMain.handle("desktop.read-text-file", async (event, relativePath: unknown) => {
    assertDesktopRenderer(event);
    return readWorkspaceText(requireWorkspace(), relativePath);
  });
  ipcMain.handle("desktop.read-html-preview", async (event, rawPath: unknown) => {
    assertDesktopRenderer(event);
    const workspace = requireWorkspace();
    if (typeof rawPath !== "string" || !isHtmlPreviewPath(rawPath)) {
      throw new Error("仅支持预览 HTML 文件");
    }
    pruneRecentHtmlWriteTargets();
    const { target, scope } = await resolveLocalPath(workspace, rawPath);
    if (scope === "external" && !recentHtmlWriteTargets.has(target)) {
      throw new Error("仅支持预览本次任务刚写入的本地 HTML 文件");
    }
    const html = await readLocalText(workspace, rawPath);
    void appendCapabilityAudit(app.getPath("userData"), {
      capability: "workspace",
      operation: "preview",
      target: String(rawPath).slice(0, 300),
      outcome: "completed",
      detail: `bytes:${Buffer.byteLength(html, "utf8")}`
    });
    return html;
  });
  ipcMain.handle("desktop.open-html-preview", async (event, rawHtml: unknown) => {
    assertDesktopRenderer(event);
    if (typeof rawHtml !== "string" || !rawHtml.trim()) throw new Error("HTML 内容无效");
    if (Buffer.byteLength(rawHtml, "utf8") > 16 * 1024 * 1024) {
      throw new Error("HTML 内容超过 16 MB");
    }
    const target = join(tmpdir(), `cowork-html-preview-${randomUUID()}.html`);
    await writeFile(target, rawHtml, { encoding: "utf8", mode: 0o600 });
    htmlPreviewTempFiles.add(target);
    await shell.openExternal(pathToFileURL(target).toString());
    void appendCapabilityAudit(app.getPath("userData"), {
      capability: "workspace",
      operation: "open-preview",
      target,
      outcome: "completed"
    });
    return true;
  });
  ipcMain.handle("desktop.write-text-file", async (event, relativePath: unknown, content: unknown) => {
    assertDesktopRenderer(event);
    const workspace = requireWorkspace();
    await writeWorkspaceText(workspace, relativePath, content);
    void appendCapabilityAudit(app.getPath("userData"), { capability: "workspace", operation: "write", target: String(relativePath), outcome: "completed" });
    return true;
  });
  ipcMain.handle("desktop.create-directory", async (event, relativePath: unknown) => {
    assertDesktopRenderer(event);
    await createWorkspaceDirectory(requireWorkspace(), relativePath);
    void appendCapabilityAudit(app.getPath("userData"), { capability: "workspace", operation: "mkdir", target: String(relativePath), outcome: "completed" });
    return true;
  });
  ipcMain.handle("desktop.trash-file", async (event, relativePath: unknown) => {
    assertDesktopRenderer(event);
    const workspace = requireWorkspace();
    const target = await resolveWorkspacePath(workspace, relativePath);
    await shell.trashItem(target);
    void appendCapabilityAudit(app.getPath("userData"), { capability: "workspace", operation: "trash", target: String(relativePath), outcome: "completed" });
    return true;
  });
  ipcMain.handle("desktop.open-file", async (event, relativePath: unknown) => {
    assertDesktopRenderer(event);
    const target = await resolveWorkspacePath(requireWorkspace(), relativePath);
    return (await shell.openPath(target)) === "";
  });
}

app.whenReady().then(async () => {
  // The renderer provides the product UI. Keep the native application menu on
  // macOS, where it belongs in the system menu bar, but remove Electron's
  // default File/Edit/View/Window menu from Windows and Linux.
  if (process.platform !== "darwin") Menu.setApplicationMenu(null);
  await log(`app ready version=${app.getVersion()} platform=${process.platform}`);
  await loadWorkspaceState();
  await registerDesktopTransport();
  registerCapabilityHandlers();
  await initializeDesktopUpdater(() => mainWindow, log, () => {
    try {
      return session.fromPartition(partition);
    } catch {
      return null;
    }
  });
  createWindow();
  // A system sleep/resume does not change the window visibility, so the
  // renderer never receives a visibilitychange after wake-up. Forward the
  // powerMonitor events (debounced: resume and unlock-screen usually fire
  // together) and let the renderer probe whether the platform session survived.
  let lastResumeForwardAt = 0;
  const forwardSystemResume = (reason: string) => {
    const now = Date.now();
    if (now - lastResumeForwardAt < 5_000) return;
    lastResumeForwardAt = now;
    const win = mainWindow;
    if (win && !win.isDestroyed()) {
      win.webContents.send("desktop.system-resumed", reason);
    }
  };
  powerMonitor.on("resume", () => forwardSystemResume("resume"));
  powerMonitor.on("unlock-screen", () => forwardSystemResume("unlock-screen"));
  // No blind startup check anymore: the update directory sits behind the
  // platform authentication, so the first check fires from the renderer once
  // the user is logged in (DesktopUpdateNotice mounts inside AuthGate) and
  // carries the session cookies via attachAuthHeaders.
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.once("will-quit", (event) => {
  if (htmlPreviewTempFiles.size === 0) return;
  event.preventDefault();
  const files = [...htmlPreviewTempFiles];
  htmlPreviewTempFiles.clear();
  void Promise.all(files.map((file) => rm(file, { force: true })))
    .catch(() => undefined)
    .finally(() => app.quit());
});
process.on("uncaughtException", (error) => { void log("uncaught exception", error); });
process.on("unhandledRejection", (reason) => { void log("unhandled rejection", reason); });
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", () => { void closeAllBrowserSessions(); });

import { mkdir, readdir, stat } from "node:fs/promises";
import { join } from "node:path";
import { app } from "electron";
import { chromium, type BrowserContext, type Download, type Frame, type Locator, type Page } from "playwright-core";
import type { LocalCapabilityName } from "./capability-protocol";
import { resolveLocalPath } from "./workspace-files";
import {
  AFTER_SNAPSHOT_LIMIT,
  LOGIN_HINT,
  SNAPSHOT_MAX_TOTAL,
  collectInFrame,
  looksLikeLoginPage,
  maskElementLine,
  nextDownloadName,
  parseNavigateTiming,
  parseReadArgs,
  parseSelectArgs,
  parseSnapshotArgs,
  parseTabIndex,
  parseTypeArgs,
  parseWaitArgs,
  sanitizeDownloadFilename,
  sanitizeElementHtml,
} from "./browser-snapshot";

export type DesktopBrowserKind = "chrome" | "edge";

export type BrowserCapabilityContext = { workspace: string | null };

type DownloadRecord = {
  id: string;
  filename: string;
  path: string;
  url: string;
  state: "in_progress" | "completed" | "canceled";
  size?: number;
  savedAt: string;
};

type BrowserSession = {
  id: string;
  kind: DesktopBrowserKind;
  context: BrowserContext;
  activePage: Page | null;
  refs: Map<string, Frame>;
  downloads: DownloadRecord[];
  downloadsDir: string;
  downloadSeq: number;
};

const sessions = new Map<string, BrowserSession>();
const executableCandidates: Record<DesktopBrowserKind, string[]> = {
  chrome: [
    process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, "Google/Chrome/Application/chrome.exe") : "",
    process.env.PROGRAMFILES ? join(process.env.PROGRAMFILES, "Google/Chrome/Application/chrome.exe") : "",
    process.env["PROGRAMFILES(X86)"] ? join(process.env["PROGRAMFILES(X86)"], "Google/Chrome/Application/chrome.exe") : "",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ],
  edge: [
    process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, "Microsoft/Edge/Application/msedge.exe") : "",
    process.env.PROGRAMFILES ? join(process.env.PROGRAMFILES, "Microsoft/Edge/Application/msedge.exe") : "",
    process.env["PROGRAMFILES(X86)"] ? join(process.env["PROGRAMFILES(X86)"], "Microsoft/Edge/Application/msedge.exe") : "",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/microsoft-edge",
  ],
};

async function findExecutable(kind: DesktopBrowserKind): Promise<string> {
  const configured = process.env.COWORK_BROWSER_EXECUTABLE?.trim();
  const candidates = configured ? [configured] : executableCandidates[kind];
  for (const candidate of candidates) {
    if (!candidate) continue;
    try {
      await stat(candidate);
      return candidate;
    } catch {
      // Try the next installed browser location.
    }
  }
  throw new Error(`未找到本机 ${kind === "edge" ? "Microsoft Edge" : "Google Chrome"}，请先安装浏览器`);
}

function requireSession(sessionId: unknown): BrowserSession {
  if (typeof sessionId !== "string" || !/^[A-Za-z0-9._:-]{8,128}$/.test(sessionId)) {
    throw new Error("浏览器会话 ID 无效");
  }
  const session = sessions.get(sessionId);
  if (!session) throw new Error("浏览器会话不存在，请先启动本地浏览器会话");
  return session;
}

function requireActivePage(session: BrowserSession): Page {
  const page = session.activePage && !session.activePage.isClosed() ? session.activePage : null;
  if (!page) {
    const fallback = session.context.pages().find((candidate) => !candidate.isClosed());
    if (!fallback) throw new Error("浏览器会话已没有打开的标签页，请重新启动本地浏览器会话");
    session.activePage = fallback;
    return fallback;
  }
  return page;
}

function requireHttpUrl(value: unknown): string {
  if (typeof value !== "string" || value.length > 4096) throw new Error("浏览器地址无效");
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("浏览器地址无效");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("浏览器只允许打开 HTTP 或 HTTPS 地址");
  return parsed.toString();
}

function resolveRef(session: BrowserSession, ref: string): Frame {
  const frame = session.refs.get(ref);
  if (!frame || frame.isDetached() || frame.page().isClosed() || frame.page() !== session.activePage) {
    throw new Error("页面已跳转或已切换标签页，旧引用已失效，请重新获取快照");
  }
  return frame;
}

function locator(session: BrowserSession, args: Record<string, unknown>) {
  const ref = typeof args.ref === "string" ? args.ref.trim() : "";
  const selector = typeof args.selector === "string" ? args.selector.trim() : "";
  if (!ref && !selector) throw new Error("请提供页面元素 ref 或 selector");
  if (ref) {
    if (!/^e[0-9]{1,5}$/.test(ref)) throw new Error("页面元素 ref 无效，请先获取最新页面快照");
    return resolveRef(session, ref).locator(`[data-cowork-ref="${ref}"]`).first();
  }
  if (selector.length > 512) throw new Error("页面选择器过长");
  return requireActivePage(session).locator(selector).first();
}

async function trackDownload(session: BrowserSession, download: Download): Promise<void> {
  session.downloadSeq += 1;
  let sourceUrl = "";
  try {
    const parsed = new URL(download.page()?.url() ?? "");
    sourceUrl = `${parsed.origin}${parsed.pathname}`;
  } catch {
    sourceUrl = "";
  }
  const record: DownloadRecord = {
    id: `d${session.downloadSeq}`,
    filename: sanitizeDownloadFilename(download.suggestedFilename()),
    path: "",
    url: sourceUrl,
    state: "in_progress",
    savedAt: new Date().toISOString(),
  };
  session.downloads.unshift(record);
  if (session.downloads.length > 50) session.downloads.length = 50;
  void (async () => {
    try {
      await mkdir(session.downloadsDir, { recursive: true });
      const existing = new Set(await readdir(session.downloadsDir).catch(() => [] as string[]));
      const finalName = nextDownloadName(existing, record.filename);
      const finalPath = join(session.downloadsDir, finalName);
      await download.saveAs(finalPath);
      record.filename = finalName;
      record.path = finalPath;
      record.size = (await stat(finalPath)).size;
      record.state = "completed";
    } catch {
      record.state = "canceled";
    }
  })();
}

function attachPage(session: BrowserSession, page: Page, options: { activate: boolean }): void {
  page.on("download", (download) => { void trackDownload(session, download); });
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) session.refs.clear();
  });
  page.on("close", () => {
    if (session.activePage === page) {
      session.activePage = session.context.pages().find((candidate) => !candidate.isClosed()) ?? null;
    }
  });
  if (options.activate) session.activePage = page;
}

async function snapshotPage(session: BrowserSession, window: { offset: number; limit: number }): Promise<Record<string, unknown>> {
  const page = requireActivePage(session);
  const frames = page.frames();
  let nextRef = 1;
  let counted = 0;
  let passwordVisible = false;
  let formCount = -1;
  const lines: string[] = [];
  const refs = new Map<string, Frame>();
  for (const frame of frames) {
    if (frame.isDetached()) continue;
    try {
      const result = await frame.evaluate(collectInFrame, { nextRef, offset: window.offset, limit: window.limit, counted, maxTotal: SNAPSHOT_MAX_TOTAL });
      for (const ref of result.refs) refs.set(ref, frame);
      for (const element of result.elements) lines.push(maskElementLine(element.ref, element.role, element.name, element.value));
      nextRef = result.nextRef;
      counted = result.total;
      if (result.passwordVisible) passwordVisible = true;
      if (frame === frames[0]) formCount = result.formCount;
    } catch {
      // The frame navigated while snapshotting; its elements are simply absent.
    }
  }
  session.refs = refs;
  const url = page.url();
  const title = await page.title().catch(() => "");
  const loginRequired = looksLikeLoginPage(url, title, passwordVisible, formCount);
  return {
    url,
    title,
    elements: lines.join("\n"),
    elementCount: lines.length,
    total: counted,
    offset: window.offset,
    limit: window.limit,
    hasMore: window.offset + lines.length < counted,
    ...(loginRequired ? { loginRequired: true, loginHint: LOGIN_HINT } : {}),
  };
}

async function afterSnapshot(session: BrowserSession): Promise<Record<string, unknown> | undefined> {
  try {
    await requireActivePage(session).waitForLoadState("domcontentloaded", { timeout: 5000 }).catch(() => undefined);
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        return await snapshotPage(session, { offset: 0, limit: AFTER_SNAPSHOT_LIMIT });
      } catch {
        await new Promise((resolve) => { setTimeout(resolve, 800); });
      }
    }
  } catch {
    // Navigation in flight; the caller omits the after field.
  }
  return undefined;
}

async function detectLoginRequired(session: BrowserSession): Promise<boolean> {
  try {
    const page = requireActivePage(session);
    const passwordVisible = await page.evaluate(() => Array.from(document.querySelectorAll("input[type=password]")).some((input) => input.getClientRects().length > 0));
    return looksLikeLoginPage(page.url(), await page.title().catch(() => ""), passwordVisible, -1);
  } catch {
    return false;
  }
}

async function pageInfo(session: BrowserSession): Promise<Record<string, unknown>> {
  const page = requireActivePage(session);
  return { url: page.url(), title: await page.title().catch(() => "") };
}

// Playwright's raw "Timeout 15000ms exceeded" tells the agent nothing about
// WHY the element was not actionable; attach an actionable diagnosis.
async function diagnoseLocator(target: Locator): Promise<string> {
  try {
    const count = await target.count();
    if (count === 0) return "元素未找到：页面可能已跳转或该元素尚未渲染，请重新获取快照，或用 browser.wait 等元素出现";
    const state = await target.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      return {
        visible: rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none",
        disabled: (element as HTMLButtonElement).disabled === true,
        size: `${Math.round(rect.width)}x${Math.round(rect.height)}`,
      };
    }).catch(() => null);
    if (!state) return "元素已脱离页面（DOM 被销毁），请重新获取快照";
    if (!state.visible) return `元素存在但不可见（尺寸 ${state.size}）：可能被隐藏，需先展开菜单、切换标签页或滚动到可见区域`;
    if (state.disabled) return "元素存在但处于禁用（disabled）状态，需先满足页面条件（如勾选协议、填写必填项）";
    return "元素可见但操作被拦截：可能被浮层遮挡，可尝试 browser.scroll 后重试或改用更精确的 selector";
  } catch {
    return "无法诊断元素状态，请重新获取快照";
  }
}

async function enrichActionError(action: string, reason: unknown, target: Locator): Promise<Error> {
  const message = reason instanceof Error ? reason.message : String(reason);
  if (!/timeout/i.test(message)) return reason instanceof Error ? reason : new Error(message);
  return new Error(`${action}超时：${await diagnoseLocator(target)}`);
}

export async function startBrowserSession(rawKind: unknown = "chrome", options: { workspaceRoot?: string | null } = {}): Promise<Record<string, unknown>> {
  const kind: DesktopBrowserKind = rawKind === "edge" ? "edge" : "chrome";
  const existing = [...sessions.values()].find((item) => item.kind === kind);
  if (existing) {
    // A user closing the automation window (or the browser crashing) leaves a
    // dead entry in the map; reusing it would hand the agent a session whose
    // pages no longer exist. Drop it and relaunch instead.
    const alive = existing.context.browser()?.isConnected() !== false && existing.context.pages().some((page) => !page.isClosed());
    if (alive) {
      requireActivePage(existing);
      return { sessionId: existing.id, session_id: existing.id, browser: existing.kind, reused: true, ...(await pageInfo(existing)) };
    }
    sessions.delete(existing.id);
    await existing.context.close().catch(() => undefined);
  }
  const executablePath = await findExecutable(kind);
  const id = `desktop-browser-${kind}-${Date.now().toString(36)}`;
  const profileDir = join(app.getPath("userData"), "browser-automation", kind);
  await mkdir(profileDir, { recursive: true });
  const context = await chromium.launchPersistentContext(profileDir, {
    executablePath,
    headless: false,
    viewport: null,
    acceptDownloads: true,
  });
  const session: BrowserSession = {
    id,
    kind,
    context,
    activePage: null,
    refs: new Map(),
    downloads: [],
    downloadsDir: options.workspaceRoot
      ? join(options.workspaceRoot, ".downloads")
      : join(app.getPath("userData"), "browser-automation", "downloads"),
    downloadSeq: 0,
  };
  context.on("page", (page) => attachPage(session, page, { activate: true }));
  const initial = context.pages()[0] ?? await context.newPage();
  attachPage(session, initial, { activate: true });
  sessions.set(id, session);
  // The runtime plugin schema spells tool parameters snake_case, so expose
  // both sessionId and session_id for the agent reading the tool result.
  return { sessionId: id, session_id: id, browser: kind, reused: false, ...(await pageInfo(session)) };
}

async function readBrowserPage(session: BrowserSession, args: Record<string, unknown>): Promise<Record<string, unknown>> {
  const { maxChars } = parseReadArgs(args);
  const page = requireActivePage(session);
  const url = page.url();
  const title = await page.title().catch(() => "");
  // Element mode: return the target's sanitized outerHTML so the agent can
  // inspect markup structure it cannot infer from the snapshot lines.
  if (typeof args.ref === "string" || typeof args.selector === "string") {
    const target = locator(session, args);
    const raw = await target.evaluate((element) => element.outerHTML).catch(() => null);
    if (raw === null) throw new Error("目标元素不可读：页面可能已跳转或元素已脱离，请重新获取快照");
    const html = sanitizeElementHtml(raw).slice(0, maxChars);
    return { url, title, html, chars: html.length, truncated: html.length >= maxChars };
  }
  const result = await page.evaluate((limit: number) => ({
    text: document.body ? document.body.innerText.slice(0, limit) : "",
    passwordVisible: Array.from(document.querySelectorAll("input[type=password]")).some((input) => input.getClientRects().length > 0),
  }), maxChars);
  const loginRequired = looksLikeLoginPage(url, title, result.passwordVisible, -1);
  return {
    url,
    title,
    text: result.text,
    chars: result.text.length,
    truncated: result.text.length >= maxChars,
    ...(loginRequired ? { loginRequired: true, loginHint: LOGIN_HINT } : {}),
  };
}

async function waitBrowserCondition(session: BrowserSession, args: Record<string, unknown>): Promise<Record<string, unknown>> {
  const request = parseWaitArgs(args);
  const page = requireActivePage(session);
  const startedAt = Date.now();
  try {
    if (request.until === "element" || request.until === "hidden") {
      await page.locator(request.selector!).first().waitFor({ state: request.until === "element" ? "visible" : "hidden", timeout: request.timeoutMs });
    } else if (request.until === "text") {
      await page.waitForFunction((value: string) => !!document.body && document.body.innerText.includes(value), request.value!, { timeout: request.timeoutMs });
    } else if (request.until === "url") {
      await page.waitForURL(request.value!, { timeout: request.timeoutMs });
    } else {
      await page.waitForLoadState(request.loadState, { timeout: request.timeoutMs });
    }
  } catch {
    const detail = (request.selector ?? request.value ?? request.loadState).slice(0, 120);
    throw new Error(`等待超时：${request.until} ${detail}（${request.timeoutMs}ms）。页面可能仍在加载或条件未满足，可重试或延长超时`);
  }
  return { ...(await pageInfo(session)), matched: true, waitedMs: Date.now() - startedAt };
}

async function selectBrowserValue(session: BrowserSession, args: Record<string, unknown>): Promise<Record<string, unknown>> {
  const request = parseSelectArgs(args);
  const target = locator(session, args);
  const info = await target.evaluate((element) => ({ tag: element.tagName, multiple: (element as HTMLSelectElement).multiple }));
  if (info.tag !== "SELECT") throw new Error("目标元素不是下拉框（select）");
  if (info.multiple) throw new Error("该下拉框支持多选，暂不支持自动选择");
  if (request.index !== undefined) {
    await target.selectOption({ index: request.index }, { timeout: 15000 });
  } else if (request.value !== undefined) {
    try {
      await target.selectOption({ value: request.value }, { timeout: 15000 });
    } catch {
      await target.selectOption({ label: request.value }, { timeout: 15000 });
    }
  } else {
    await target.selectOption({ label: request.label }, { timeout: 15000 });
  }
  const selected = await target.evaluate((element) => Array.from((element as HTMLSelectElement).selectedOptions).map((option) => option.value || option.text));
  return { ...(await pageInfo(session)), selected };
}

async function uploadBrowserFile(session: BrowserSession, args: Record<string, unknown>, context: BrowserCapabilityContext): Promise<Record<string, unknown>> {
  if (!context.workspace) throw new Error("请先选择工作文件夹");
  const rawPath = typeof args.path === "string" ? args.path : "";
  if (!rawPath.trim()) throw new Error("请提供要上传的本地文件路径");
  const { target } = await resolveLocalPath(context.workspace, rawPath);
  const info = await stat(target);
  if (!info.isFile()) throw new Error("上传目标必须是普通文件");
  if (info.size > 100 * 1024 * 1024) throw new Error("上传文件超过 100 MiB 限制");
  try {
    await locator(session, args).setInputFiles(target, { timeout: 15000 });
  } catch (reason) {
    const message = reason instanceof Error ? reason.message : "";
    if (message.includes("input") || message.includes("file")) throw new Error("目标不是文件上传控件，请确认选择的是 file input");
    throw reason;
  }
  return { ...(await pageInfo(session)), uploaded: target.split(/[\\/]/).pop() };
}

function openPages(session: BrowserSession): Page[] {
  return session.context.pages().filter((page) => !page.isClosed());
}

async function listBrowserTabs(session: BrowserSession): Promise<Record<string, unknown>> {
  const pages = openPages(session);
  const tabs = [] as Array<Record<string, unknown>>;
  for (let index = 0; index < pages.length; index += 1) {
    tabs.push({
      index,
      url: pages[index].url(),
      title: await pages[index].title().catch(() => ""),
      active: pages[index] === session.activePage,
    });
  }
  return { tabs, activeIndex: pages.findIndex((page) => page === session.activePage), count: pages.length };
}

async function newBrowserTab(session: BrowserSession, args: Record<string, unknown>): Promise<Record<string, unknown>> {
  const url = typeof args.url === "string" && args.url.trim() ? requireHttpUrl(args.url) : "";
  const page = await session.context.newPage();
  attachPage(session, page, { activate: true });
  if (url) await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
  return { index: openPages(session).indexOf(page), url: page.url(), title: await page.title().catch(() => ""), count: openPages(session).length };
}

async function selectBrowserTab(session: BrowserSession, args: Record<string, unknown>): Promise<Record<string, unknown>> {
  const tabIndex = parseTabIndex(args, true);
  const pages = openPages(session);
  if (tabIndex >= pages.length) throw new Error(`标签页索引超出范围（当前共 ${pages.length} 个），请先用 tab_list 确认`);
  const page = pages[tabIndex];
  session.activePage = page;
  session.refs.clear();
  await page.bringToFront().catch(() => undefined);
  return { index: tabIndex, url: page.url(), title: await page.title().catch(() => ""), count: pages.length };
}

async function closeBrowserTab(session: BrowserSession, args: Record<string, unknown>): Promise<Record<string, unknown>> {
  const tabIndex = parseTabIndex(args, false);
  const pages = openPages(session);
  if (pages.length <= 1) throw new Error("这是最后一个标签页，请改用 browser.session_close 结束会话");
  const index = tabIndex >= 0 ? tabIndex : pages.findIndex((page) => page === session.activePage);
  if (index < 0 || index >= pages.length) throw new Error(`标签页索引超出范围（当前共 ${pages.length} 个），请先用 tab_list 确认`);
  await pages[index].close();
  const remaining = openPages(session);
  if (session.activePage?.isClosed() ?? true) session.activePage = remaining[Math.min(index, remaining.length - 1)] ?? null;
  return { closed: true, activeIndex: remaining.findIndex((page) => page === session.activePage), count: remaining.length };
}

export async function closeBrowserSession(args: Record<string, unknown>): Promise<boolean> {
  const session = requireSession(args.sessionId);
  sessions.delete(session.id);
  await session.context.close();
  return true;
}

export async function closeAllBrowserSessions(): Promise<void> {
  const active = [...sessions.values()];
  sessions.clear();
  await Promise.allSettled(active.map((session) => session.context.close()));
}

// Every browser.* capability except browser.open (which routes through the OS
// default browser) dispatches from this map; executeCapability must reject
// anything missing here instead of falling through to command execution.
export const BROWSER_CAPABILITY_HANDLERS: Partial<Record<LocalCapabilityName, (args: Record<string, unknown>, context: BrowserCapabilityContext) => Promise<unknown>>> = {
  "browser.session_start": (args, context) => startBrowserSession(args.browser, { workspaceRoot: context.workspace }),
  "browser.navigate": async (args) => {
    const session = requireSession(args.sessionId);
    const url = requireHttpUrl(args.url);
    const timing = parseNavigateTiming(args, { waitUntil: "domcontentloaded", timeoutMs: 30000 });
    await requireActivePage(session).goto(url, timing);
    const loginRequired = await detectLoginRequired(session);
    return { ...(await pageInfo(session)), ...(loginRequired ? { loginRequired: true, loginHint: LOGIN_HINT } : {}) };
  },
  "browser.snapshot": (args) => snapshotPage(requireSession(args.sessionId), parseSnapshotArgs(args)),
  "browser.click": async (args) => {
    const session = requireSession(args.sessionId);
    const target = locator(session, args);
    try {
      await target.click({ timeout: 15000 });
    } catch (reason) {
      throw await enrichActionError("点击", reason, target);
    }
    return { ...(await pageInfo(session)), ...(args.withSnapshot === false ? {} : { after: await afterSnapshot(session) }) };
  },
  "browser.type": async (args) => {
    const request = parseTypeArgs(args);
    const session = requireSession(args.sessionId);
    if (request.mode === "keystrokes") {
      const target = locator(session, args);
      try {
        await target.click({ timeout: 15000 });
      } catch (reason) {
        throw await enrichActionError("定位输入框", reason, target);
      }
      await requireActivePage(session).keyboard.type(request.text, { delay: 30 });
    } else {
      const target = locator(session, args);
      try {
        await target.fill(request.text, { timeout: 15000 });
      } catch (reason) {
        throw await enrichActionError("填充输入框", reason, target);
      }
    }
    return { ...(await pageInfo(session)), ...(args.withSnapshot === false ? {} : { after: await afterSnapshot(session) }) };
  },
  "browser.press": async (args) => {
    const key = typeof args.key === "string" ? args.key.trim() : "";
    if (!key || key.length > 64) throw new Error("按键无效");
    const session = requireSession(args.sessionId);
    if (args.ref || args.selector) {
      const target = locator(session, args);
      try {
        await target.press(key, { timeout: 15000 });
      } catch (reason) {
        throw await enrichActionError("按键", reason, target);
      }
    } else {
      await requireActivePage(session).keyboard.press(key);
    }
    return { ...(await pageInfo(session)), ...(args.withSnapshot === false ? {} : { after: await afterSnapshot(session) }) };
  },
  "browser.scroll": async (args) => {
    const direction = args.direction === "up" ? "up" : "down";
    const amount = typeof args.amount === "number" && Number.isFinite(args.amount) ? Math.max(100, Math.min(2000, args.amount)) : 700;
    const session = requireSession(args.sessionId);
    await requireActivePage(session).evaluate(({ direction: value, amount: pixels }) => window.scrollBy({ top: value === "up" ? -pixels : pixels, behavior: "instant" }), { direction, amount });
    return pageInfo(session);
  },
  "browser.screenshot": async (args) => {
    const session = requireSession(args.sessionId);
    const data = await requireActivePage(session).screenshot({ type: "jpeg", quality: 55 });
    const encoded = data.toString("base64");
    if (encoded.length > 180000) return { ...(await pageInfo(session)), truncated: true, size: encoded.length };
    return { ...(await pageInfo(session)), mimeType: "image/jpeg", dataUrl: `data:image/jpeg;base64,${encoded}` };
  },
  "browser.session_close": (args) => closeBrowserSession(args),
  "browser.read": (args) => readBrowserPage(requireSession(args.sessionId), args),
  "browser.wait": (args) => waitBrowserCondition(requireSession(args.sessionId), args),
  "browser.hover": async (args) => {
    const session = requireSession(args.sessionId);
    const target = locator(session, args);
    try {
      await target.hover({ timeout: 15000 });
    } catch (reason) {
      throw await enrichActionError("悬停", reason, target);
    }
    return { ...(await pageInfo(session)), ...(args.withSnapshot === false ? {} : { after: await afterSnapshot(session) }) };
  },
  "browser.select": async (args) => {
    const session = requireSession(args.sessionId);
    const result = await selectBrowserValue(session, args);
    return { ...result, ...(args.withSnapshot === false ? {} : { after: await afterSnapshot(session) }) };
  },
  "browser.upload": (args, context) => uploadBrowserFile(requireSession(args.sessionId), args, context),
  "browser.downloads": async (args) => {
    const session = requireSession(args.sessionId);
    return { dir: session.downloadsDir, downloads: session.downloads };
  },
  "browser.back": async (args) => {
    const session = requireSession(args.sessionId);
    const timing = parseNavigateTiming(args, { waitUntil: "domcontentloaded", timeoutMs: 30000 });
    const response = await requireActivePage(session).goBack(timing);
    return { ...(await pageInfo(session)), navigated: response !== null };
  },
  "browser.reload": async (args) => {
    const session = requireSession(args.sessionId);
    const timing = parseNavigateTiming(args, { waitUntil: "domcontentloaded", timeoutMs: 30000 });
    session.refs.clear();
    const page = requireActivePage(session);
    if (args.ignoreCache === true) {
      // playwright's reload has no ignoreCache flag; disable the HTTP cache
      // over CDP for this reload only.
      const client = await session.context.newCDPSession(page);
      try {
        await client.send("Network.setCacheDisabled", { cacheDisabled: true });
        await page.reload(timing);
      } finally {
        await client.detach().catch(() => undefined);
      }
    } else {
      await page.reload(timing);
    }
    const loginRequired = await detectLoginRequired(session);
    return { ...(await pageInfo(session)), ...(loginRequired ? { loginRequired: true, loginHint: LOGIN_HINT } : {}) };
  },
  "browser.tab_list": (args) => listBrowserTabs(requireSession(args.sessionId)),
  "browser.tab_new": (args) => newBrowserTab(requireSession(args.sessionId), args),
  "browser.tab_select": (args) => selectBrowserTab(requireSession(args.sessionId), args),
  "browser.tab_close": (args) => closeBrowserTab(requireSession(args.sessionId), args),
};

// Pure logic for the browser automation snapshot pipeline. This module must
// stay free of electron/playwright runtime imports so node --test can load it
// directly; only serialize-safe helpers live here.

export const SNAPSHOT_MAX_TOTAL = 5000;
export const SNAPSHOT_DEFAULT_LIMIT = 200;
export const SNAPSHOT_MAX_LIMIT = 200;
export const AFTER_SNAPSHOT_LIMIT = 80;
export const LOGIN_HINT = "页面似乎需要登录：请在自动化浏览器窗口完成登录，然后重新获取快照";

export type SnapshotWindow = { offset: number; limit: number };

export function parseSnapshotArgs(args: Record<string, unknown>): SnapshotWindow {
  const offset = typeof args.offset === "number" && Number.isFinite(args.offset) ? Math.max(0, Math.min(10000, Math.floor(args.offset))) : 0;
  const limit = typeof args.limit === "number" && Number.isFinite(args.limit) ? Math.max(1, Math.min(SNAPSHOT_MAX_LIMIT, Math.floor(args.limit))) : SNAPSHOT_DEFAULT_LIMIT;
  return { offset, limit };
}

export type WaitRequest = {
  until: "element" | "hidden" | "text" | "url" | "load";
  selector: string | null;
  value: string | null;
  loadState: "domcontentloaded" | "load" | "networkidle";
  timeoutMs: number;
};

export function parseWaitArgs(args: Record<string, unknown>): WaitRequest {
  const until = args.until === "element" || args.until === "text" || args.until === "url" || args.until === "load" || args.until === "hidden" ? args.until : null;
  if (!until) throw new Error(`等待条件无效：until 只支持 element、text、url、load 或 hidden。示例 {"until": "element", "selector": "#submit"} 或 {"until": "text", "value": "已提交"}；实际收到 until=${JSON.stringify(args.until ?? null)}`);
  const timeoutMs = typeof args.timeoutMs === "number" && Number.isFinite(args.timeoutMs) ? Math.max(1000, Math.min(60000, Math.floor(args.timeoutMs))) : 10000;
  const rawValue = typeof args.value === "string" ? args.value.trim() : "";
  if (until === "element" || until === "hidden") {
    const selector = typeof args.selector === "string" ? args.selector.trim() : "";
    if (selector.length > 512) throw new Error("页面选择器过长");
    if (!selector) throw new Error("until 为 element 或 hidden 时必须提供 selector");
    return { until, selector, value: null, loadState: "domcontentloaded", timeoutMs };
  }
  if (until === "load") {
    const loadState = rawValue === "load" || rawValue === "networkidle" ? rawValue : "domcontentloaded";
    return { until, selector: null, value: rawValue || null, loadState, timeoutMs };
  }
  if (!rawValue) throw new Error(`until 为 ${until} 时必须提供 value`);
  if (rawValue.length > 512) throw new Error("等待条件 value 过长");
  return { until, selector: null, value: rawValue, loadState: "domcontentloaded", timeoutMs };
}

export type NavigateTiming = { waitUntil: "domcontentloaded" | "load" | "networkidle"; timeoutMs: number };

export function parseNavigateTiming(args: Record<string, unknown>, defaults: { waitUntil: NavigateTiming["waitUntil"]; timeoutMs: number }): NavigateTiming {
  const waitUntil = args.waitUntil === "load" || args.waitUntil === "networkidle" ? args.waitUntil : args.waitUntil === "domcontentloaded" ? "domcontentloaded" : defaults.waitUntil;
  const timeoutMs = typeof args.timeoutMs === "number" && Number.isFinite(args.timeoutMs) ? Math.max(1000, Math.min(120000, Math.floor(args.timeoutMs))) : defaults.timeoutMs;
  return { waitUntil, timeoutMs };
}

export type TypeRequest = { mode: "replace" | "keystrokes"; text: string };

export function parseTypeArgs(args: Record<string, unknown>): TypeRequest {
  const mode = args.mode === "keystrokes" ? "keystrokes" : "replace";
  const text = typeof args.text === "string" ? args.text : "";
  const cap = mode === "keystrokes" ? 2000 : 100000;
  if (text.length > cap) throw new Error(`输入内容过长（${mode === "keystrokes" ? "逐键" : "替换"}模式最多 ${cap} 字符）`);
  return { mode, text };
}

export type SelectRequest = { value?: string; label?: string; index?: number };

export function parseSelectArgs(args: Record<string, unknown>): SelectRequest {
  const provided = ["value", "label", "index"].filter((key) => args[key] !== undefined && args[key] !== null);
  if (provided.length !== 1) throw new Error("请在 value、label、index 中恰好提供一个选择条件");
  const request: SelectRequest = {};
  const value = typeof args.value === "string" ? args.value : null;
  const label = typeof args.label === "string" ? args.label : null;
  const index = typeof args.index === "number" && Number.isInteger(args.index) ? args.index : null;
  if (value !== null && provided.includes("value")) {
    if (value.length > 512) throw new Error("选项 value 过长");
    request.value = value;
  }
  if (label !== null && provided.includes("label")) {
    if (label.length > 512) throw new Error("选项 label 过长");
    request.label = label;
  }
  if (index !== null && provided.includes("index")) {
    if (index < 0 || index > 999) throw new Error("选项 index 超出范围");
    request.index = index;
  }
  if (provided.length === 1 && !request.value && !request.label && request.index === undefined) throw new Error("选择条件类型无效");
  return request;
}

export function parseReadArgs(args: Record<string, unknown>): { maxChars: number } {
  const maxChars = typeof args.maxChars === "number" && Number.isFinite(args.maxChars) ? Math.max(500, Math.min(40000, Math.floor(args.maxChars))) : 20000;
  return { maxChars };
}

export function parseTabIndex(args: Record<string, unknown>, required: boolean): number {
  if (typeof args.tabIndex !== "number" || !Number.isInteger(args.tabIndex)) {
    if (required) throw new Error("请提供有效的 tab_index");
    return -1;
  }
  if (args.tabIndex < 0 || args.tabIndex > 99) throw new Error("tab_index 超出范围");
  return args.tabIndex;
}

export function looksLikeLoginPage(url: string, title: string, passwordVisible: boolean, formCount: number): boolean {
  if (!passwordVisible) return false;
  let pathAndQuery = "";
  try {
    const parsed = new URL(url);
    pathAndQuery = `${parsed.pathname}${parsed.search}`;
  } catch {
    pathAndQuery = url;
  }
  const urlHits = /(login|log-?in|sign-?in|auth|sso|passport|cas|oauth|verify|account|userlogin)/i.test(pathAndQuery);
  const titleHits = /(登录|登陆|login|sign in)/i.test(title);
  const formHint = formCount >= 0 && formCount <= 2;
  return urlHits || titleHits || formHint;
}

export function maskElementLine(ref: string, role: string, name: string, value: string): string {
  // Password inputs are rendered without name/value; keep the line so the
  // agent can still target the field for typing.
  if (role === "password") return `${ref} [password] (value hidden)`;
  const safeName = name.replace(/\s+/g, " ").trim().slice(0, 180);
  const safeValue = value.slice(0, 120);
  return `${ref} [${role}] ${JSON.stringify(safeName)}${safeValue && safeValue !== safeName ? ` value=${JSON.stringify(safeValue)}` : ""}`;
}

const WINDOWS_RESERVED = new Set(["CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"]);

export function sanitizeDownloadFilename(rawName: string): string {
  let name = rawName.replace(/[\\/:*?"<>|\x00-\x1f]/g, "_").trim();
  name = name.replace(/^[. ]+|[. ]+$/g, "");
  if (!name) name = "download";
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  if (WINDOWS_RESERVED.has(stem.toUpperCase())) name = `_${stem}${ext}`;
  return name.slice(0, 200);
}

export function nextDownloadName(existingNames: ReadonlySet<string>, suggested: string): string {
  if (!existingNames.has(suggested)) return suggested;
  const dot = suggested.lastIndexOf(".");
  const stem = dot > 0 ? suggested.slice(0, dot) : suggested;
  const ext = dot > 0 ? suggested.slice(dot) : "";
  for (let attempt = 1; attempt < 1000; attempt += 1) {
    const candidate = `${stem} (${attempt})${ext}`;
    if (!existingNames.has(candidate)) return candidate;
  }
  return `${stem} (${Date.now()})${ext}`;
}

// outerHTML carries value="" attributes verbatim; strip them from password
// inputs before the markup crosses the bridge.
export function sanitizeElementHtml(html: string): string {
  return html.replace(/<input\b[^>]*>/gi, (tag) => (/type=["']?password/i.test(tag)
    ? tag.replace(/\svalue=["'][^"']*["']/i, ' value="[hidden]"')
    : tag));
}

// Serialized into the page by frame.evaluate; must be fully self-contained
// (no closure over module constants). state flows across frames from Node.
export type FrameCollectPayload = {
  nextRef: number;
  offset: number;
  limit: number;
  counted: number;
  maxTotal: number;
};

export type FrameCollectElement = {
  ref: string;
  role: string;
  name: string;
  value: string;
};

export type FrameCollectResult = {
  nextRef: number;
  total: number;
  passwordVisible: boolean;
  formCount: number;
  elements: FrameCollectElement[];
  refs: string[];
};

export function collectInFrame(payload: FrameCollectPayload): FrameCollectResult {
  const selector = "a,button,input,textarea,select,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[contenteditable=true]";
  let nextRef = payload.nextRef;
  let total = payload.counted;
  let passwordVisible = false;
  const elements: FrameCollectElement[] = [];
  const refs: string[] = [];
  const deepClean = (root: ParentNode): void => {
    root.querySelectorAll("[data-cowork-ref]").forEach((node) => node.removeAttribute("data-cowork-ref"));
    root.querySelectorAll("*").forEach((node) => {
      if (node.shadowRoot) deepClean(node.shadowRoot);
    });
  };
  // Legacy enterprise UIs (table layouts, no aria) leave inputs without any
  // accessible name; recover a human label from label/title/table context so
  // the agent can tell fields apart instead of guessing.
  const labelOf = (element: Element): string => {
    const labelledBy = element.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = labelledBy.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter((target): target is HTMLElement => target !== null)
        .map((target) => target.textContent || "")
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
      if (text) return text;
    }
    const wrappingLabel = element.closest("label");
    if (wrappingLabel && wrappingLabel.textContent && wrappingLabel.textContent.trim()) {
      return wrappingLabel.textContent.replace(/\s+/g, " ").trim();
    }
    const id = element.getAttribute("id");
    if (id) {
      const forLabel = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (forLabel && forLabel.textContent && forLabel.textContent.trim()) {
        return forLabel.textContent.replace(/\s+/g, " ").trim();
      }
    }
    const title = element.getAttribute("title");
    if (title && title.trim()) return title.trim();
    const cell = element.closest("td,th");
    if (cell) {
      const previous = cell.previousElementSibling;
      if (previous && previous.textContent && previous.textContent.trim()) {
        return previous.textContent.replace(/\s+/g, " ").trim();
      }
    }
    return "";
  };
  const walk = (root: ParentNode): void => {
    for (const element of root.querySelectorAll("*")) {
      if (total >= payload.maxTotal) return;
      const tag = element.tagName;
      const inputType = tag === "INPUT" ? (element.getAttribute("type") || "text").toLowerCase() : "";
      const isPassword = tag === "INPUT" && inputType === "password";
      if (isPassword && element.getClientRects().length > 0) passwordVisible = true;
      if (element.matches(selector)) {
        // ASP.NET pages carry input[type=hidden] (ViewState etc.) that can
        // never be operated; other invisible elements are actionability traps
        // (playwright would time out on them). Keep both out of the ref list.
        const isHiddenInput = tag === "INPUT" && inputType === "hidden";
        const visible = element.getClientRects().length > 0;
        if (!isHiddenInput && visible) {
          const index = total;
          total += 1;
          const ref = `e${nextRef}`;
          nextRef += 1;
          refs.push(ref);
          if (index >= payload.offset && index < payload.offset + payload.limit) {
            element.setAttribute("data-cowork-ref", ref);
            // Password name/value are dropped inside the page and never cross
            // the bridge; the Node side renders the masked placeholder line.
            elements.push(isPassword
              ? { ref, role: "password", name: "", value: "" }
              : {
                ref,
                role: element.getAttribute("role") || tag.toLowerCase(),
                name: (element.getAttribute("aria-label") || labelOf(element) || element.getAttribute("placeholder") || element.textContent || ("value" in element ? String((element as HTMLInputElement).value) : "")).replace(/\s+/g, " ").trim().slice(0, 180),
                value: "value" in element ? String((element as HTMLInputElement).value || "").slice(0, 120) : "",
              });
          }
        }
      }
      if (element.shadowRoot) walk(element.shadowRoot);
    }
  };
  deepClean(document);
  walk(document);
  return { nextRef, total, passwordVisible, formCount: document.forms.length, elements, refs };
}

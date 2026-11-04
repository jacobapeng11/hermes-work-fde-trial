// Single source of truth for local capability names. The same 28 names are
// mirrored in preload/index.ts, preload/index.d.ts, src/desktop.d.ts,
// src/api/runtime-edge-client.ts (isLocalCapabilityName), packages/schemas,
// backend/app/schemas/runtime_edge.py and supervisor contracts.py — keep them
// in sync (guarded by tests/preload-capability-guard.test.mjs).
export const LOCAL_CAPABILITY_NAMES = [
  "workspace.list",
  "workspace.read",
  "workspace.write",
  "workspace.mkdir",
  "workspace.trash",
  "command.run",
  "browser.open",
  "browser.session_start",
  "browser.navigate",
  "browser.snapshot",
  "browser.click",
  "browser.type",
  "browser.press",
  "browser.scroll",
  "browser.screenshot",
  "browser.session_close",
  "browser.read",
  "browser.wait",
  "browser.hover",
  "browser.select",
  "browser.upload",
  "browser.downloads",
  "browser.back",
  "browser.reload",
  "browser.tab_list",
  "browser.tab_new",
  "browser.tab_select",
  "browser.tab_close",
] as const;

export type LocalCapabilityName = (typeof LOCAL_CAPABILITY_NAMES)[number];

export type LocalCapabilityRequest = {
  requestId: string;
  capability: LocalCapabilityName;
  args: Record<string, unknown>;
};

export type LocalCapabilityResult = {
  requestId: string;
  ok: boolean;
  value?: unknown;
  error?: string;
  canceled?: boolean;
  requiresApproval?: boolean;
  approval?: LocalCapabilityApproval;
  /** Approval accepted; execution runs in main and the result arrives via
   * the `desktop.capability-result` event instead of this reply. */
  acknowledged?: boolean;
};

export type LocalCapabilityApproval = {
  scope: "external";
  question: string;
  target?: string;
};

const capabilityNameSet = new Set<string>(LOCAL_CAPABILITY_NAMES);

export function validateCapabilityRequest(raw: unknown): LocalCapabilityRequest {
  if (!raw || typeof raw !== "object") throw new Error("本地能力请求无效");
  const value = raw as Partial<LocalCapabilityRequest>;
  if (typeof value.requestId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(value.requestId)) throw new Error("请求 ID 无效");
  if (typeof value.capability !== "string" || !capabilityNameSet.has(value.capability)) throw new Error("本地能力不受支持");
  if (!value.args || typeof value.args !== "object" || Array.isArray(value.args)) throw new Error("能力参数无效");
  let args = value.args as Record<string, unknown>;
  // The runtime desktop-local-bridge plugin declares browser tool parameters
  // in snake_case (session_id, wait_until, tab_index, ...) while desktop
  // handlers read camelCase. Normalize every snake_case top-level key at this
  // single boundary so the bridge never breaks on a parameter name alone.
  if (value.capability.startsWith("browser.")) {
    for (const [key, argValue] of Object.entries(args)) {
      if (key.length > 64 || !/^[a-z][a-z0-9]*(?:_[a-z][a-z0-9]*)+$/.test(key)) continue;
      const camel = key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());
      if (!(camel in args)) args = { ...args, [camel]: argValue };
    }
  }
  return { requestId: value.requestId, capability: value.capability as LocalCapabilityName, args };
}

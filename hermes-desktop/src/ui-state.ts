export type RuntimeConnectionState = "connecting" | "starting" | "mounting" | "probing" | "checking" | "waking" | "ready" | "failed";

const runtimeLabels: Record<RuntimeConnectionState, string> = {
  connecting: "正在连接",
  starting: "正在预热",
  mounting: "正在预热",
  probing: "正在预热",
  checking: "正在检测运行环境...",
  waking: "正在唤醒",
  ready: "已连接",
  failed: "连接异常"
};

export function runtimeStatusLabel(status: RuntimeConnectionState): string {
  return runtimeLabels[status];
}

export function runtimeConnectionStateForDisplay(
  state: RuntimeConnectionState,
  runtimeStatus: string | null | undefined,
  runtimeAwake: boolean
): RuntimeConnectionState {
  if (state === "waking") return state;
  if (runtimeStatus === "ready" && runtimeAwake) return "ready";
  return state;
}

// The desktop client starts every visit with the cloud-files pane collapsed
// (its file artifacts land in the local workspace folder, so the cloud pane is
// noise there); opening it is a per-visit, in-session toggle. Web keeps the
// persisted preference: wide screens open by default, an explicit toggle wins.
export function nextFilesPaneOpen(saved: string | null, wideScreen: boolean, desktop = false): boolean {
  if (desktop) return false;
  return saved === null ? wideScreen : saved === "true";
}

export function shouldSendOnKeyDown(event: { key: string; shiftKey: boolean; isComposing: boolean }): boolean {
  return event.key === "Enter" && !event.shiftKey && !event.isComposing;
}

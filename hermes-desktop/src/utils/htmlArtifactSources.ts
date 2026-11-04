import type { ChatFileSummary } from "@hermes-cloud/schemas";

export interface HtmlToolEvent {
  runtimeRunId?: string | null;
  name: string;
  status?: string | null;
  input?: string | null;
  result?: string | null;
  preview?: string | null;
}

const PATH_KEYS = new Set(["path", "file", "filename", "file_path", "name"]);
const TEXT_COMMAND_KEYS = new Set(["command", "cmd", "script"]);
// Directory listings enumerate every file in the workspace folder, so the
// list tool's result would drag unrelated historical .html files (e.g. an
// old sohu_mil.html) into the preview candidates and preempt the file this
// run actually produced.
const NON_PRODUCTIVE_TOOL_NAMES = new Set(["desktop_workspace_list"]);
const MAX_PARSE_DEPTH = 5;

export function isHtmlPath(value: string): boolean {
  const normalized = value.trim().replace(/\\/g, "/").split("?")[0];
  return /\.(?:html|htm)$/i.test(normalized);
}

export function isHtmlChatFile(file: ChatFileSummary): boolean {
  return file.mimeType?.toLowerCase() === "text/html"
    || isHtmlPath(file.relativePath || "")
    || isHtmlPath(file.displayName || "");
}

export function isZipChatFile(file: ChatFileSummary): boolean {
  return file.mimeType?.toLowerCase() === "application/zip"
    || /\.zip$/i.test(file.relativePath || "")
    || /\.zip$/i.test(file.displayName || "");
}

export function htmlPathsFromToolEvents(
  events: HtmlToolEvent[],
  runtimeRunId: string | null | undefined
): string[] {
  if (!runtimeRunId) return [];
  const paths = new Set<string>();
  for (const event of events) {
    if (event.runtimeRunId !== runtimeRunId) continue;
    if (NON_PRODUCTIVE_TOOL_NAMES.has(event.name)) continue;
    if (event.status && event.status !== "completed") continue;
    collectPaths(event.input, paths, 0, false);
    collectPaths(event.result, paths, 0, false);
    collectPaths(event.preview, paths, 0, false);
  }
  return [...paths];
}

function collectPaths(
  value: unknown,
  output: Set<string>,
  depth: number,
  includeTextCandidates: boolean
): void {
  if (value == null || depth > MAX_PARSE_DEPTH) return;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return;
    if (!/\s/.test(trimmed) && isHtmlPath(trimmed)) {
      output.add(trimmed);
      return;
    }
    if (includeTextCandidates && trimmed.length <= 20_000) {
      const candidates = trimmed.match(/(?:[^\s"'<>|&;]*\.html?)/gi) ?? [];
      for (const candidate of candidates) {
        const path = candidate.split(/[\s\\/]+/).pop() ?? candidate;
        if (isHtmlPath(path)) output.add(path);
      }
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return;
    }
    collectPaths(parsed, output, depth + 1, includeTextCandidates);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) collectPaths(item, output, depth + 1, includeTextCandidates);
    return;
  }
  if (typeof value !== "object") return;
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (typeof item === "string" && PATH_KEYS.has(key) && isHtmlPath(item)) {
      output.add(item);
      continue;
    }
    collectPaths(
      item,
      output,
      depth + 1,
      includeTextCandidates || TEXT_COMMAND_KEYS.has(key)
    );
  }
}

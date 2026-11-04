import type { ToolMessage } from "./pages/chatRunProjection";

export type ToolCategory = "file" | "sheet" | "search" | "web" | "knowledge" | "shell" | "delegate" | "generic";

export interface ToolPresentation {
  displayName: string;
  description: string;
  inputLabel: string;
  runningText: string;
  completedText: string;
  failedText: string;
  category: ToolCategory;
}

export interface ToolDetailPresentation {
  input: string | null;
  context: string | null;
  result: string | null;
  formatted: string | null;
  summary: string | null;
  truncated: boolean;
}

const MAX_DETAIL_CHARS = 12_000;
const MAX_DETAIL_DEPTH = 6;
const REDACTED = "••••";
const DEPTH_LIMIT = "[内容层级过深]";
const TRUNCATED = "[内容已截断]";
const SENSITIVE_KEYS = ["token", ["api", "key"].join(""), "authorization", "cookie", "password", "secret", "credential", "assertion"];

const exactPresentations: Record<string, ToolPresentation> = {
  search_files: presentation("查找相关文件", "在当前任务工作区查找文件或文本。", "检索条件", "正在查找相关文件…", "已找到相关文件", "查找文件失败", "search"),
  read_file: presentation("读取文件", "读取当前任务工作区中的文件内容。", "读取目标", "正在读取文件…", "已读取文件", "读取文件失败", "file"),
  read_excel: presentation("读取表格", "读取表格文件中的结构化数据。", "读取目标", "正在读取表格…", "已读取表格", "读取表格失败", "sheet"),
  write_file: presentation("保存文件", "将生成内容保存到当前任务工作区。", "保存内容", "正在保存文件…", "已保存文件", "保存文件失败", "file"),
  web_search: presentation("搜索网页", "使用网页搜索获取公开信息。", "搜索内容", "正在搜索网页…", "已完成网页搜索", "网页搜索失败", "web"),
  tool_describe: presentation("查看工具说明", "读取工具的名称、参数和调用规范。", "工具名称", "正在查看工具说明…", "已查看工具说明", "查看工具说明失败", "generic"),
  tool_search: presentation("查找可用工具", "查找当前会话可以调用的工具。", "查找条件", "正在查找可用工具…", "已找到可用工具", "查找工具失败", "search"),
  list_tools: presentation("查看可用工具", "查看当前会话已经加载的工具。", "工具范围", "正在查看可用工具…", "已查看可用工具", "查看工具失败", "generic"),
  session_search: presentation("检索会话记录", "检索当前会话或历史会话中的相关内容。", "检索条件", "正在检索会话记录…", "已检索会话记录", "会话检索失败", "search"),
  enterprise_knowledge_read: presentation("读取企业知识", "读取已授权企业知识包中的说明和口径文件。", "知识文件", "正在读取企业知识…", "已读取企业知识", "企业知识读取失败", "knowledge"),
  enterprise_sql_context: presentation("获取数据口径", "获取企业数据查询所需的表结构、口径和示例。", "查询问题", "正在获取数据口径…", "已获取数据口径", "数据口径获取失败", "knowledge"),
  enterprise_sql_query: presentation("查询企业数据", "执行经过权限过滤的只读企业数据查询。", "SQL 查询", "正在查询企业数据…", "已查询企业数据", "企业数据查询失败", "knowledge"),
  enterprise_document_search: presentation("检索企业知识库", "在已授权的企业知识库中查找资料。", "检索条件", "正在检索企业知识库…", "已检索企业知识库", "企业知识检索失败", "knowledge"),
  personal_knowledge_search: presentation("检索个人知识库", "在当前用户有权访问的个人知识库中查找资料。", "检索条件", "正在检索个人知识库…", "已检索个人知识库", "个人知识检索失败", "knowledge"),
  read_primer: presentation("读取知识说明", "读取当前任务所需的知识说明和执行规范。", "说明文件", "正在读取知识说明…", "已读取知识说明", "知识说明读取失败", "knowledge"),
  primer_read: presentation("读取知识说明", "读取当前任务所需的知识说明和执行规范。", "说明文件", "正在读取知识说明…", "已读取知识说明", "知识说明读取失败", "knowledge"),
  load_primer: presentation("读取知识说明", "读取当前任务所需的知识说明和执行规范。", "说明文件", "正在读取知识说明…", "已读取知识说明", "知识说明读取失败", "knowledge"),
  skill_view: presentation("查看能力说明", "读取指定能力的使用说明和操作规范。", "查看能力", "正在查看能力说明…", "已查看能力说明", "查看能力说明失败", "generic"),
  todo: presentation("更新任务步骤", "记录当前任务的执行步骤与完成状态。", "任务步骤", "正在更新任务步骤…", "任务步骤已更新", "任务步骤更新失败", "generic"),
  patch: presentation("修改文件", "按指定位置更新当前任务工作区中的文件。", "修改目标", "正在修改文件…", "文件已修改", "文件修改失败", "file"),
  execute_code: presentation("执行代码", "在隔离运行环境中运行代码以处理或验证内容。", "执行代码", "正在执行代码…", "代码已执行", "代码执行失败", "shell"),
  view_image: presentation("查看图像", "查看任务中的图像以进行内容或视觉检查。", "查看图像", "正在查看图像…", "已查看图像", "查看图像失败", "file"),
  shell: presentation("执行命令", "在隔离运行环境中执行命令。", "执行命令", "正在执行命令…", "已执行命令", "执行命令失败", "shell"),
  terminal: presentation("执行命令", "在隔离运行环境中执行命令。", "执行命令", "正在执行命令…", "已执行命令", "执行命令失败", "shell"),
  exec_command: presentation("执行命令", "在隔离运行环境中执行命令。", "执行命令", "正在执行命令…", "已执行命令", "执行命令失败", "shell"),
  delegate_task: presentation("分派后台任务", "将可独立执行的工作交给后台数字人处理。", "分派内容", "正在分派后台任务…", "已分派后台任务", "后台任务分派失败", "delegate")
};

const prefixPresentations: Array<[string, ToolPresentation]> = [
  ["enterprise_", presentation("查询企业数据", "在已授权的企业数据源中查询信息。", "检索条件", "正在查询企业数据…", "已查询企业数据", "企业数据查询失败", "knowledge")],
  ["search_file", exactPresentations.search_files],
  ["find_file", exactPresentations.search_files],
  ["read_excel", exactPresentations.read_excel],
  ["read_spreadsheet", exactPresentations.read_excel],
  ["read_", exactPresentations.read_file],
  ["write_", exactPresentations.write_file],
  ["save_", exactPresentations.write_file],
  ["web_search", exactPresentations.web_search],
  ["search_web", exactPresentations.web_search],
  ["delegate", exactPresentations.delegate_task]
];

const genericPresentation = presentation("执行操作", "执行已授权的任务操作。", "执行内容", "正在执行操作…", "操作已完成", "操作失败", "generic");

function presentation(
  displayName: string,
  description: string,
  inputLabel: string,
  runningText: string,
  completedText: string,
  failedText: string,
  category: ToolCategory
): ToolPresentation {
  return { displayName, description, inputLabel, runningText, completedText, failedText, category };
}

export function presentationForTool(name: string): ToolPresentation {
  const normalized = name.trim().toLowerCase();
  const direct = presentationForNormalizedName(normalized);
  if (direct) return direct;
  // MCP providers namespace tools as mcp__<server>__<tool>. Resolve the
  // trailing tool name so the public card remains user-facing instead of
  // falling back to the opaque generic "执行操作" label.
  const candidates = normalized.split(/__|[.:/]/).filter(Boolean).reverse();
  for (const candidate of candidates) {
    const presentation = presentationForNormalizedName(candidate);
    if (presentation) return presentation;
  }
  return genericPresentation;
}

function presentationForNormalizedName(normalized: string): ToolPresentation | undefined {
  if (exactPresentations[normalized]) return exactPresentations[normalized];
  const prefixed = prefixPresentations.find(([prefix]) => normalized.startsWith(prefix))?.[1];
  if (prefixed) return prefixed;
  // Some Hermes/plugin adapters add an action prefix (for example
  // get_enterprise_sql_context). Keep those names user-facing as well.
  if (normalized.includes("sql") && normalized.includes("context")) return exactPresentations.enterprise_sql_context;
  if (normalized.includes("sql") && (normalized.includes("query") || normalized.includes("execute"))) return exactPresentations.enterprise_sql_query;
  if (normalized.includes("document") && normalized.includes("search")) return exactPresentations.enterprise_document_search;
  if (normalized.includes("knowledge") && (normalized.includes("read") || normalized.includes("load"))) return exactPresentations.enterprise_knowledge_read;
  if (normalized.includes("primer")) return exactPresentations.read_primer;
  if (normalized.includes("tool") && (normalized.includes("describe") || normalized.includes("schema"))) return exactPresentations.tool_describe;
  if (normalized.includes("tool") && (normalized.includes("search") || normalized.includes("list"))) return exactPresentations.tool_search;
  return undefined;
}

export function statusText(message: ToolMessage, tool = presentationForTool(message.name)): string {
  return message.status === "running" ? tool.runningText : message.status === "failed" ? tool.failedText : tool.completedText;
}

const MAX_TRACE_SUMMARY_CHARS = 96;

function traceActionForTool(message: ToolMessage): string {
  const name = message.name.trim().toLowerCase();
  if (presentationForTool(message.name).displayName === "查看工具说明" || name.includes("list_tools") || name.includes("tool_search")) return "查看";
  if (name.includes("sql_context")) return "获取";
  if (name.includes("knowledge_read") || name.includes("primer_read") || name.includes("read_primer") || name.includes("load_primer")) return "读取";
  const category = presentationForTool(message.name).category;
  if (category === "web" || category === "search" || category === "knowledge") return "查询";
  if (category === "shell") return "执行";
  if (category === "delegate") return "分派";
  if (category === "sheet") return "读取";
  if (category === "file") {
    const name = message.name.trim().toLowerCase();
    return name.startsWith("write_") || name.startsWith("save_") || name === "patch" ? "保存" : "读取";
  }
  return "执行";
}

function compactTraceInput(value: string): string | null {
  const compact = value.replace(/\s+/g, " ").trim();
  if (!compact || compact === "已提交参数" || compact === "已提交代码") return null;
  return compact.length > MAX_TRACE_SUMMARY_CHARS
    ? `${compact.slice(0, MAX_TRACE_SUMMARY_CHARS - 1)}…`
    : compact;
}

/** User-facing one-line tool summary; detailed and technical fields remain in the expandable card. */
export function traceLabelForTool(message: ToolMessage): string {
  const tool = presentationForTool(message.name);
  const detail = detailForTool(message);
  const input = detail.input ? compactTraceInput(detail.input) : null;
  // Skill identifiers are already part of headerNameForTool; repeating them adds noise.
  if (!input || message.name.trim().toLowerCase() === "skill_view") {
    return statusText(message, tool);
  }
  const prefix = message.status === "running" ? "正在调用" : message.status === "failed" ? "调用失败" : "调用";
  return `${prefix}${headerNameForTool(message)}，${traceActionForTool(message)}“${input}”`;
}

function normalizedKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function sensitiveKey(value: string): boolean {
  const normalized = normalizedKey(value);
  return SENSITIVE_KEYS.some((part) => normalized.includes(part));
}

function privateHostname(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (host === "localhost" || host === "::1" || host.endsWith(".local") || host.endsWith(".internal") || host.endsWith(".svc")) return true;
  const parts = host.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part))) return host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80:");
  return parts[0] === 10
    || parts[0] === 127
    || (parts[0] === 169 && parts[1] === 254)
    || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)
    || (parts[0] === 192 && parts[1] === 168);
}

function redactUrl(raw: string): string {
  const trailingMatch = raw.match(/[.,;:!?\)\]}]+$/);
  const trailing = trailingMatch?.[0] ?? "";
  const candidate = trailing ? raw.slice(0, -trailing.length) : raw;
  try {
    const url = new URL(candidate);
    const base = privateHostname(url.hostname) ? "<endpoint>" : `${url.protocol}//${url.hostname}${url.port ? `:${url.port}` : ""}`;
    const query = [...url.searchParams.entries()].map(([key, value]) => `${encodeURIComponent(key)}=${sensitiveKey(key) ? REDACTED : encodeURIComponent(value)}`).join("&");
    return `${base}${url.pathname}${query ? `?${query}` : ""}${trailing}`;
  } catch {
    return `<endpoint>${trailing}`;
  }
}

function redactText(value: string): string {
  return value
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, REDACTED)
    .replace(/\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{2,}\b/g, REDACTED)
    .replace(/\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b/gi, REDACTED)
    .replace(/https?:\/\/[^\s"'<>]+/gi, redactUrl)
    .replace(/\b[A-Za-z]:\\(?:[^\\\s"'<>]+\\)*[^\\\s"'<>]+/g, (path) => `<path>/${path.split("\\").pop() || "文件"}`)
    .replace(/(?<![A-Za-z0-9_>:/])\/(?:[^/\s"'<>]+\/)+[^/\s"'<>]+/g, (path) => `<path>/${path.split("/").pop() || "文件"}`);
}

function sanitizeValue(value: unknown, depth: number): unknown {
  if (Array.isArray(value)) {
    if (depth >= MAX_DETAIL_DEPTH) return DEPTH_LIMIT;
    return value.map((item) => sanitizeValue(item, depth + 1));
  }
  if (value && typeof value === "object") {
    if (depth >= MAX_DETAIL_DEPTH) return DEPTH_LIMIT;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, sensitiveKey(key) ? REDACTED : sanitizeValue(item, depth + 1)]));
  }
  return typeof value === "string" ? redactText(value) : value;
}

function truncate(value: string): { value: string; truncated: boolean } {
  if (value.length <= MAX_DETAIL_CHARS) return { value, truncated: false };
  return { value: `${value.slice(0, MAX_DETAIL_CHARS)}\n${TRUNCATED}`, truncated: true };
}

function parseJson(value: string, allowString = false): unknown | undefined {
  const trimmed = value.trim();
  if (!trimmed || !["{", "["].includes(trimmed[0]) && !(allowString && trimmed[0] === "\"")) return undefined;
  try {
    return JSON.parse(trimmed);
  } catch {
    return undefined;
  }
}

function unwrappedJson(value: string): unknown | undefined {
  const parsed = parseJson(value, true);
  if (typeof parsed !== "string") return parsed;
  return parseJson(parsed) ?? parsed;
}

function sanitizedDetail(value: string): { formatted: string; parsed?: unknown; truncated: boolean } {
  const parsed = unwrappedJson(value);
  if (parsed !== undefined) {
    const safe = sanitizeValue(parsed, 0);
    const limited = truncate(JSON.stringify(safe, null, 2));
    return { formatted: limited.value, parsed: safe, truncated: limited.truncated };
  }
  const limited = truncate(redactText(value));
  return { formatted: limited.value, truncated: limited.truncated };
}

export function sanitizeToolDetail(value: string): string {
  return sanitizedDetail(value).formatted;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function countValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return Math.max(0, Math.round(value));
  return Array.isArray(value) ? value.length : null;
}

function safeSummaryText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const safe = redactText(value).replace(/\s+/g, " ").trim();
  return safe ? safe.slice(0, 100) : null;
}

function filename(value: unknown): string | null {
  const safe = safeSummaryText(value);
  if (!safe) return null;
  return safe.split(/[\\/]/).pop()?.replace(/^<path>/, "") || null;
}

function summaryFor(message: ToolMessage, parsed: unknown): string | null {
  const data = record(parsed);
  if (!data) return null;
  if (message.status === "failed") {
    return safeSummaryText(data.error ?? data.message ?? data.detail);
  }
  const name = message.name.toLowerCase();
  if (name.includes("excel") || name.includes("sheet")) {
    const rows = countValue(data.rows ?? data.rowCount ?? data.row_count);
    if (rows !== null) return `已读取 ${rows} 行`;
  }
  if (name.includes("search") && name.includes("file")) {
    const files = countValue(data.matches ?? data.files ?? data.items);
    if (files !== null) return `找到 ${files} 个文件`;
  }
  if (name.includes("web") || presentationForTool(name).category === "web") {
    const results = countValue(data.results ?? data.items);
    if (results !== null) return `找到 ${results} 条相关结果`;
  }
  if (name.startsWith("write_") || name.startsWith("save_")) {
    const saved = filename(data.path ?? data.file ?? data.filename ?? data.name);
    if (saved) return `已保存 ${saved}`;
  }
  return null;
}

export function detailForTool(message: ToolMessage): ToolDetailPresentation {
  const rawInput = message.input;
  const rawResult = message.result || message.preview;
  if (!rawInput && !rawResult) return { input: null, context: null, result: null, formatted: null, summary: null, truncated: false };
  const inputDetail = rawInput ? sanitizedDetail(rawInput) : undefined;
  const resultDetail = rawResult ? sanitizedDetail(rawResult) : undefined;
  const input = readableInput(message, inputDetail?.parsed, inputDetail?.formatted);
  const context = skillViewTarget(message)?.filePath ?? null;
  const result = readableResult(message, resultDetail?.parsed, resultDetail?.formatted);
  const formatted = technicalDetail(inputDetail, resultDetail);
  return {
    input,
    context,
    result,
    formatted,
    summary: summaryFor(message, resultDetail?.parsed),
    truncated: Boolean(inputDetail?.truncated || resultDetail?.truncated)
  };
}

export function headerNameForTool(message: ToolMessage): string {
  const tool = presentationForTool(message.name);
  if (message.name.trim().toLowerCase() !== "skill_view") return tool.displayName;
  const target = skillViewTarget(message);
  if (!target?.name) return tool.displayName;
  return target.filePath ? `查看 ${target.name} 技能 · ${target.filePath}` : `查看 ${target.name} 技能能力`;
}

function skillViewTarget(message: ToolMessage): { name: string; filePath?: string } | null {
  if (message.name.trim().toLowerCase() !== "skill_view" || !message.input) return null;
  const data = record(unwrappedJson(message.input));
  const name = firstText(data, ["name", "skill", "skill_name"]);
  if (!name) return null;
  const filePath = firstText(data, ["file_path"]);
  return filePath ? { name, filePath } : { name };
}

function firstText(data: Record<string, unknown> | null, keys: string[]): string | null {
  if (!data) return null;
  for (const key of keys) {
    const value = data[key];
    if (typeof value === "string" && value.trim()) return redactText(value);
  }
  return null;
}

function readableInput(message: ToolMessage, parsed: unknown, fallback?: string): string | null {
  const data = toolArgumentRecord(parsed);
  const name = message.name.toLowerCase();
  if (name === "todo") {
    const todos = data?.todos;
    return Array.isArray(todos) ? `更新 ${todos.length} 个任务步骤` : "更新任务步骤";
  }
  if (name === "execute_code") return "已提交代码";
  if (name.includes("knowledge_read") || name.includes("primer_read") || name.includes("read_primer") || name.includes("load_primer")) {
    return firstText(data, ["relative_path", "file_path", "path", "name"]) ?? (data ? "已提交参数" : fallback ?? null);
  }
  if (name.includes("sql_context")) {
    return firstText(data, ["query", "question", "domain"]) ?? (data ? "已提交参数" : fallback ?? null);
  }
  if (name.includes("sql_query")) {
    return firstText(data, ["sql", "query"]) ?? (data ? "已提交参数" : fallback ?? null);
  }
  if (name.includes("document_search") || name.includes("knowledge_search")) {
    return firstText(data, ["question", "query", "q", "search"]) ?? (data ? "已提交参数" : fallback ?? null);
  }
  const category = presentationForTool(message.name).category;
  const keys = category === "shell" ? ["command", "cmd"]
    : category === "search" || category === "web" || category === "knowledge" ? ["query", "q", "pattern", "search"]
      : category === "file" || category === "sheet" ? ["path", "file", "filename", "file_path"]
        : name === "skill_view" ? ["name", "skill", "skill_name"]
          : ["name", "path", "query", "command"];
  return firstText(data, keys) ?? (data ? "已提交参数" : fallback ?? null);
}

function toolArgumentRecord(value: unknown): Record<string, unknown> | null {
  const data = record(value);
  if (!data) return null;
  for (const key of ["arguments", "args", "input", "params", "parameters"]) {
    const nested = record(data[key]);
    if (nested) return nested;
  }
  return data;
}

function readableResult(message: ToolMessage, parsed: unknown, fallback?: string): string | null {
  const data = record(parsed);
  const failure = firstText(data, ["error", "message", "detail"]);
  if (failure) return failure;
  const name = message.name.toLowerCase();
  if (message.status === "running") return null;
  if (presentationForTool(message.name).category === "shell") {
    return firstText(data, ["output", "stdout", "result"]) ?? fallback ?? null;
  }
  if (name === "skill_view") return "已读取能力说明";
  if (name === "todo") return "任务步骤已更新";
  if (name === "patch") return "文件已修改";
  if (name === "read_file" || name === "read_excel" || name === "view_image") return "已读取内容";
  if (name === "write_file") return "文件已保存";
  return summaryFor(message, parsed) ?? (data ? statusText(message) : fallback ?? null);
}

function technicalValue(detail?: { parsed?: unknown; formatted: string }): unknown {
  return detail?.parsed ?? detail?.formatted;
}

function technicalDetail(
  input?: { parsed?: unknown; formatted: string },
  result?: { parsed?: unknown; formatted: string }
): string | null {
  const detail: Record<string, unknown> = {};
  if (input) detail["调用参数"] = technicalValue(input);
  if (result) detail["执行结果"] = technicalValue(result);
  return Object.keys(detail).length ? JSON.stringify(sanitizeValue(detail, 0), null, 2) : null;
}

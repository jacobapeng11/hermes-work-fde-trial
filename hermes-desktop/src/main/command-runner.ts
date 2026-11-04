import { spawn } from "node:child_process";
import { realpath } from "node:fs/promises";
import { homedir } from "node:os";
import { posix, resolve, win32 } from "node:path";
import { builtinPythonDir } from "./builtin-python";
import { classifyLocalPath } from "./workspace-files";

const MAX_OUTPUT = 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 120_000;

export interface CommandResult {
  exitCode: number | null;
  signal: string | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

export function validateCommand(command: unknown): string {
  if (typeof command !== "string" || command.trim().length === 0 || command.length > 16_384 || command.includes("\0")) {
    throw new Error("命令不能为空、包含无效字符或过长");
  }
  return command;
}

// Environment lookups on Windows are case-insensitive; normalize manually so
// win32-mode scans behave identically wherever the tests run.
function lookupEnvValue(name: string): string | undefined {
  const direct = process.env[name];
  if (direct !== undefined) return direct;
  const target = name.toLowerCase();
  for (const [key, value] of Object.entries(process.env)) {
    if (key.toLowerCase() === target) return value;
  }
  return undefined;
}

// Non-literal path spellings expand to their literal form before scanning:
// `%VAR%` in cmd and `$env:VAR` / `$HOME` in PowerShell hid the real target —
// an agent reading `%USERPROFILE%\.claude\credentials.json` bypassed the
// external-path reminder entirely. Names that resolve to nothing stay
// verbatim (never fabricate a path); batch forms (`%1`, `%%f`) never match
// the name pattern, so loops stay untouched.
function expandWindowsVariables(text: string): string {
  return text
    .replace(/%([A-Za-z_][A-Za-z0-9_]*)%/g, (whole, name: string) => lookupEnvValue(name) ?? whole)
    .replace(/\$env:([A-Za-z_][A-Za-z0-9_]*)/gi, (whole, name: string) => lookupEnvValue(name) ?? whole)
    .replace(/\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}/gi, (whole, name: string) => lookupEnvValue(name) ?? whole)
    .replace(/\$HOME(?![A-Za-z0-9_])/g, () => lookupEnvValue("USERPROFILE") ?? "$HOME");
}

function expandPosixVariables(text: string): string {
  // Both `$HOME/...` and the braced `${HOME}/...` are first-class shell
  // spellings on macOS/Linux; expand either to the real home directory.
  return text
    .replace(/\$\{HOME\}/g, () => homedir())
    .replace(/\$HOME(?![A-Za-z0-9_])/g, () => homedir());
}

// Design stance for the external-path scan below: it is a REMINDER layer,
// not a security layer. Shell and interpreter code are Turing-complete —
// paths can be built at runtime, so any static scan has both false
// positives and false negatives. The real boundaries are the command
// timeout, the audit trail and the user's trust in the agent. When in
// doubt, prefer false negatives (audited) over false positives (they
// interrupt every task with a confusing approval card). Token shapes are
// platform-aware: Windows scans drive-letter and UNC references only (a
// bare `/x` is a cmd flag or literal — `cd /d`, `robocopy /XO`), while
// macOS/Linux scan posix-rooted and `~/` references, which are first-class
// in sh/zsh.
export function referencedPaths(command: string, platform: NodeJS.Platform = process.platform): string[] {
  // URLs are not filesystem references. Keep the path scan intentionally
  // conservative; shell syntax cannot be fully analyzed without executing it.
  const stripped = command
    .replace(/https?:\/\/[^\s"'<>]+/gi, " ")
    // Inline interpreter code (`python -c "..."`, `node -e '...'`) is program
    // logic, not shell file references. Windows paths with spaces inside it
    // (`r'C:\Program Files\...'`) get truncated at the space and expanduser
    // fragments (`'/Pictures/*'`) look like posix roots — the two main
    // false-positive sources — so the quoted code body is not scanned.
    .replace(/(?:^|[\s=(])-[ce](?:\s*=|\s+)(?:"[^"]*"|'[^']*')/g, " ");
  // Variable expansion runs after the code-body strip (interpreter strings
  // are not shell references) and before quote masking, so an expanded
  // `C:\Program Files\...` still enjoys space-in-quotes protection.
  const text = (platform === "win32" ? expandWindowsVariables(stripped) : expandPosixVariables(stripped))
    // Spaces inside a quoted segment may belong to one path; mask them so
    // scanners keep `C:\Program Files\...` as a single token.
    .replace(/"[^"]*"|'[^']*'/g, (quoted) => quoted.replace(/\s+/g, "\u0000"))
    // Do not join a drive root with a language escape such as `\n` from
    // `"D:\\"` followed by `"\\n". Keep escapes embedded in a real name.
    .replace(/\\[nrtbfv0](?=$|[\s"'()[\]{}.,;:+|&])/g, " ");
  // Keep the suffix optional so a drive root such as `D:/` or `C:\\` is
  // still classified as an outside-workspace path.
  const tokens: string[] = [];
  if (platform === "win32") {
    tokens.push(...(text.match(/(?:[A-Za-z]:[\\/][^\s"'<>|;&]*|\\\\[^\s"'<>|;&]+)/g) ?? []));
  } else {
    // The leading boundary keeps a workspace-relative argument such as
    // `.work_ppt/make.py` from being sliced at the inner slash (that slice
    // resolves against `/` and wrongly raises a card); expanduser-style
    // fragments live inside `-c` code bodies, which are already stripped.
    tokens.push(...(text.match(/(?:^|[\s"'=])\/(?:[A-Za-z_~][^\s"'<>|;&/]*(?:\/[^\s"'<>|;&]+)*)/g)?.map((value) => value.replace(/^[\s"'=]+/, "")) ?? []));
    // `~/x` and bare `~` expand to the home directory in sh/zsh.
    tokens.push(...(text.match(/(?:^|[\s"'=])~(?:\/[^\s"'<>|;&]+)?/g)?.map((value) => value.replace(/^[\s"'=]+/, "").replace(/^~$/, homedir()).replace(/^~\//, `${homedir()}/`)) ?? []));
  }
  const traversal = text.match(/(?:^|[\s"'])((?:\.\.[\\/])+[^\s"']*)/g) ?? [];
  const parentToken = /(?:^|[\s"'])\.\.(?=$|[\s"';&|])/g.test(text) ? [".."] : [];
  return [...tokens, ...traversal.map((value) => value.trim().replace(/^["']/, "")), ...parentToken]
    .map((value) => value.replace(/\u0000/g, " "));
}

/** Return the first explicit absolute path that leaves the selected folder. */
export async function findExternalCommandPath(command: unknown, workspace: string, platform: NodeJS.Platform = process.platform): Promise<string | undefined> {
  const source = validateCommand(command);
  const root = await realpath(workspace);
  for (const raw of referencedPaths(source, platform)) {
    const candidate = platform === "win32"
      ? (win32.isAbsolute(raw) ? win32.resolve(raw) : resolve(root, raw))
      : (posix.isAbsolute(raw) ? posix.resolve(raw) : resolve(root, raw));
    if (await classifyLocalPath(root, candidate) === "external") return candidate;
  }
  return undefined;
}

// Commands inherit the user PATH plus the builtin Python appended LAST, so a
// system Python still shadows the embeddable one and only Python-less
// machines fall through to it. Exported for the spawn-options test.
export function commandEnvironment(): NodeJS.ProcessEnv | undefined {
  const pythonDir = builtinPythonDir();
  if (!pythonDir) return undefined;
  return { ...process.env, PATH: `${process.env.PATH ?? ""};${pythonDir}` };
}

export async function runWorkspaceCommand(command: unknown, workspace: string, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<CommandResult> {
  const source = validateCommand(command);
  const root = await realpath(workspace);
  const child = spawn(source, {
    cwd: root,
    shell: true,
    windowsHide: true,
    detached: process.platform !== "win32",
    stdio: ["ignore", "pipe", "pipe"],
    env: commandEnvironment(),
  });
  let stdout = "";
  let stderr = "";
  const append = (current: string, chunk: Buffer): string => (current + chunk.toString("utf8")).slice(-MAX_OUTPUT);
  child.stdout.on("data", (chunk: Buffer) => { stdout = append(stdout, chunk); });
  child.stderr.on("data", (chunk: Buffer) => { stderr = append(stderr, chunk); });
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    if (process.platform === "win32" && child.pid) spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true });
    else child.kill("SIGTERM");
  }, Math.max(1_000, Math.min(timeoutMs, DEFAULT_TIMEOUT_MS)));
  return new Promise((resolvePromise, reject) => {
    child.once("error", reject);
    child.once("close", (exitCode, signal) => {
      clearTimeout(timer);
      resolvePromise({ exitCode, signal, stdout, stderr, timedOut });
    });
  });
}

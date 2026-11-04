import { randomUUID } from "node:crypto";
import { lstat, mkdir, open, readdir, realpath, rename, rm, stat } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve, win32 } from "node:path";

const MAX_TEXT_BYTES = 10 * 1024 * 1024;

function inside(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel === "" || (!isAbsolute(rel) && rel !== ".." && !rel.startsWith("..\\") && !rel.startsWith("../"));
}

function validateRelative(raw: unknown, allowRoot: boolean): string {
  if (typeof raw !== "string" || raw.includes("\0") || raw.includes(":")) throw new Error("路径无效");
  if (isAbsolute(raw) || win32.isAbsolute(raw)) throw new Error("只接受工作文件夹内的相对路径");
  if (raw.split(/[\\/]/).some((part) => part === "..")) throw new Error("不允许父目录路径");
  if (!allowRoot && (!raw.trim() || raw === ".")) throw new Error("不能修改工作文件夹本身");
  return raw;
}

export async function resolveWorkspacePath(rootPath: string, raw: unknown, options: { missing?: boolean; root?: boolean } = {}): Promise<string> {
  const rel = validateRelative(raw, options.root ?? false);
  const root = await realpath(rootPath);
  const candidate = resolve(root, rel);
  if (!inside(root, candidate) || (!(options.root ?? false) && candidate === root)) throw new Error("路径超出工作文件夹");
  let probe = candidate;
  while (true) {
    try {
      const resolved = await realpath(probe);
      if (!inside(root, resolved)) throw new Error("链接指向工作文件夹外部");
      if (probe === candidate) {
        if (!(options.root ?? false) && resolved === root) throw new Error("不能修改工作文件夹本身");
        return resolved;
      }
      return candidate;
    } catch (error) {
      if (!options.missing || (error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      // A dangling symlink is not a missing path: never create through it.
      try { await lstat(probe); throw new Error("目标链接不可用"); }
      catch (entryError) { if ((entryError as NodeJS.ErrnoException).code !== "ENOENT") throw entryError; }
      const parent = dirname(probe);
      if (parent === probe) throw error;
      probe = parent;
    }
  }
}

export type LocalPathScope = "workspace" | "external";

/**
 * Classify a path before executing a capability. Explicit absolute paths and
 * parent traversal are allowed to proceed to the chat approval flow; they are
 * not rejected at the protocol boundary.
 */
export async function classifyLocalPath(rootPath: string, raw: unknown): Promise<LocalPathScope> {
  if (typeof raw !== "string" || raw.includes("\0")) throw new Error("路径无效");
  const root = await realpath(rootPath);
  if (!raw.trim() || raw === ".") return "workspace";
  const candidate = isAbsolute(raw) || win32.isAbsolute(raw) ? resolve(raw) : resolve(root, raw);
  let resolved = candidate;
  try {
    resolved = await realpath(candidate);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    // Missing paths resolve through the nearest existing parent so workspace
    // writes to new files still classify correctly. For unresolvable paths
    // (EPERM/EACCES/EBUSY — e.g. kernel-locked files such as DumpStack.log)
    // fall back to pure geometry instead of throwing: throwing here used to
    // turn protected-file references into silent capability failures that
    // never reached the approval card the user should have seen.
    if (code === "ENOENT") {
      let probe = dirname(candidate);
      while (probe !== dirname(probe)) {
        try {
          resolved = join(await realpath(probe), basename(candidate));
          break;
        } catch (parentError) {
          if ((parentError as NodeJS.ErrnoException).code !== "ENOENT") break;
          probe = dirname(probe);
        }
      }
    }
  }
  return inside(root, resolved) ? "workspace" : "external";
}

/** Resolve either a workspace-relative path or an explicitly approved path. */
export async function resolveLocalPath(
  rootPath: string,
  raw: unknown,
  options: { missing?: boolean; root?: boolean } = {}
): Promise<{ target: string; scope: LocalPathScope }> {
  const scope = await classifyLocalPath(rootPath, raw);
  if (scope === "workspace") {
    const workspace = await realpath(rootPath);
    const normalizedRaw = typeof raw === "string" && (isAbsolute(raw) || win32.isAbsolute(raw))
      ? relative(workspace, resolve(raw))
      : raw;
    return { target: await resolveWorkspacePath(workspace, normalizedRaw, options), scope };
  }
  if (typeof raw !== "string") throw new Error("路径无效");
  const root = await realpath(rootPath);
  const candidate = isAbsolute(raw) || win32.isAbsolute(raw) ? resolve(raw) : resolve(root, raw);
  let target = candidate;
  try {
    target = await realpath(candidate);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT" || !options.missing) throw error;
    let probe = dirname(candidate);
    let suffix = basename(candidate);
    while (probe !== dirname(probe)) {
      try {
        target = join(await realpath(probe), suffix);
        break;
      } catch (parentError) {
        if ((parentError as NodeJS.ErrnoException).code !== "ENOENT") throw parentError;
        suffix = join(basename(probe), suffix);
        probe = dirname(probe);
      }
    }
  }
  if ((options.root ?? false) && target === root) throw new Error("不能修改工作文件夹本身");
  return { target, scope };
}

async function listDirectoryAt(target: string) {
  const entries = await readdir(target, { withFileTypes: true });
  const visible = await Promise.all(entries.map(async (entry) => {
    try {
      const entryPath = join(target, entry.name);
      const resolved = await realpath(entryPath);
      const info = await stat(resolved);
      if (!info.isFile() && !info.isDirectory()) return null;
      return { name: entry.name, kind: info.isDirectory() ? "directory" as const : "file" as const, size: info.size };
    } catch { return null; }
  }));
  return visible.filter((entry): entry is NonNullable<typeof entry> => entry !== null)
    .sort((a, b) => Number(b.kind === "directory") - Number(a.kind === "directory") || a.name.localeCompare(b.name));
}

export async function listWorkspaceDirectory(root: string, raw: unknown) {
  const target = await resolveWorkspacePath(root, raw, { root: true });
  const entries = await readdir(target, { withFileTypes: true });
  const visible = await Promise.all(entries.map(async (entry) => {
    try {
      // Skip outward links rather than leaking external size or type information.
      const resolved = await resolveWorkspacePath(root, relative(root, join(target, entry.name)), { root: true });
      const info = await stat(resolved);
      if (!info.isFile() && !info.isDirectory()) return null;
      return { name: entry.name, kind: info.isDirectory() ? "directory" as const : "file" as const, size: info.size };
    } catch { return null; }
  }));
  return visible.filter((entry): entry is NonNullable<typeof entry> => entry !== null)
    .sort((a, b) => Number(b.kind === "directory") - Number(a.kind === "directory") || a.name.localeCompare(b.name));
}

export async function listLocalDirectory(root: string, raw: unknown) {
  const { target } = await resolveLocalPath(root, raw, { root: true });
  return listDirectoryAt(target);
}

export async function readWorkspaceText(root: string, raw: unknown): Promise<string> {
  const target = await resolveWorkspacePath(root, raw);
  const file = await open(target, "r");
  try {
    const info = await file.stat();
    if (!info.isFile() || info.size > MAX_TEXT_BYTES) throw new Error("只支持不超过 10 MiB 的文本文件");
    const buffer = Buffer.alloc(MAX_TEXT_BYTES + 1);
    let length = 0;
    while (length < buffer.length) {
      const result = await file.read(buffer, length, buffer.length - length, length);
      if (!result.bytesRead) break;
      length += result.bytesRead;
    }
    if (length > MAX_TEXT_BYTES || buffer.subarray(0, length).includes(0)) throw new Error("文件过大或不是文本文件");
    try { return new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, length)); }
    catch { throw new Error("文件不是 UTF-8 文本"); }
  } finally { await file.close(); }
}

export async function readLocalText(root: string, raw: unknown): Promise<string> {
  const { target } = await resolveLocalPath(root, raw);
  const file = await open(target, "r");
  try {
    const info = await file.stat();
    if (!info.isFile() || info.size > MAX_TEXT_BYTES) throw new Error("只支持不超过 10 MiB 的文本文件");
    const buffer = Buffer.alloc(MAX_TEXT_BYTES + 1);
    let length = 0;
    while (length < buffer.length) {
      const result = await file.read(buffer, length, buffer.length - length, length);
      if (!result.bytesRead) break;
      length += result.bytesRead;
    }
    if (length > MAX_TEXT_BYTES || buffer.subarray(0, length).includes(0)) throw new Error("文件过大或不是文本文件");
    try { return new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, length)); }
    catch { throw new Error("文件不是 UTF-8 文本"); }
  } finally { await file.close(); }
}

export async function writeWorkspaceText(root: string, raw: unknown, content: unknown): Promise<void> {
  if (typeof content !== "string" || Buffer.byteLength(content, "utf8") > MAX_TEXT_BYTES) throw new Error("文本无效或超过 10 MiB");
  const target = await resolveWorkspacePath(root, raw, { missing: true });
  let parent: string;
  try {
    parent = await realpath(dirname(target));
  } catch (error) {
    // Writing into a not-yet-existing subdirectory used to surface as a raw
    // ENOENT that pushed agents toward the cloud write path. The missing
    // probe above already anchors the target inside the workspace, so
    // creating the parent chain here keeps local generation frictionless.
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    await mkdir(dirname(target), { recursive: true });
    parent = await realpath(dirname(target));
  }
  const parentRelative = relative(await realpath(root), parent);
  await resolveWorkspacePath(root, parentRelative, { root: true });
  let mode = 0o600;
  try {
    const info = await stat(target);
    if (!info.isFile()) throw new Error("目标不是普通文件");
    mode = info.mode;
  } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  const temporary = join(parent, `.${basename(target)}.${randomUUID()}.tmp`);
  const file = await open(temporary, "wx", mode);
  try {
    await file.writeFile(content, "utf8");
    await file.sync();
  } catch (error) { await rm(temporary, { force: true }); throw error; }
  finally { await file.close(); }
  try {
    const checked = await resolveWorkspacePath(root, raw, { missing: true });
    if (checked !== target) throw new Error("路径在操作期间发生变化，请重试");
    await rename(temporary, target);
  } finally { await rm(temporary, { force: true }); }
}

export async function writeLocalText(root: string, raw: unknown, content: unknown): Promise<void> {
  if (typeof content !== "string" || Buffer.byteLength(content, "utf8") > MAX_TEXT_BYTES) throw new Error("文本无效或超过 10 MiB");
  const { target } = await resolveLocalPath(root, raw, { missing: true });
  let parent: string;
  try {
    parent = await realpath(dirname(target));
  } catch (error) {
    // Approved outside writes deserve the same auto-parent behavior; the
    // approval card has already been answered by the time this runs.
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    await mkdir(dirname(target), { recursive: true });
    parent = await realpath(dirname(target));
  }
  let mode = 0o600;
  try {
    const info = await stat(target);
    if (!info.isFile()) throw new Error("目标不是普通文件");
    mode = info.mode;
  } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  const temporary = join(parent, `.${basename(target)}.${randomUUID()}.tmp`);
  const file = await open(temporary, "wx", mode);
  try {
    await file.writeFile(content, "utf8");
    await file.sync();
  } catch (error) { await rm(temporary, { force: true }); throw error; }
  finally { await file.close(); }
  try { await rename(temporary, target); }
  finally { await rm(temporary, { force: true }); }
}

export async function createWorkspaceDirectory(root: string, raw: unknown): Promise<void> {
  const target = await resolveWorkspacePath(root, raw, { missing: true });
  await mkdir(target, { recursive: true });
  await resolveWorkspacePath(root, raw);
}

export async function createLocalDirectory(root: string, raw: unknown): Promise<void> {
  const { target } = await resolveLocalPath(root, raw, { missing: true });
  await mkdir(target, { recursive: true });
}

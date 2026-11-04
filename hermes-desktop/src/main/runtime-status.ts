import { spawn } from "node:child_process";
import { join } from "node:path";
import { builtinPythonDir } from "./builtin-python";

type RuntimeInfo = { available: boolean; version?: string; path?: string; error?: string };

function probe(executable: string): Promise<RuntimeInfo> {
  return new Promise((resolve) => {
    const child = spawn(executable, ["--version"], { windowsHide: true });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk.toString(); });
    child.stderr.on("data", (chunk) => { output += chunk.toString(); });
    child.once("error", () => resolve({ available: false, error: "未找到" }));
    child.once("close", (code) => {
      const version = output.trim().split(/\r?\n/)[0];
      resolve(code === 0 ? { available: true, version, path: executable } : { available: false, error: version || `退出码 ${code}` });
    });
  });
}

// A usable system Python (or COWORK_PYTHON override) wins; only when that
// probe fails does the bundled embeddable under resources/python answer, so
// the reported status matches what command.run's PATH-fallback would do.
async function probePython(): Promise<RuntimeInfo> {
  const preferred = process.env.COWORK_PYTHON || (process.platform === "win32" ? "python.exe" : "python3");
  const system = await probe(preferred);
  if (system.available) return system;
  const dir = builtinPythonDir();
  if (dir) {
    const builtin = await probe(join(dir, "python.exe"));
    if (builtin.available) return builtin;
  }
  return system;
}

export async function getRuntimeStatus(): Promise<{ python: RuntimeInfo; node: RuntimeInfo }> {
  const [python, node] = await Promise.all([probePython(), probe("node")]);
  return { python, node };
}

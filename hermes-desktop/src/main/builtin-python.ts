import { existsSync } from "node:fs";
import { dirname, join } from "node:path";

// The Windows installer ships an embeddable CPython under resources/python so
// agent commands keep working on machines without their own Python. It is a
// fallback only: command.run appends this directory AFTER the inherited PATH,
// so a system installation always wins and the builtin fills the gap.
//
// Resolution order (Windows only):
//   1. COWORK_BUILTIN_PYTHON_DIR env override — dev runs point this at
//      build/python to exercise the fallback locally
//   2. <resourcesPath>/python — packaged installs
//
// Deliberately no __dirname-based dev path: this module is loaded by the ESM
// node --test harness where __dirname does not exist, and dev machines have
// their own Python anyway.
export function builtinPythonDir(): string | null {
  if (process.platform !== "win32") return null;
  const override = process.env.COWORK_BUILTIN_PYTHON_DIR;
  if (override) return existsSync(join(override, "python.exe")) ? override : null;
  // process.resourcesPath is undefined outside Electron (node --test), where
  // joining it would throw; treat that as "no builtin".
  const resources = process.resourcesPath;
  if (typeof resources === "string") {
    const packaged = join(resources, "python", "python.exe");
    if (existsSync(packaged)) return dirname(packaged);
  }
  return null;
}

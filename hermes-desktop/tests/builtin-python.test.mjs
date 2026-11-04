import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { builtinPythonDir } from "../src/main/builtin-python.ts";
import { commandEnvironment } from "../src/main/command-runner.ts";

// The builtin embeddable Python is a PATH fallback for Windows installs.
// These cases pin the resolution and injection contract without needing a
// real interpreter: existence checks drive the logic, never execution.

test("COWORK_BUILTIN_PYTHON_DIR resolves when it holds python.exe", async () => {
  const dir = await mkdtemp(join(tmpdir(), "cowork-builtin-python-"));
  try {
    await writeFile(join(dir, "python.exe"), "");
    process.env.COWORK_BUILTIN_PYTHON_DIR = dir;
    try {
      assert.equal(builtinPythonDir(), dir);
      // The injected environment appends the builtin dir AFTER the inherited
      // PATH so a system Python still shadows it.
      const env = commandEnvironment();
      assert.ok(env, "expected an injected environment");
      assert.ok(env.PATH?.endsWith(`;${dir}`), env.PATH ?? "");
      assert.equal(env.PATH?.startsWith(process.env.PATH ?? ""), true);
      // Everything else passes through untouched.
      assert.equal(env.SYSTEMROOT ?? env.SystemRoot, process.env.SYSTEMROOT ?? process.env.SystemRoot);
    } finally {
      delete process.env.COWORK_BUILTIN_PYTHON_DIR;
    }
    assert.equal(commandEnvironment(), undefined, "override cleared");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("an override without python.exe is ignored", async () => {
  const dir = await mkdtemp(join(tmpdir(), "cowork-builtin-python-empty-"));
  try {
    process.env.COWORK_BUILTIN_PYTHON_DIR = dir;
    try {
      assert.equal(builtinPythonDir(), null);
    } finally {
      delete process.env.COWORK_BUILTIN_PYTHON_DIR;
    }
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("without Electron resources or override the builtin stays dormant", () => {
  // node --test runs outside Electron: process.resourcesPath is undefined and
  // no build/python staging exists next to out/, so nothing must inject.
  const previous = process.env.COWORK_BUILTIN_PYTHON_DIR;
  delete process.env.COWORK_BUILTIN_PYTHON_DIR;
  try {
    assert.equal(builtinPythonDir(), null);
    assert.equal(commandEnvironment(), undefined);
  } finally {
    if (previous !== undefined) process.env.COWORK_BUILTIN_PYTHON_DIR = previous;
  }
});

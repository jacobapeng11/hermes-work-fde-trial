import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeLocalText, writeWorkspaceText } from "../src/main/workspace-files.ts";

// Regression: writing into a not-yet-existing subdirectory (the agent flow
// `.work_lightning/gen_lightning.py`) used to throw a raw ENOENT from
// realpath(parent), which pushed agents toward the cloud write path after a
// single local failure. Writes now create the parent chain automatically.

async function makeWorkspace() {
  return mkdtemp(join(tmpdir(), "cowork-workspace-files-"));
}

test("workspace write creates missing parent directories automatically", async () => {
  const workspace = await makeWorkspace();
  await writeWorkspaceText(workspace, ".work_lightning/gen_lightning.py", "print('thunder')");
  const written = await readFile(join(workspace, ".work_lightning", "gen_lightning.py"), "utf8");
  assert.equal(written, "print('thunder')");
});

test("workspace write into a deeper chain still stays inside the root", async () => {
  const workspace = await makeWorkspace();
  await writeWorkspaceText(workspace, "reports/2026/q4/summary.md", "# ok");
  const written = await readFile(join(workspace, "reports", "2026", "q4", "summary.md"), "utf8");
  assert.equal(written, "# ok");
});

test("workspace write refuses traversal even with auto-created parents", async () => {
  const workspace = await makeWorkspace();
  await assert.rejects(() => writeWorkspaceText(workspace, "../escape.txt", "no"));
});

test("local write outside the root also creates missing parents", async () => {
  const workspace = await makeWorkspace();
  const outside = await mkdtemp(join(tmpdir(), "cowork-outside-"));
  const target = join(outside, "nested", "note.txt");
  await writeLocalText(workspace, target, "hello");
  const written = await readFile(target, "utf8");
  assert.equal(written, "hello");
});

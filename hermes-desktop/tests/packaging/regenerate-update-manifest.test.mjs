import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";
import test from "node:test";

test("regenerating an update manifest emits an electron-builder blockmap", async () => {
  const workDir = await mkdtemp(join(tmpdir(), "cowork-update-manifest-"));
  const exePath = join(workDir, "Hermes-Work-Setup-1.2.3.exe");
  const outputDir = join(workDir, "release");
  const installer = Buffer.from(Array.from({ length: 128 * 1024 }, (_, index) => index % 251));
  await writeFile(exePath, installer);

  const result = spawnSync(
    process.execPath,
    [
      "scripts/regenerate-update-manifest.js",
      exePath,
      "--version",
      "1.2.3",
      "--output",
      outputDir,
    ],
    { encoding: "utf8" },
  );

  assert.equal(result.status, 0, result.stderr);

  const blockmap = JSON.parse(gunzipSync(await readFile(join(outputDir, "Hermes-Work-Setup-1.2.3.exe.blockmap"))).toString());
  assert.equal(blockmap.version, "2");
  assert.equal(blockmap.files.length, 1);
  assert.equal(blockmap.files[0].offset, 0);
  assert.equal(blockmap.files[0].sizes.reduce((total, size) => total + size, 0), installer.length);
  assert.ok(blockmap.files[0].checksums.length > 1);

  const sha512 = createHash("sha512").update(installer).digest("base64");
  const manifest = await readFile(join(outputDir, "latest.yml"), "utf8");
  assert.match(manifest, /^version: 1\.2\.3$/m);
  assert.ok(manifest.includes(`sha512: ${sha512}`));
  assert.ok(manifest.includes(`size: ${installer.length}`));
  assert.match(manifest, /^path: Hermes-Work-Setup-1\.2\.3\.exe$/m);
});

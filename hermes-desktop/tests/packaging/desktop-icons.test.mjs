import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const resourceRoot = new URL("../../resources/", import.meta.url);

function readResource(name) {
  return readFileSync(new URL(name, resourceRoot));
}

test("the runtime taskbar PNG is a 512x512 RGBA image", () => {
  const png = readResource("icon.png");
  assert.equal(png.subarray(1, 4).toString("ascii"), "PNG");
  assert.equal(png.readUInt32BE(16), 512);
  assert.equal(png.readUInt32BE(20), 512);
  assert.equal(png[24], 8);
  assert.equal(png[25], 6);
});

test("the Windows ICO contains all taskbar and installer sizes", () => {
  const ico = readResource("icon.ico");
  assert.equal(ico.readUInt16LE(0), 0);
  assert.equal(ico.readUInt16LE(2), 1);
  const count = ico.readUInt16LE(4);
  const entries = Array.from({ length: count }, (_, index) => {
    const offset = 6 + index * 16;
    return {
      width: ico[offset] || 256,
      height: ico[offset + 1] || 256,
      bpp: ico.readUInt16LE(offset + 6)
    };
  });

  assert.deepEqual(
    entries.map((entry) => entry.width),
    [16, 24, 32, 48, 64, 128, 256]
  );
  assert.ok(entries.every((entry) => entry.width === entry.height));
  assert.ok(entries.every((entry) => entry.bpp === 32));
});

test("the macOS ICNS contains Retina application sizes", () => {
  const icns = readResource("icon.icns");
  assert.equal(icns.subarray(0, 4).toString("ascii"), "icns");
  assert.equal(icns.readUInt32BE(4), icns.length);

  const entries = new Set();
  let offset = 8;
  while (offset + 8 <= icns.length) {
    const type = icns.subarray(offset, offset + 4).toString("ascii");
    const length = icns.readUInt32BE(offset + 4);
    assert.ok(length >= 8 && offset + length <= icns.length);
    entries.add(type);
    offset += length;
  }

  assert.deepEqual([...entries].sort(), ["ic07", "ic08", "ic09", "ic10"]);
});

test("all desktop icon entry points reference the shared icon set", () => {
  const builder = readFileSync("electron-builder.yml", "utf8");
  const mainWindow = readFileSync("src/main/index.ts", "utf8");

  assert.match(builder, /icon: resources\/icon\.ico/);
  assert.match(builder, /icon: resources\/icon\.icns/);
  assert.match(builder, /from: resources\/icon\.png/);
  assert.match(mainWindow, /resources\/icon\.png/);
});

test("the Windows installer download URL uses the external brand and version", () => {
  const builder = readFileSync("electron-builder.yml", "utf8");
  const pkg = JSON.parse(readFileSync("package.json", "utf8"));
  const winSection = builder.slice(builder.indexOf("\nwin:"), builder.indexOf("\nnsis:"));

  assert.match(winSection, /target:\s*nsis/);
  assert.doesNotMatch(winSection, /ia32/);
  assert.match(
    builder,
    /artifactName:\s*"Hermes-Work-Setup-\$\{version\}\.\$\{ext\}"/
  );
  assert.match(pkg.version, /^\d+\.\d+\.\d+$/);
  assert.match(
    `https://app.example.com/api/v1/desktop-updates/`
      + `Hermes-Work-Setup-${pkg.version}.exe`,
    /^https:\/\/app\.example\.com\/api\/v1\/desktop-updates\/Hermes-Work-Setup-\d+\.\d+\.\d+\.exe$/
  );
});

test("the macOS installer download URL uses the external brand and version", () => {
  const builder = readFileSync("electron-builder.yml", "utf8");
  const pkg = JSON.parse(readFileSync("package.json", "utf8"));

  const dmgSection = builder.slice(
    builder.indexOf("\ndmg:"),
    builder.indexOf("\nextraResources:")
  );
  assert.match(
    dmgSection,
    /artifactName:\s*"Hermes-Work-Setup-\$\{version\}\.\$\{ext\}"/
  );
  assert.match(pkg.version, /^\d+\.\d+\.\d+$/);
  assert.match(
    `https://app.example.com/api/v1/desktop-updates/`
      + `Hermes-Work-Setup-${pkg.version}.dmg`,
    /^https:\/\/app\.example\.com\/api\/v1\/desktop-updates\/Hermes-Work-Setup-\d+\.\d+\.\d+\.dmg$/
  );
});

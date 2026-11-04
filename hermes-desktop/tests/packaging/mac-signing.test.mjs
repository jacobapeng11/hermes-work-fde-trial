import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const projectRoot = new URL("../../", import.meta.url);

function readProjectFile(path) {
  return readFileSync(new URL(path, projectRoot), "utf8");
}

test("macOS distribution builds require recursive Developer ID code signing", () => {
  const builder = readProjectFile("electron-builder.yml");
  const macSection = builder.slice(builder.indexOf("\nmac:"), builder.indexOf("\ndmg:"));

  assert.match(macSection, /forceCodeSigning:\s*true/);
  assert.match(macSection, /hardenedRuntime:\s*true/);
  assert.match(macSection, /entitlements:\s*entitlements\.mac\.plist/);
  assert.match(
    macSection,
    /entitlementsInherit:\s*entitlements\.mac\.inherit\.plist/
  );
});

test("macOS notarization stays external to the desktop build", () => {
  const builder = readProjectFile("electron-builder.yml");
  const macSection = builder.slice(builder.indexOf("\nmac:"), builder.indexOf("\ndmg:"));
  const macScript = readProjectFile("scripts/build-mac.sh");

  assert.match(macSection, /notarize:\s*false/);
  assert.doesNotMatch(macScript, /\bAPPLE_PASSWORD\b/);
  assert.doesNotMatch(macScript, /\bAPPLE_ID\b/);
  assert.doesNotMatch(macScript, /\bAPPLE_APP_SPECIFIC_PASSWORD\b/);
  assert.doesNotMatch(macScript, /\bAPPLE_TEAM_ID\b/);
});

test("macOS entitlements remain limited to Electron signing requirements", () => {
  const expected = `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n` +
    `<plist version="1.0">\n` +
    `<dict>\n` +
    `  <key>com.apple.security.cs.allow-jit</key>\n` +
    `  <true/>\n` +
    `  <key>com.apple.security.cs.allow-unsigned-executable-memory</key>\n` +
    `  <true/>\n` +
    `  <key>com.apple.security.cs.disable-library-validation</key>\n` +
    `  <true/>\n` +
    `</dict>\n` +
    `</plist>\n`;

  assert.equal(readProjectFile("entitlements.mac.plist"), expected);
  assert.equal(readProjectFile("entitlements.mac.inherit.plist"), expected);
});

test("unsigned macOS builds require an explicit local-only escape hatch", () => {
  const macScript = readProjectFile("scripts/build-mac.sh");

  assert.match(macScript, /ALLOW_UNSIGNED_MAC/);
  assert.match(macScript, /--allow-unsigned/);
  assert.match(macScript, /--config\.mac\.forceCodeSigning=false/);
  assert.match(macScript, /仅生成本机测试包，禁止分发/);
});

test("the macOS build entry accepts explicit certificate arguments", () => {
  const macScript = readProjectFile("scripts/build-mac.sh");

  assert.match(macScript, /--cert/);
  assert.match(macScript, /--cert-password/);
  assert.match(macScript, /export CSC_LINK="\$cert_path"/);
  assert.match(macScript, /export CSC_KEY_PASSWORD="\$cert_password"/);
  assert.match(macScript, /shell history\/进程参数/);
});

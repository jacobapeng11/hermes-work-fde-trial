import test from "node:test";
import assert from "node:assert/strict";
import { LOCAL_CAPABILITY_NAMES, validateCapabilityRequest } from "../src/main/capability-protocol.ts";

test("browser tool parameters accept the plugin's snake_case session_id", () => {
  const request = validateCapabilityRequest({
    requestId: "req-12345678",
    capability: "browser.navigate",
    args: { session_id: "desktop-browser-chrome-mabcdef12", url: "https://example.com" },
  });
  assert.equal(request.args.sessionId, "desktop-browser-chrome-mabcdef12");
  // The original spelling survives so nothing downstream loses information.
  assert.equal(request.args.session_id, "desktop-browser-chrome-mabcdef12");
});

test("browser snake_case arguments normalize generically", () => {
  const request = validateCapabilityRequest({
    requestId: "req-12345678",
    capability: "browser.tab_select",
    args: { session_id: "desktop-browser-chrome-mabcdef12", tab_index: 2, wait_until: "load" },
  });
  assert.equal(request.args.sessionId, "desktop-browser-chrome-mabcdef12");
  assert.equal(request.args.tabIndex, 2);
  assert.equal(request.args.waitUntil, "load");
});

test("camelCase sessionId passes through unchanged", () => {
  const request = validateCapabilityRequest({
    requestId: "req-12345678",
    capability: "browser.snapshot",
    args: { sessionId: "desktop-browser-edge-mabcdef12" },
  });
  assert.equal(request.args.sessionId, "desktop-browser-edge-mabcdef12");
  assert.equal("session_id" in request.args, false);
});

test("existing camelCase keys are never overwritten by normalization", () => {
  const request = validateCapabilityRequest({
    requestId: "req-12345678",
    capability: "browser.wait",
    args: { sessionId: "kept", session_id: "dropped", timeout_ms: 5000 },
  });
  assert.equal(request.args.sessionId, "kept");
  assert.equal(request.args.timeoutMs, 5000);
});

test("non-browser capabilities keep their args untouched", () => {
  const request = validateCapabilityRequest({
    requestId: "req-12345678",
    capability: "workspace.read",
    args: { session_id: "not-a-session-field", path: "notes.txt" },
  });
  assert.equal("sessionId" in request.args, false);
  assert.equal(request.args.path, "notes.txt");
});

test("the capability whitelist covers all 28 registered names", () => {
  assert.equal(LOCAL_CAPABILITY_NAMES.length, 28);
  assert.equal(new Set(LOCAL_CAPABILITY_NAMES).size, 28);
  for (const name of ["browser.read", "browser.wait", "browser.hover", "browser.select", "browser.upload", "browser.downloads", "browser.back", "browser.reload", "browser.tab_list", "browser.tab_new", "browser.tab_select", "browser.tab_close"]) {
    assert.ok(LOCAL_CAPABILITY_NAMES.includes(name), `${name} missing`);
    assert.doesNotThrow(() => validateCapabilityRequest({ requestId: "req-12345678", capability: name, args: {} }));
  }
  assert.throws(() => validateCapabilityRequest({ requestId: "req-12345678", capability: "browser.unknown", args: {} }), /本地能力不受支持/);
});

test("invalid requests are rejected before normalization", () => {
  assert.throws(() => validateCapabilityRequest(null), /本地能力请求无效/);
  assert.throws(() => validateCapabilityRequest({ requestId: "", capability: "browser.navigate", args: {} }), /请求 ID 无效/);
  assert.throws(() => validateCapabilityRequest({ requestId: "req-12345678", capability: "browser.navigate", args: [] }), /能力参数无效/);
});

import test from "node:test";
import assert from "node:assert/strict";
import {
  looksLikeLoginPage,
  maskElementLine,
  nextDownloadName,
  parseReadArgs,
  parseSelectArgs,
  parseSnapshotArgs,
  parseTypeArgs,
  parseWaitArgs,
  sanitizeDownloadFilename,
} from "../src/main/browser-snapshot.ts";

test("snapshot window args are clamped", () => {
  assert.deepEqual(parseSnapshotArgs({}), { offset: 0, limit: 200 });
  assert.deepEqual(parseSnapshotArgs({ offset: -5, limit: 9999 }), { offset: 0, limit: 200 });
  assert.deepEqual(parseSnapshotArgs({ offset: 20, limit: 3 }), { offset: 20, limit: 3 });
});

test("wait args require selector or value per mode", () => {
  assert.equal(parseWaitArgs({ until: "element", selector: "#x" }).selector, "#x");
  assert.throws(() => parseWaitArgs({ until: "hidden" }), /必须提供 selector/);
  assert.throws(() => parseWaitArgs({ until: "text" }), /必须提供 value/);
  assert.deepEqual(parseWaitArgs({ until: "load", value: "networkidle" }).loadState, "networkidle");
  assert.deepEqual(parseWaitArgs({ until: "load" }).loadState, "domcontentloaded");
  assert.throws(() => parseWaitArgs({ until: "nope" }), /等待条件无效/);
  assert.equal(parseWaitArgs({ until: "text", value: "v", timeoutMs: 999999 }).timeoutMs, 60000);
});

test("type mode caps keystroke text at 2000 chars", () => {
  assert.deepEqual(parseTypeArgs({ text: "hi" }), { mode: "replace", text: "hi" });
  assert.deepEqual(parseTypeArgs({ mode: "keystrokes", text: "hi" }), { mode: "keystrokes", text: "hi" });
  assert.throws(() => parseTypeArgs({ mode: "keystrokes", text: "x".repeat(2001) }), /逐键/);
  assert.doesNotThrow(() => parseTypeArgs({ text: "x".repeat(2001) }));
});

test("select args accept exactly one of value/label/index", () => {
  assert.deepEqual(parseSelectArgs({ value: "a" }), { value: "a" });
  assert.deepEqual(parseSelectArgs({ index: 2 }), { index: 2 });
  assert.throws(() => parseSelectArgs({ value: "a", label: "b" }), /恰好提供一个/);
  assert.throws(() => parseSelectArgs({}), /恰好提供一个/);
  assert.throws(() => parseSelectArgs({ index: 1000 }), /超出范围/);
});

test("read args clamp max chars", () => {
  assert.equal(parseReadArgs({}).maxChars, 20000);
  assert.equal(parseReadArgs({ maxChars: 10 }).maxChars, 500);
  assert.equal(parseReadArgs({ maxChars: 999999 }).maxChars, 40000);
});

test("login heuristic requires a visible password plus a login signal", () => {
  assert.equal(looksLikeLoginPage("https://crm.example.com/userlogin.aspx", "CRM", true, 3), true);
  assert.equal(looksLikeLoginPage("https://crm.example.com/Default.aspx", "工作台", true, 1), true);
  assert.equal(looksLikeLoginPage("https://crm.example.com/Default.aspx", "工作台", false, 1), false);
  assert.equal(looksLikeLoginPage("https://crm.example.com/Default.aspx", "工作台", true, -1), false);
});

test("element lines never leak password values", () => {
  assert.equal(maskElementLine("e3", "password", "", ""), "e3 [password] (value hidden)");
  assert.equal(maskElementLine("e4", "button", "提交", ""), 'e4 [button] "提交"');
  const line = maskElementLine("e5", "input", "用户名", "alice");
  assert.ok(line.includes('value="alice"'));
});

test("download filenames are sanitized and deduplicated", () => {
  assert.equal(sanitizeDownloadFilename('re:port*.xlsx'), "re_port_.xlsx");
  assert.equal(sanitizeDownloadFilename("CON"), "_CON");
  assert.equal(sanitizeDownloadFilename("..hidden.."), "hidden");
  const names = new Set(["report.xlsx", "report (1).xlsx"]);
  assert.equal(nextDownloadName(names, "report.xlsx"), "report (2).xlsx");
  assert.equal(nextDownloadName(names, "fresh.csv"), "fresh.csv");
});

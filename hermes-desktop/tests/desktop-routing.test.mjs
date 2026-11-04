import test from "node:test";
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { serverOrigin, isLocalDocument, localAssetPath } from "../src/main/desktop-routing.ts";

test("platform configuration refuses credentials, insecure URLs and paths", () => {
  for (const value of ["http://example.com", "https://user:secret@example.com", "https://example.com/api", "https://example.com/?secret=x"]) {
    assert.throws(() => serverOrigin(value));
  }
  assert.equal(serverOrigin("https://example.com/"), "https://example.com");
});
test("only user documents are local; API and runtime traffic stays remote", () => {
  for (const path of ["/", "/user/chat", "/user/application-agents/1/debug", "/auth/error", "/auth/complete", "/auth/desktop-complete"]) assert.equal(isLocalDocument(path), true);
  for (const path of ["/user/assets/index.js", "/user/fonts/main.woff2"]) assert.equal(isLocalDocument(path), false);
  for (const path of ["/api/v1/auth/refresh", "/api/v1/auth/oauth2/callback", "/runtime-edge/abc/ws", "/admin", "/uploads/file.pdf"]) assert.equal(isLocalDocument(path), false);
});
test("local asset resolution rejects path escapes and malformed encoding", () => {
  const root = resolve("renderer");
  assert.equal(localAssetPath("/assets/main.js", root), resolve(root, "assets/main.js"));
  assert.equal(localAssetPath("/user/assets/main.js", root), resolve(root, "assets/main.js"));
  assert.equal(localAssetPath("/user/fonts/main.woff2", root), resolve(root, "fonts/main.woff2"));
  for (const path of ["/assets/../../secret", "/assets/%2e%2e/%2e%2e/secret", "/assets/%5csecret", "/assets/%00", "/assets/%zz", "/api/v1/secrets"]) {
    assert.equal(localAssetPath(path, root), null);
  }
});

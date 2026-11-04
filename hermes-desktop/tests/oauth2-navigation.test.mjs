import test from "node:test";
import assert from "node:assert/strict";
import { allowedOAuth2Navigation, isOAuth2Login, parseOAuth2Origins } from "../src/main/oauth2-navigation.ts";

test("OAuth2 navigation requires exact HTTPS origins, without wildcard expansion", () => {
  const platform = "https://platform.example";
  const origins = parseOAuth2Origins('["https://idp.example:8443"]');
  assert.equal(allowedOAuth2Navigation(`${platform}/api/v1/auth/oauth2/callback?code=secret`, platform, origins), true);
  assert.equal(allowedOAuth2Navigation("https://idp.example:8443/authorize", platform, origins), true);
  for (const raw of ["https://evil.idp.example:8443", "http://idp.example:8443", "https://idp.example", "https://user:secret@idp.example:8443", "javascript:alert(1)"]) {
    assert.equal(allowedOAuth2Navigation(raw, platform, origins), false);
  }
  for (const raw of ['["https://*.example"]', '["https://idp.example/path"]', '["https://idp.example?x=1"]', '{}']) {
    assert.throws(() => parseOAuth2Origins(raw));
  }
});

test("OAuth2 login intercept never captures an AES callback or a different origin", () => {
  const platform = "https://platform.example";
  assert.equal(isOAuth2Login(`${platform}/api/v1/auth/oauth2/login?return_to=%2Fuser%2Fchat`, platform), true);
  for (const raw of [`${platform}/?token=aes`, `${platform}/auth/complete`, "https://evil.example/api/v1/auth/oauth2/login"]) {
    assert.equal(isOAuth2Login(raw, platform), false);
  }
});

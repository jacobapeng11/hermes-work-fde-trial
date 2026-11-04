import test from "node:test";
import assert from "node:assert/strict";
import { allowedGatewayNavigation, isLegacySsoGatewayPage, isPlatformLoginConfiguration,
  recoverGatewayConfiguration, transferableGatewayCookie } from "../../src/main/gateway-auth.ts";

const origin = "https://platform.example";
const challenge = () => new Response('<title>Loading...</title><script src="/public/manifest.js"></script><script src="old/shortcut_main.js"></script><img src="images/loading.gif">',
  { headers: { "content-type": "text/html" } });
const config = () => Response.json({ login_method: "oauth2" });

test("gateway HTML is recognized without accepting arbitrary HTML or backend errors", async () => {
  assert.equal(await isLegacySsoGatewayPage(challenge()), true);
  for (const response of [config(), new Response("<title>Loading...</title>", { headers: { "content-type": "text/html" } }),
    new Response("Bad Gateway", { status: 502 })]) assert.equal(await isLegacySsoGatewayPage(response), false);
  assert.equal(await isPlatformLoginConfiguration(challenge()), false);
  for (const body of [{ login_method: "dev" }, {}, null]) assert.equal(await isPlatformLoginConfiguration(Response.json(body)), false);
  assert.equal(await isPlatformLoginConfiguration(config()), true);
  assert.equal(await isPlatformLoginConfiguration(Response.json({ login_method: "enterprise_idpAES" })), true);
});

test("gateway admission precedes retry and HTML never escapes as login configuration", async () => {
  const calls = [];
  const result = await recoverGatewayConfiguration(`${origin}/api/v1/auth/config`, "GET", origin, challenge(),
    async () => { calls.push("gateway"); }, async () => { calls.push("retry"); return config(); });
  assert.deepEqual(calls, ["gateway", "retry"]);
  assert.deepEqual(await result.json(), { login_method: "oauth2" });
  await assert.rejects(recoverGatewayConfiguration(`${origin}/api/v1/auth/config`, "GET", origin, challenge(),
    async () => {}, async () => challenge()), /网关认证尚未完成/);
});

test("normal config, unrelated URLs and POSTs do not open gateway authentication", async () => {
  const unexpected = async () => { assert.fail("unexpected gateway operation"); };
  const normal = config();
  assert.equal(await recoverGatewayConfiguration(`${origin}/api/v1/auth/config`, "GET", origin, normal, unexpected, unexpected), normal);
  for (const [url, method] of [[`${origin}/api/v1/chat`, "GET"], ["https://other.example/api/v1/auth/config", "GET"],
    [`${origin}/api/v1/auth/config`, "POST"]]) {
    const response = challenge();
    assert.equal(await recoverGatewayConfiguration(url, method, origin, response, unexpected, unexpected), response);
  }
});

test("cancelled gateway admission never retries the platform request", async () => {
  await assert.rejects(recoverGatewayConfiguration(`${origin}/api/v1/auth/config`, "GET", origin, challenge(),
    async () => { throw new Error("cancelled"); }, async () => { assert.fail("retry after cancellation"); }), /cancelled/);
});

test("gateway navigation permits exact gateway origins and never provider or wildcard navigation", () => {
  for (const url of [origin, "https://gateway.example.com:6440/portal/shortcut.html", "https://login.example.com/"])
    assert.equal(allowedGatewayNavigation(url, origin), true);
  for (const url of ["http://gateway.example.com:6440", "https://gateway.example.com", "https://evil.gateway.example.com",
    "https://idp.example.com:444", "https://user:secret@platform.example", "javascript:alert(1)"])
    assert.equal(allowedGatewayNavigation(url, origin), false);
});

test("gateway admission transfer excludes every platform and one-use OAuth2 cookie", () => {
  assert.equal(transferableGatewayCookie("gateway_session"), true);
  for (const name of ["hermes_platform_session", "hermes_oauth2_browser", "hermes_oauth2_completion", "HERMES_PLATFORM_SESSION"])
    assert.equal(transferableGatewayCookie(name), false);
});

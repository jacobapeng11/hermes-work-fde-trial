import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { loadConfigFromFile } from "vite";

const configPath = fileURLToPath(new URL("../../electron.vite.config.ts", import.meta.url));

async function withOrigin(origin, check) {
  const previous = process.env.COWORK_SERVER_ORIGIN;
  try {
    if (origin === undefined) delete process.env.COWORK_SERVER_ORIGIN;
    else process.env.COWORK_SERVER_ORIGIN = origin;
    const result = await loadConfigFromFile({ command: "build", mode: "production" }, configPath);
    assert.ok(result);
    await check(result.config.main.define);
  } finally {
    if (previous === undefined) delete process.env.COWORK_SERVER_ORIGIN;
    else process.env.COWORK_SERVER_ORIGIN = previous;
  }
}

test("an explicit nondefault platform origin survives packaging without a runtime environment", async () => {
  await withOrigin("https://configured-platform.example/", (defines) => {
    assert.equal(defines["process.env.COWORK_SERVER_ORIGIN"], '"https://configured-platform.example"');
    assert.deepEqual(Object.keys(defines).sort(), [
      "process.env.COWORK_SERVER_ORIGIN", "process.env.OAUTH2_DESKTOP_ALLOWED_ORIGINS",
    ]);
  });
});

test("an absent build origin preserves the existing runtime override and default", async () => {
  await withOrigin(undefined, (defines) => {
    assert.equal(defines["process.env.COWORK_SERVER_ORIGIN"], undefined);
  });
});

test("a configured origin cannot embed credentials, HTTP or an application path", async () => {
  for (const origin of ["http://platform.example", "https://user:secret@platform.example", "https://platform.example/user/chat"]) {
    await assert.rejects(withOrigin(origin, () => {}), /HTTPS origin without a path or credentials/);
  }
});

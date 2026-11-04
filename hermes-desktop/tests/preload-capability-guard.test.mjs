import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { LOCAL_CAPABILITY_NAMES } from "../src/main/capability-protocol.ts";

const projectRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

// The preload bridge, its hand-written .d.ts and the renderer-facing
// desktop.d.ts each inline the capability union with no compiler cross-check;
// this guard keeps the copies from drifting apart.
function extractInlineUnion(source, anchor) {
  const anchorIndex = source.indexOf(anchor);
  assert.ok(anchorIndex >= 0, `anchor not found: ${anchor}`);
  const start = source.indexOf('"', anchorIndex);
  let end = start;
  let inString = false;
  while (end < source.length) {
    const char = source[end];
    if (char === '"') inString = !inString;
    else if (!inString && char === ";") break;
    else if (!inString && !/[\s"|]/.test(char) && char !== undefined) {
      // Only quotes, pipes and whitespace may appear outside the strings.
      throw new Error(`unexpected character outside union strings: ${char}`);
    }
    end += 1;
  }
  const segment = source.slice(start, end);
  return new Set(segment.match(/"([^"]+)"/g).map((match) => match.slice(1, -1)));
}

test("preload capability unions match the protocol source of truth", async () => {
  const expected = new Set(LOCAL_CAPABILITY_NAMES);
  const files = [
    ["src/preload/index.ts", "requestCapability"],
    ["src/preload/index.d.ts", "requestCapability"],
    ["src/desktop.d.ts", "requestCapability"],
  ];
  for (const [relativePath, anchor] of files) {
    const source = await readFile(join(projectRoot, relativePath), "utf8");
    const union = extractInlineUnion(source, anchor);
    assert.deepEqual(
      [...union].sort(),
      [...expected].sort(),
      `${relativePath} capability union drifted from LOCAL_CAPABILITY_NAMES`,
    );
  }
});

test("runtime edge client mirror matches the protocol source of truth", async () => {
  const source = await readFile(join(projectRoot, "src/api/runtime-edge-client.ts"), "utf8");
  const marker = "const LOCAL_CAPABILITY_NAMES";
  const markerIndex = source.indexOf(marker);
  assert.ok(markerIndex >= 0, "runtime-edge-client mirror not found");
  const closingIndex = source.indexOf("]);", markerIndex);
  const segment = source.slice(markerIndex, closingIndex);
  const names = segment.match(/"([^"]+)"/g).map((match) => match.slice(1, -1)).filter((name) => name.includes("."));
  assert.deepEqual(
    [...new Set(names)].sort(),
    [...LOCAL_CAPABILITY_NAMES].sort(),
    "runtime-edge-client capability set drifted from LOCAL_CAPABILITY_NAMES",
  );
});

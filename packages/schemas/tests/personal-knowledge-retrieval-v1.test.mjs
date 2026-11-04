import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const fixtureDirectory = fileURLToPath(
  new URL("../fixtures/personal-knowledge-retrieval-v1/", import.meta.url),
);

async function loadFixture(name) {
  return JSON.parse(await readFile(`${fixtureDirectory}${name}`, "utf8"));
}

test("personal knowledge state exposes only platform IDs and bounded capabilities", async () => {
  const state = await loadFixture("state.json");
  assert.deepEqual(Object.keys(state).sort(), [
    "enabled",
    "knowledge_bases",
    "revision",
  ]);
  assert.equal(state.enabled, true);
  assert.equal(state.knowledge_bases.length, 1);
  const item = state.knowledge_bases[0];
  assert.equal(item.id, 2);
  assert.equal(item.ready_file_count, 1);
  assert.deepEqual(item.capabilities, { rag: true, wiki: true });
  assert.equal("dataset_id" in item, false);
  assert.equal("provider" in item, false);
  assert.equal(JSON.stringify(state).includes("wk-secret"), false);
});

test("hybrid search separates RAG and Wiki results without upstream identities", async () => {
  const result = await loadFixture("hybrid-search.json");
  assert.equal(result.rag_results.length, 1);
  assert.equal(result.wiki_results.length, 1);
  assert.equal(result.rag_results[0].knowledge_base_id, 3);
  assert.equal(result.wiki_results[0].knowledge_base_id, 2);
  assert.deepEqual(
    result.rag_searched_knowledge_base_ids,
    [3],
  );
  assert.deepEqual(
    result.wiki_searched_knowledge_base_ids,
    [2],
  );
  assert.equal(JSON.stringify(result).includes("wk-secret"), false);
  assert.equal(JSON.stringify(result).includes("upstream"), false);
});

test("wiki search returns page summaries while read-page carries content and platform sources", async () => {
  const search = await loadFixture("wiki-search.json");
  const hit = search.matches[0];
  assert.equal(hit.source_type, "wiki_page");
  assert.equal(hit.knowledge_base_id, 2);
  assert.equal("content" in hit, false);
  assert.equal("file_id" in hit, false);
  assert.equal("id" in hit, false);

  const page = await loadFixture("wiki-read-page.json");
  assert.equal(page.content, "完整 Wiki 正文");
  assert.deepEqual(page.source_files.map((file) => file.file_id), [11, 22]);
  assert.equal("chunk_refs" in page, false);
  assert.equal("knowledge_id" in page, false);
  assert.equal(JSON.stringify(page).includes("upstream"), false);
});

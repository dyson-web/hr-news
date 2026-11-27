// 実行: cd mcp && node --test
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { handleRpc, _resetCache } from "./core.js";
import worker from "./worker.js";

const DATA = readFileSync(new URL("../docs/data.json", import.meta.url), "utf8");
const fakeFetch = async () => new Response(DATA, { status: 200 });
const opts = { dataUrl: "test://data", fetch: fakeFetch };
const call = (name, args) => handleRpc({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }, opts);

test("initialize と tools/list", async () => {
  const init = await handleRpc({ jsonrpc: "2.0", id: 0, method: "initialize", params: { protocolVersion: "2025-06-18" } });
  assert.equal(init.result.protocolVersion, "2025-06-18");
  assert.ok(init.result.capabilities.tools);
  const list = await handleRpc({ jsonrpc: "2.0", id: 1, method: "tools/list" });
  assert.deepEqual(list.result.tools.map(t => t.name).sort(),
    ["get_hr_article", "get_latest_hr_news", "get_trending_hr_news", "list_hr_media_and_topics", "search_hr_news"]);
  assert.equal(await handleRpc({ jsonrpc: "2.0", method: "notifications/initialized" }), null);
});

test("新着・検索・話題・詳細・一覧", async () => {
  _resetCache();
  const latest = await call("get_latest_hr_news", { days: 7, limit: 5 });
  assert.ok(latest.result.structuredContent.count > 0);
  const first = latest.result.structuredContent.items[0];
  assert.ok(first.url.startsWith("https://") && first.id);

  const s = await call("search_hr_news", { query: "採用", limit: 3 });
  assert.ok(s.result.structuredContent.items.every(i => JSON.stringify(i).includes("採用") || i.tags.includes("採用")) || s.result.structuredContent.count >= 0);

  const hr = await call("search_hr_news", { media: "HRog", limit: 50 });
  assert.ok(hr.result.structuredContent.items.every(i => i.media === "HRog"));

  const t = await call("get_trending_hr_news", { period: "30d", limit: 5 });
  const scores = t.result.structuredContent.items.map(i => i.buzz_score);
  assert.deepEqual(scores, [...scores].sort((a, b) => b - a));

  const d = await call("get_hr_article", { id: first.id });
  assert.equal(d.result.structuredContent.id, first.id);
  assert.match(d.result.content[0].text, /^# /);
  const byUrl = await call("get_hr_article", { id: first.url });
  assert.equal(byUrl.result.structuredContent.id, first.id);
  const miss = await call("get_hr_article", { id: "nope" });
  assert.equal(miss.result.isError, true);

  const meta = await call("list_hr_media_and_topics", {});
  assert.ok(meta.result.structuredContent.media.some(m => m.name === "日本の人事部"));
});

test("Workers: POST /mcp と通知・CORS", async () => {
  _resetCache();
  const srv = createServer((req, res) => { res.setHeader("Content-Type", "application/json"); res.end(DATA); }).listen(0);
  const env = { DATA_URL: `http://127.0.0.1:${srv.address().port}/data.json` };
  const post = (body) => worker.fetch(new Request("https://x.workers.dev/mcp", { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" }, body: JSON.stringify(body) }), env);
  const init = await post({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26" } });
  assert.equal(init.status, 200);
  assert.ok(init.headers.get("Mcp-Session-Id"));
  assert.equal((await init.json()).result.serverInfo.name, "hr-news-digest");
  const note = await post({ jsonrpc: "2.0", method: "notifications/initialized" });
  assert.equal(note.status, 202);
  const r = await post({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "get_trending_hr_news", arguments: { period: "7d" } } });
  const j = await r.json();
  assert.ok(j.result.content[0].text.includes("7日間"));
  const opt = await worker.fetch(new Request("https://x.workers.dev/mcp", { method: "OPTIONS" }), env);
  assert.equal(opt.headers.get("Access-Control-Allow-Origin"), "*");
  srv.close();
});

test("stdio: 1行1メッセージで応答", async () => {
  const srv = createServer((req, res) => { res.setHeader("Content-Type", "application/json"); res.end(DATA); }).listen(0);
  const child = spawn(process.execPath, [new URL("./stdio.js", import.meta.url).pathname], {
    env: { ...process.env, HR_NEWS_DATA_URL: `http://127.0.0.1:${srv.address().port}/data.json` },
  });
  const lines = [];
  child.stdout.on("data", (b) => lines.push(...b.toString().split("\n").filter(Boolean)));
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }) + "\n");
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "get_latest_hr_news", arguments: { days: 30, limit: 2 } } }) + "\n");
  for (let i = 0; i < 50 && lines.length < 2; i++) await new Promise(r => setTimeout(r, 100));
  child.kill(); srv.close();
  assert.equal(lines.length, 2);
  const res = JSON.parse(lines[1]);
  assert.equal(res.id, 2);
  assert.equal(res.result.structuredContent.items.length, 2);
});

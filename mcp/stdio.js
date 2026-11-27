#!/usr/bin/env node
// ローカル用エントリ（stdio）。Claude Desktop / Claude Code から直接起動して使う。
// 依存パッケージなし（Node.js 18 以上）。
import { createInterface } from "node:readline";
import { handleRpc, DEFAULT_DATA_URL } from "./core.js";

const opts = { dataUrl: process.env.HR_NEWS_DATA_URL || DEFAULT_DATA_URL };
const rl = createInterface({ input: process.stdin, terminal: false });
rl.on("line", async (line) => {
  if (!line.trim()) return;
  let msg;
  try { msg = JSON.parse(line); } catch {
    process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }) + "\n");
    return;
  }
  const res = await handleRpc(msg, opts);
  if (res) process.stdout.write(JSON.stringify(res) + "\n");
});

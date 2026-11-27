// Cloudflare Workers 用エントリ（リモートMCP / Streamable HTTP）
// エンドポイント: https://<worker>.workers.dev/mcp
import { handleRpc, DEFAULT_DATA_URL } from "./core.js";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS, DELETE",
  "Access-Control-Allow-Headers": "Content-Type, Accept, Mcp-Session-Id, Mcp-Protocol-Version, Authorization",
  "Access-Control-Expose-Headers": "Mcp-Session-Id",
};
const json = (body, status = 200, extra = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...CORS, ...extra } });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (url.pathname === "/" && request.method === "GET") {
      return new Response("HR News Digest MCP server. Connect to /mcp", { headers: { "Content-Type": "text/plain; charset=utf-8", ...CORS } });
    }
    if (url.pathname !== "/mcp") return json({ error: "not found" }, 404);
    // このサーバーはサーバー起点の通知を送らないので、GET（SSE）は使わない
    if (request.method === "GET") return new Response("Method Not Allowed", { status: 405, headers: { Allow: "POST", ...CORS } });
    if (request.method === "DELETE") return new Response(null, { status: 204, headers: CORS });
    if (request.method !== "POST") return new Response("Method Not Allowed", { status: 405, headers: CORS });

    let body;
    try { body = await request.json(); } catch { return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }, 400); }
    const opts = { dataUrl: env?.DATA_URL || DEFAULT_DATA_URL };
    const msgs = Array.isArray(body) ? body : [body];
    const results = (await Promise.all(msgs.map(m => handleRpc(m, opts)))).filter(Boolean);
    const extra = msgs.some(m => m?.method === "initialize") ? { "Mcp-Session-Id": crypto.randomUUID() } : {};
    if (!results.length) return new Response(null, { status: 202, headers: CORS });
    return json(Array.isArray(body) ? results : results[0], 200, extra);
  },
};

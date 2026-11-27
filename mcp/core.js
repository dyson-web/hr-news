// HR News Digest MCP — 共通ロジック（Cloudflare Workers と ローカルstdio の両方から使う）
// サイトが公開している data.json を読み、検索・新着・話題ランキング・記事詳細をツールとして提供する。

export const DEFAULT_DATA_URL = "https://dyson-web.github.io/hr-news/data.json";
const SERVER_INFO = { name: "hr-news-digest", version: "1.0.0" };
const PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"];
const CACHE_MS = 5 * 60 * 1000;
const SITE_URL = "https://dyson-web.github.io/hr-news/";

let cache = { at: 0, url: "", data: null };

export async function loadData(dataUrl = DEFAULT_DATA_URL, fetchImpl = fetch) {
  if (cache.data && cache.url === dataUrl && Date.now() - cache.at < CACHE_MS) return cache.data;
  const r = await fetchImpl(dataUrl, { headers: { "User-Agent": "hr-news-digest-mcp/1.0" } });
  if (!r.ok) throw new Error(`記事データを取得できませんでした（HTTP ${r.status}）`);
  const data = await r.json();
  data.items.sort((a, b) => b.published.localeCompare(a.published));
  cache = { at: Date.now(), url: dataUrl, data };
  return data;
}
export function _resetCache() { cache = { at: 0, url: "", data: null }; }

// ── 整形 ──
const shortSrc = (s) => String(s || "").replace(/（.*?）/g, "").trim();
const PERIOD = { "24h": "d1", "7d": "d7", "30d": "d30" };
const PERIOD_JA = { "24h": "24時間", "7d": "7日間", "30d": "30日間" };

function jstDate(iso) {
  const d = new Date(Date.parse(iso) + 9 * 3600e3);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
}

function brief(it, period) {
  const out = {
    id: it.id, title: it.title, url: it.url, media: shortSrc(it.source),
    date: it.date_unknown ? null : jstDate(it.published),
    digest: it.digest || null, tags: it.tags || [],
    hatena_bookmarks: it.hb ?? 0, heat: it.heat ?? 0,
  };
  if (it.rank) out.media_ranking = `${it.rank.label} ${it.rank.rank}位`;
  if (period) {
    const s = it.signals?.[PERIOD[period]] || {};
    out.buzz_score = it.buzz?.[PERIOD[period]] ?? 0;
    out.signals = { hatena_increase: s.hb || 0, best_media_rank: s.rank || null };
  }
  return out;
}

function full(it) {
  return {
    ...brief(it),
    summary: it.summary || [],
    description: it.desc || null,
    read_minutes: it.read_min || null,
    ai_summary: !!it.ai,
    image: it.image || null,
    buzz: it.buzz || null,
    site_link: `${SITE_URL}#a=${it.id}`,
  };
}

function lineOf(b, i) {
  const bits = [b.media, b.date || "日付不明"];
  if (b.media_ranking) bits.push(b.media_ranking);
  if (b.hatena_bookmarks) bits.push(`はてブ${b.hatena_bookmarks}`);
  if (b.tags?.length) bits.push(b.tags.map(t => `#${t}`).join(" "));
  let s = `${i + 1}. ${b.title}\n   ${bits.join("｜")}\n   ${b.url}`;
  if (b.digest) s += `\n   要点: ${b.digest}`;
  s += `\n   id: ${b.id}`;
  return s;
}

function listResult(header, items, period) {
  const briefs = items.map(it => brief(it, period));
  const text = briefs.length ? `${header}\n\n${briefs.map(lineOf).join("\n\n")}` : `${header}\n\n該当する記事はありませんでした。`;
  return { content: [{ type: "text", text }], structuredContent: { count: briefs.length, items: briefs } };
}

// ── 絞り込み ──
function filterItems(items, { query, media, topic, days, since } = {}) {
  const now = Date.now();
  const words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  const m = media ? shortSrc(media).toLowerCase() : "";
  return items.filter(it => {
    if (m && !shortSrc(it.source).toLowerCase().includes(m)) return false;
    if (topic && !(it.tags || []).includes(topic)) return false;
    if (days && (it.date_unknown || now - Date.parse(it.published) > days * 864e5)) return false;
    if (since && it.published.slice(0, 10) < since) return false;
    if (words.length) {
      const hay = `${it.title} ${it.digest || ""} ${it.desc || ""} ${(it.summary || []).join(" ")} ${(it.tags || []).join(" ")} ${it.source}`.toLowerCase();
      if (!words.every(w => hay.includes(w))) return false;
    }
    return true;
  });
}
const clamp = (n, lo, hi, d) => Math.max(lo, Math.min(hi, Number.isFinite(+n) && n !== null && n !== undefined ? +n : d));

// ── ツール定義 ──
export const TOOLS = [
  {
    name: "search_hr_news",
    title: "人事ニュースを検索",
    description: "日本の人事系メディア（日本の人事部、HRog、労基旬報オンライン、DHBR、企業と人材、noteの人事コミュニティなど）から集めた記事を、キーワード・媒体・トピック・期間で検索する。複数キーワードはスペース区切りでAND検索。",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "キーワード（例: 人的資本 開示）。タイトル・要約・概要・タグを対象に検索" },
        media: { type: "string", description: "媒体名で絞る（部分一致。例: HRog, 日本の人事部）" },
        topic: { type: "string", description: "トピックで絞る。list_hr_media_and_topics で一覧を確認できる" },
        days: { type: "integer", description: "直近何日の記事を対象にするか（既定 90）", minimum: 1, maximum: 365 },
        limit: { type: "integer", description: "最大件数（既定 10、最大 50）", minimum: 1, maximum: 50 },
      },
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: "get_latest_hr_news",
    title: "人事ニュースの新着",
    description: "人事系メディアの新着記事を新しい順に返す。朝のチェックや「今日の人事ニュースは？」に使う。",
    inputSchema: {
      type: "object",
      properties: {
        days: { type: "integer", description: "直近何日分か（既定 1）", minimum: 1, maximum: 30 },
        media: { type: "string", description: "媒体名で絞る（部分一致）" },
        topic: { type: "string", description: "トピックで絞る" },
        limit: { type: "integer", description: "最大件数（既定 20、最大 50）", minimum: 1, maximum: 50 },
      },
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: "get_trending_hr_news",
    title: "いま話題の人事ニュース",
    description: "話題度の高い順に記事を返す。話題度は、はてなブックマーク数の期間内の増加と、各メディア公式アクセスランキングの順位から算出（記事PVそのものではない）。",
    inputSchema: {
      type: "object",
      properties: {
        period: { type: "string", enum: ["24h", "7d", "30d"], description: "期間（既定 7d）" },
        topic: { type: "string", description: "トピックで絞る" },
        limit: { type: "integer", description: "最大件数（既定 10、最大 30）", minimum: 1, maximum: 30 },
      },
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: "get_hr_article",
    title: "記事の詳細",
    description: "記事1件の詳細（AIの3点要約、媒体の概要、トピック、話題度、読了時間、元記事URL）を返す。id は他のツールの結果に含まれる。URLでも指定できる。",
    inputSchema: {
      type: "object",
      properties: { id: { type: "string", description: "記事id、または元記事のURL" } },
      required: ["id"],
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: "list_hr_media_and_topics",
    title: "収集媒体とトピックの一覧",
    description: "収集している媒体と記事数、使えるトピック、データの最終更新時刻を返す。",
    inputSchema: { type: "object", properties: {} },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
];

export async function callTool(name, args = {}, opts = {}) {
  const data = await loadData(opts.dataUrl, opts.fetch);
  const items = data.items;
  switch (name) {
    case "search_hr_news": {
      const limit = clamp(args.limit, 1, 50, 10);
      const hits = filterItems(items, { query: args.query, media: args.media, topic: args.topic, days: clamp(args.days, 1, 365, 90) });
      const cond = [args.query && `「${args.query}」`, args.media, args.topic && `#${args.topic}`].filter(Boolean).join("・") || "すべて";
      return listResult(`${cond} の検索結果：${hits.length}件中 ${Math.min(limit, hits.length)}件`, hits.slice(0, limit));
    }
    case "get_latest_hr_news": {
      const days = clamp(args.days, 1, 30, 1), limit = clamp(args.limit, 1, 50, 20);
      const hits = filterItems(items.filter(it => it.origin !== "ranking"), { media: args.media, topic: args.topic, days });
      return listResult(`直近${days}日の新着：${hits.length}件（新しい順に${Math.min(limit, hits.length)}件）`, hits.slice(0, limit));
    }
    case "get_trending_hr_news": {
      const period = PERIOD[args.period] ? args.period : "7d";
      const key = PERIOD[period], limit = clamp(args.limit, 1, 30, 10);
      const hits = filterItems(items, { topic: args.topic })
        .filter(it => (it.buzz?.[key] || 0) > 0)
        .sort((a, b) => b.buzz[key] - a.buzz[key]);
      return listResult(`${PERIOD_JA[period]}で話題の記事（はてブ増加数と媒体ランキングによる）`, hits.slice(0, limit), period);
    }
    case "get_hr_article": {
      const key = String(args.id || "").trim();
      const it = items.find(x => x.id === key || x.url === key || x.url.replace(/\/$/, "") === key.replace(/\/$/, ""));
      if (!it) return { content: [{ type: "text", text: `記事が見つかりませんでした: ${key}` }], isError: true };
      const f = full(it);
      const lines = [`# ${f.title}`, `${f.media}｜${f.date || "日付不明"}${f.read_minutes ? `｜約${f.read_minutes}分` : ""}`, f.url];
      if (f.digest) lines.push("", `要点: ${f.digest}`);
      if (f.summary.length) lines.push("", "要約（AI作成）:", ...f.summary.map(s => `- ${s}`));
      if (f.description) lines.push("", `媒体の概要: ${f.description}`);
      if (f.tags.length) lines.push("", `トピック: ${f.tags.join("、")}`);
      lines.push("", `話題度: はてブ${f.hatena_bookmarks}${f.media_ranking ? `、${f.media_ranking}` : ""}`);
      return { content: [{ type: "text", text: lines.join("\n") }], structuredContent: f };
    }
    case "list_hr_media_and_topics": {
      const counts = {};
      items.forEach(it => { const m = shortSrc(it.source); counts[m] = (counts[m] || 0) + 1; });
      const topics = {};
      items.forEach(it => (it.tags || []).forEach(t => topics[t] = (topics[t] || 0) + 1));
      const res = {
        updated: data.updated, total: items.length,
        media: Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([name, count]) => ({ name, count })),
        topics: (data.topics || []).map(name => ({ name, count: topics[name] || 0 })),
        ai_summary_enabled: !!data.features?.ai,
      };
      const text = [`最終更新: ${res.updated}（全${res.total}件）`, "", "媒体:", ...res.media.map(m => `- ${m.name}（${m.count}件）`),
        "", "トピック:", ...res.topics.map(t => `- ${t.name}（${t.count}件）`)].join("\n");
      return { content: [{ type: "text", text }], structuredContent: res };
    }
    default:
      throw Object.assign(new Error(`不明なツール: ${name}`), { code: -32602 });
  }
}

// ── JSON-RPC（MCP）ハンドラ ──
export async function handleRpc(msg, opts = {}) {
  const { id, method, params } = msg || {};
  const isNotification = id === undefined || id === null;
  const ok = (result) => ({ jsonrpc: "2.0", id, result });
  const err = (code, message) => ({ jsonrpc: "2.0", id: id ?? null, error: { code, message } });
  try {
    switch (method) {
      case "initialize": {
        const want = params?.protocolVersion;
        return ok({
          protocolVersion: PROTOCOLS.includes(want) ? want : PROTOCOLS[0],
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER_INFO,
          instructions: "日本の人事系メディアの記事を検索・取得できます。新着は get_latest_hr_news、話題は get_trending_hr_news、テーマ調査は search_hr_news、1件の詳細は get_hr_article。回答では元記事URLを示してください。要約はAI作成なので、重要な判断は元記事で確認するよう添えてください。",
        });
      }
      case "ping": return ok({});
      case "tools/list": return ok({ tools: TOOLS });
      case "tools/call": {
        const result = await callTool(params?.name, params?.arguments || {}, opts);
        return ok(result);
      }
      case "resources/list": return ok({ resources: [] });
      case "prompts/list": return ok({ prompts: [] });
      default:
        if (isNotification) return null; // notifications/initialized など
        return err(-32601, `Method not found: ${method}`);
    }
  } catch (e) {
    if (e.code === -32602) return err(-32602, e.message);
    if (method === "tools/call") return ok({ content: [{ type: "text", text: `エラー: ${e.message}` }], isError: true });
    return err(-32603, e.message);
  }
}

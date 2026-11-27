"""人事系メディアの新着を集め、概要・AI要約・話題度を付けてニュースサイト(docs/)を生成する。

使い方:
  python collect.py                 # 収集 → 補完(概要/要約/タグ) → 話題度更新 → サイト生成（Slack設定があれば投稿）
  python collect.py --dry-run       # Slackには送らず、送る内容を表示
  python collect.py --seed          # 今ある記事を既読として登録だけする（Slackに流さない）
  python collect.py --only 名前     # そのソースだけ収集（調整用）

環境変数（すべて任意）:
  ANTHROPIC_API_KEY   設定すると、記事ごとにAIの3点要約・一言まとめ・トピック分類を付ける
  SLACK_WEBHOOK_URL   設定すると、新着をSlackに投稿する
  SITE_URL            Slackメッセージ末尾に付けるサイトのURL
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
ITEMS_PATH = ROOT / "data" / "items.json"
SITE_DIR = ROOT / "docs"
TEMPLATE_PATH = ROOT / "templates" / "index.html"

JST = timezone(timedelta(hours=9))
USER_AGENT = "HRNewsDigestBot/1.1 (+https://dyson-web.github.io/hr-news/)"
REQUEST_INTERVAL_SEC = 2.0     # 同じサイトへのリクエスト間隔（相手サイトへの配慮）
MAX_ITEMS_PER_SOURCE = 20      # 1ソースから1回に取り込む上限
KEEP_ITEMS = 1500              # items.json に保持する件数
ENRICH_PER_RUN = 80            # 1回の実行で概要・要約を補う記事数の上限（古い記事も少しずつ埋まる）
HATENA_WINDOW_DAYS = 30        # はてブ数を追う期間
SLACK_MAX_ITEMS = 40
AI_MODEL = "claude-haiku-5-5"

TOPICS = [
    "採用", "育成・研修", "評価・報酬", "組織開発", "労務・法改正", "人的資本経営",
    "働き方", "エンゲージメント", "ダイバーシティ", "HRテック・AI", "キャリア", "調査・データ", "書籍",
]
TOPIC_KEYWORDS = {
    "採用": ["採用", "求人", "就活", "新卒", "中途", "内定", "転職", "面接", "応募", "スカウト", "人材紹介", "求職"],
    "育成・研修": ["育成", "研修", "リスキリング", "学び", "教育", "OJT", "人材開発", "スキル", "研修費"],
    "評価・報酬": ["評価", "報酬", "賃金", "給与", "賃上げ", "賞与", "等級", "人事制度", "360度", "昇給", "退職金"],
    "組織開発": ["組織開発", "組織文化", "マネジメント", "管理職", "リーダー", "チーム", "1on1", "心理的安全", "変革", "マネジャー"],
    "労務・法改正": ["法改正", "労働基準", "労基", "残業", "ハラスメント", "社会保険", "雇用保険", "労災", "就業規則", "最低賃金", "障害者雇用", "育児・介護休業", "厚生労働省", "法制", "コンプライアンス"],
    "人的資本経営": ["人的資本", "開示", "ISO 30414", "人材戦略", "経営戦略"],
    "働き方": ["働き方", "リモート", "テレワーク", "副業", "兼業", "出社", "週休", "フレックス", "ワークライフ"],
    "エンゲージメント": ["エンゲージメント", "離職", "定着", "ウェルビーイング", "従業員満足", "働く喜び", "モチベーション", "退職"],
    "ダイバーシティ": ["ダイバーシティ", "女性活躍", "D&I", "DEI", "シニア", "外国人", "障害者", "LGBT", "インクルージョン", "Z世代"],
    "HRテック・AI": ["AI", "生成AI", "HRテック", "DX", "システム", "ツール", "データ活用", "タレントマネジメント"],
    "キャリア": ["キャリア", "自律", "天職", "異動", "社内公募", "副業"],
    "調査・データ": ["調査", "実態", "アンケート", "白書", "統計", "ランキング", "割合", "％", "%", "レポート"],
    "書籍": ["書籍", "刊行", "著）", "新刊"],
}

DATE_RE = re.compile(r"(20\d{2})[./年-]\s*(\d{1,2})[./月-]\s*(\d{1,2})")
DROP_QUERY = ("utm_", "newstop=", "fbclid=", "ref=", "rclk=", "search=")


# ── HTTP ──────────────────────────────────────────────
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_last_request: dict[str, float] = {}
_page_cache: dict[str, requests.Response] = {}


def allowed_by_robots(url: str) -> bool:
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = _session.get(base + "/robots.txt", timeout=15)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None  # robots.txt なし＝制限なし
        except requests.RequestException:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def fetch(url: str) -> requests.Response:
    """ホストごとに間隔をあけて取得する。同じ実行内の同じURLは使い回す。"""
    if url in _page_cache:
        return _page_cache[url]
    host = urlsplit(url).netloc
    wait = REQUEST_INTERVAL_SEC - (time.monotonic() - _last_request.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.monotonic()
    r = _session.get(url, timeout=30)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    _page_cache[url] = r
    return r


# ── URL・日付・テキスト ─────────────────────────────────
def normalize_url(url: str) -> str:
    """トラッキング用クエリ(?newstop=new 等)を落として重複判定を安定させる。"""
    parts = urlsplit(url)
    keep = "&".join(q for q in parts.query.split("&") if q and not q.startswith(DROP_QUERY))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, keep, ""))


def make_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def parse_date_text(text: str) -> datetime | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=JST)
    except ValueError:
        return None


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(JST)


def clean_text(s: str, limit: int | None = None) -> str:
    s = BeautifulSoup(s or "", "html.parser").get_text(" ") if "<" in (s or "") else (s or "")
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rstrip("、。 ") + "…"
    return s


# ── 一覧の収集 ─────────────────────────────────────────
def collect_rss(src: dict, content: bytes) -> list[dict]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = clean_text(e.get("title") or "")
        link = e.get("link")
        if not title or not link:
            continue
        st = e.get("published_parsed") or e.get("updated_parsed")
        dt = datetime(*st[:6], tzinfo=timezone.utc).astimezone(JST) if st else None
        desc = clean_text(e.get("summary") or e.get("description") or "", 220)
        out.append({"title": title, "url": link, "dt": dt, "desc": desc})
    return out


def _anchor_title(a) -> tuple[str, bool]:
    """リンク内の見出し要素を優先してタイトルを取る。(タイトル, 見出し由来か)"""
    for sel in ("h1", "h2", "h3", "h4", "[class*=title]", "[class*=ttl]", "[class*=heading]"):
        el = a.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(" ", strip=True), True
    if a.get("title"):
        return a["title"].strip(), False
    return a.get_text(" ", strip=True), False


def _nearby_date(a) -> datetime | None:
    node = a
    for _ in range(4):
        dt = parse_date_text(node.get_text(" ", strip=True))
        if dt:
            return dt
        node = node.parent
        if node is None or len(node.get_text(strip=True)) > 400:
            break
    t = a.find_previous("time") or a.find_next("time")
    if t:
        return parse_date_text(t.get("datetime") or t.get_text())
    return None


def collect_html(src: dict, content: bytes, base_url: str) -> list[dict]:
    soup = BeautifulSoup(content, "html.parser")
    pattern = re.compile(src["link_pattern"])
    exclude = re.compile(src["exclude_pattern"]) if src.get("exclude_pattern") else None
    best: dict[str, tuple[str, bool, datetime | None]] = {}
    order: list[str] = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"])
        if not pattern.search(url) or (exclude and exclude.search(url)):
            continue
        key = normalize_url(url)
        title, from_heading = _anchor_title(a)
        title = re.sub(r"\s+", " ", title).strip()
        if key not in best:
            order.append(key)
        prev = best.get(key)
        dt = (prev[2] if prev else None) or _nearby_date(a)
        if len(title) < 6:          # 画像だけのリンクや「続きを読む」は捨てる
            best[key] = prev[:2] + (dt,) if prev else ("", False, dt)
            continue
        title = DATE_RE.sub("", title).strip(" |｜-") or title
        if len(title) > 140:
            title = title[:140] + "…"
        if prev is None or not prev[0] or (from_heading and not prev[1]) or (
            from_heading == prev[1] and len(title) < len(prev[0])
        ):
            best[key] = (title, from_heading, dt)
        else:
            best[key] = (prev[0], prev[1], dt)
    out = [{"title": best[k][0], "url": k, "dt": best[k][2], "desc": ""} for k in order if best[k][0]]
    return out[:MAX_ITEMS_PER_SOURCE]


def collect_source(src: dict) -> list[dict]:
    url = src["url"]
    if not allowed_by_robots(url):
        raise RuntimeError("robots.txt で取得が禁止されています")
    r = fetch(url)
    entries = collect_rss(src, r.content) if src["type"] == "rss" else collect_html(src, r.content, r.url)
    strip = src.get("title_strip")
    if strip:
        for e in entries:
            e["title"] = re.sub(strip, "", e["title"]).strip() or e["title"]
    kws = src.get("include_keywords")
    if kws:
        entries = [e for e in entries if any(k in e["title"] for k in kws)]
    return entries


# ── メディア公式のアクセスランキング ─────────────────────
def collect_ranking(rk: dict) -> list[tuple[str, str]]:
    """[(正規化URL, タイトル)] を順位順で返す。"""
    if not allowed_by_robots(rk["url"]):
        raise RuntimeError("robots.txt で取得が禁止されています")
    r = fetch(rk["url"])
    soup = BeautifulSoup(r.content, "html.parser")
    pattern = re.compile(rk["link_pattern"])
    anchors = soup.find_all("a", href=True)
    if rk.get("section_heading"):
        head = None
        for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "div", "span"]):
            if el.get_text(strip=True) == rk["section_heading"] or (
                rk["section_heading"] in el.get_text(strip=True) and len(el.get_text(strip=True)) < 30
            ):
                head = el
                break
        if head is None:
            return []
        node = head
        while node is not None:
            links = [a for a in node.find_all("a", href=True) if pattern.search(urljoin(r.url, a["href"]))]
            if len({normalize_url(urljoin(r.url, a["href"])) for a in links}) >= 3:
                anchors = links
                break
            node = node.parent
        else:
            return []
    out, seen = [], set()
    for a in anchors:
        url = urljoin(r.url, a["href"])
        if not pattern.search(url):
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        title, _ = _anchor_title(a)
        out.append((key, re.sub(r"\s+", " ", title).strip()))
        if len(out) >= rk.get("max", 10):
            break
    return out


# ── 記事ページからの補完（概要・本文量・日付） ──────────────
def extract_article(html: bytes) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names):
        for n in names:
            el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if el and el.get("content"):
                return el["content"].strip()
        return ""

    desc = clean_text(meta("og:description", "description", "twitter:description"), 220)
    published = meta("article:published_time", "pubdate", "date")
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        published = t["datetime"] if t else ""
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe"]):
        tag.decompose()
    candidates = soup.select("article, [itemprop=articleBody], .entry-content, .post-content, .article-body, main") or [soup.body or soup]
    best_text = ""
    for c in candidates:
        text = "\n".join(p.get_text(" ", strip=True) for p in c.find_all(["p", "li", "h2", "h3"]))
        if len(text) > len(best_text):
            best_text = text
    best_text = re.sub(r"[ \t]+", " ", best_text).strip()
    return {"desc": desc, "published": published, "text": best_text}


def guess_tags(text: str) -> list[str]:
    scored = []
    for topic, kws in TOPIC_KEYWORDS.items():
        n = sum(text.count(k) for k in kws)
        if n:
            scored.append((n, topic))
    scored.sort(reverse=True)
    return [t for _, t in scored[:3]]


def ai_summarize(item: dict, text: str, api_key: str) -> dict | None:
    """Claude で一言まとめ・3点要約・トピックを作る。本文は保存しない。"""
    prompt = f"""あなたは人事担当者向けニュースキュレーションサイトの編集者です。下の記事を読み、JSONだけを返してください。

ルール:
- digest: 記事の要点を30字以内の一文で。見出しの言い換えではなく「何がわかるか」を書く
- summary: 要点を3つ。各40〜70字。数字や固有名詞は正確に。記事に書かれていないことは書かない
- 原文の文章をそのまま写さず、自分の言葉で言い換える
- tags: 次の候補から最大3つ: {"、".join(TOPICS)}
- 本文が取れておらず内容が判断できないときは summary を空配列にする

出力形式: {{"digest": "...", "summary": ["...", "...", "..."], "tags": ["..."]}}

媒体: {item['source']}
タイトル: {item['title']}
概要: {item.get('desc', '')}
本文（冒頭）:
{text[:6000]}"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": AI_MODEL, "max_tokens": 700, "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    out = "".join(b.get("text", "") for b in r.json().get("content", []))
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    data = json.loads(m.group(0))
    return {
        "digest": clean_text(str(data.get("digest", "")), 60),
        "summary": [clean_text(str(s), 120) for s in data.get("summary", [])][:3],
        "tags": [t for t in data.get("tags", []) if t in TOPICS][:3],
    }


def enrich(item: dict, api_key: str | None) -> None:
    item["enrich_tries"] = item.get("enrich_tries", 0) + 1
    text = ""
    try:
        if allowed_by_robots(item["url"]):
            art = extract_article(fetch(item["url"]).content)
            text = art["text"]
            if not item.get("desc") and art["desc"]:
                item["desc"] = art["desc"]
            if item.get("date_unknown") and art["published"]:
                dt = parse_date_text(art["published"]) or None
                if dt:
                    item["published"] = dt.isoformat()
                    item.pop("date_unknown", None)
            if len(text) > 200:
                item["read_min"] = max(1, round(len(text) / 600))
    except Exception as e:
        item["enrich_error"] = f"{type(e).__name__}"[:80]
    if not item.get("tags"):
        item["tags"] = guess_tags(f"{item['title']} {item.get('desc', '')} {text[:1500]}")
    if api_key and (text or item.get("desc")):
        try:
            ai = ai_summarize(item, text, api_key)
            if ai:
                item["digest"] = ai["digest"]
                item["summary"] = ai["summary"]
                if ai["tags"]:
                    item["tags"] = ai["tags"]
                item["ai"] = True
        except Exception as e:
            item["enrich_error"] = f"AI: {type(e).__name__}"[:80]
    item["enriched_at"] = datetime.now(JST).isoformat(timespec="minutes")


def needs_enrich(item: dict, has_key: bool) -> bool:
    if item.get("enrich_tries", 0) >= 3:
        return False
    if not item.get("enriched_at"):
        return True
    return has_key and not item.get("ai") and item.get("enrich_tries", 0) < 2


# ── 話題度（はてブ数の伸び＋メディア公式ランキング） ─────────
def update_hatena(items: list[dict], now: datetime) -> str | None:
    targets = [it for it in items if now - parse_iso(it["published"]) < timedelta(days=HATENA_WINDOW_DAYS) or it.get("rank_hist")]
    stamp = now.isoformat(timespec="minutes")
    try:
        for i in range(0, len(targets), 50):
            chunk = targets[i:i + 50]
            r = _session.get(
                "https://bookmark.hatenaapis.com/count/entries",
                params=[("url", it["url"]) for it in chunk], timeout=30,
            )
            r.raise_for_status()
            counts = r.json()
            for it in chunk:
                c = int(counts.get(it["url"], 0) or 0)
                hist = it.setdefault("hb_hist", [])
                if not hist or hist[-1][1] != c or (now - parse_iso(hist[-1][0])) > timedelta(hours=20):
                    hist.append([stamp, c])
                cutoff = now - timedelta(days=32)
                it["hb_hist"] = [h for h in hist if parse_iso(h[0]) > cutoff][-40:] or hist[-1:]
                it["hb"] = c
            time.sleep(1)
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:160]
    return None


def hb_delta(it: dict, now: datetime, days: int) -> int:
    hist = it.get("hb_hist") or []
    if not hist:
        return 0
    cur = hist[-1][1]
    if now - parse_iso(it["published"]) <= timedelta(days=days):
        return cur  # 期間内に出た記事は全件が「伸び」
    base = None
    for ts, c in hist:
        if now - parse_iso(ts) >= timedelta(days=days):
            base = c
    if base is None:
        base = hist[0][1]
    return max(0, cur - base)


def best_rank(it: dict, now: datetime, days: int) -> int | None:
    ranks = [r for ts, r, _ in it.get("rank_hist", []) if now - parse_iso(ts) <= timedelta(days=days)]
    return min(ranks) if ranks else None


def compute_buzz(items: list[dict], now: datetime) -> None:
    for it in items:
        buzz, signals = {}, {}
        for key, days in (("d1", 1), ("d7", 7), ("d30", 30)):
            hb = hb_delta(it, now, days)
            rank = best_rank(it, now, days)
            score = hb * 1.0 + (max(0, 11 - rank) * 4 if rank else 0)
            buzz[key] = round(score, 1)
            signals[key] = {"hb": hb, "rank": rank}
        it["buzz"] = buzz
        it["signals"] = signals
        total = it.get("hb", 0)
        rank = best_rank(it, now, 2)
        lvl = 0
        for th, v in ((1, 1), (5, 2), (20, 3), (60, 4)):
            if total >= th:
                lvl = v
        if rank:
            lvl = max(lvl, 4 if rank <= 3 else 3 if rank <= 6 else 2)
        it["heat"] = lvl


# ── 保存・サイト・Slack ───────────────────────────────────
def load_items() -> list[dict]:
    if not ITEMS_PATH.exists():
        return []
    items = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    # 旧版で残ったトラッキング付きURLを正規化し、重複をまとめる
    merged: dict[str, dict] = {}
    for it in items:
        it["url"] = normalize_url(it["url"]) if "?" in it["url"] and any(d in it["url"] for d in DROP_QUERY) else it["url"]
        it["id"] = make_id(it["url"])
        if it["id"] in merged:
            continue
        merged[it["id"]] = it
    return list(merged.values())


def save_items(items: list[dict]) -> list[dict]:
    items.sort(key=lambda x: x["published"], reverse=True)
    items = items[:KEEP_ITEMS]
    ITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ITEMS_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return items


PUBLIC_FIELDS = ("id", "title", "url", "source", "category", "published", "found_at", "desc", "digest",
                 "summary", "tags", "read_min", "hb", "heat", "buzz", "signals", "origin", "ai", "date_unknown")


def render_site(items: list[dict], sources: list[dict], rankings: list[dict], status: dict) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(JST)
    public = [{k: it[k] for k in PUBLIC_FIELDS if k in it and it[k] not in (None, "", [])} for it in items]
    for p, it in zip(public, items):
        if it.get("rank_hist"):
            ts, r, label = it["rank_hist"][-1]
            if now - parse_iso(ts) <= timedelta(days=2):
                p["rank"] = {"rank": r, "label": label}
    data = {
        "updated": now.isoformat(timespec="minutes"),
        "topics": TOPICS,
        "items": public,
        "sources": [
            {"name": s["name"], "category": s.get("category", ""), "url": s["url"],
             "enabled": s.get("enabled", True), "error": status["errors"].get(s["name"])}
            for s in sources
        ] + [
            {"name": r["name"], "category": "ランキング", "url": r["url"], "enabled": True,
             "error": status["errors"].get(r["name"])} for r in rankings
        ],
        "features": {"ai": status["ai"], "hatena_error": status.get("hatena_error")},
    }
    version = hashlib.sha1(json.dumps(data, ensure_ascii=False).encode()).hexdigest()[:10]
    (SITE_DIR / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    html = TEMPLATE_PATH.read_text(encoding="utf-8").replace("__DATA_VERSION__", version)
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")


def slack_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_slack_payload(new_items: list[dict], site_url: str | None) -> dict:
    today = datetime.now(JST).strftime("%-m/%-d")
    shown = new_items[:SLACK_MAX_ITEMS]
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📰 人事ニュース {today}（{len(new_items)}件）"}},
    ]
    by_source: dict[str, list[dict]] = {}
    for it in shown:
        by_source.setdefault(it["source"], []).append(it)
    for source, its in by_source.items():
        lines = [f"*{slack_escape(source)}*"] + [f"• <{it['url']}|{slack_escape(it['title'])}>" for it in its]
        text = "\n".join(lines)
        while text:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}})
            text = text[2900:]
    footer = []
    if len(new_items) > len(shown):
        footer.append(f"ほか {len(new_items) - len(shown)} 件")
    if site_url:
        footer.append(f"<{site_url}|ニュースサイトで一覧を見る>")
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "　".join(footer)}]})
    return {"text": f"人事ニュース {today}（{len(new_items)}件）", "blocks": blocks[:50],
            "unfurl_links": False, "unfurl_media": False}


def post_to_slack(payload: dict, webhook: str) -> None:
    r = requests.post(webhook, json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Slack投稿に失敗: {r.status_code} {r.text}")


def source_for_url(url: str, sources: list[dict]) -> dict | None:
    host = urlsplit(url).netloc
    for s in sources:
        if s.get("link_pattern") and re.search(s["link_pattern"], url):
            return s
    for s in sources:
        if urlsplit(s["url"]).netloc == host:
            return s
    return None


# ── メイン ──────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--no-enrich", action="store_true", help="概要・要約の補完をしない（調整用）")
    args = ap.parse_args()

    conf = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    sources, rankings = conf["sources"], conf.get("rankings", [])
    enabled = [s for s in sources if s.get("enabled", True)]
    api_key = os.environ.get("ANTHROPIC_API_KEY") or None
    items = load_items()
    if not items and not args.seed:
        print("初回実行のため、既存記事を既読登録します（Slack投稿なし）", file=sys.stderr)
        args.seed = True
    by_id = {it["id"]: it for it in items}
    now = datetime.now(JST)
    new_items: list[dict] = []
    errors: dict[str, str] = {}

    # 1) 一覧から新着
    for src in enabled:
        if args.only and args.only not in src["name"]:
            continue
        print(f"- {src['name']}", file=sys.stderr)
        try:
            entries = collect_source(src)
        except Exception as e:
            errors[src["name"]] = f"{type(e).__name__}: {e}"[:200]
            print(f"  [error] {errors[src['name']]}", file=sys.stderr)
            continue
        if not entries:
            errors[src["name"]] = "記事が見つかりませんでした（ページ構成が変わった可能性）"
        added = 0
        for e in entries:
            url = normalize_url(e["url"]) if src["type"] == "html" else e["url"]
            iid = make_id(url)
            if iid in by_id:
                old = by_id[iid]
                if e["desc"] and not old.get("desc"):
                    old["desc"] = e["desc"]
                continue
            it = {"id": iid, "title": e["title"], "url": url, "source": src["name"],
                  "category": src.get("category", ""), "published": (e["dt"] or now).isoformat(timespec="minutes"),
                  "found_at": now.isoformat(timespec="minutes")}
            if not e["dt"]:
                it["date_unknown"] = True
            if e["desc"]:
                it["desc"] = e["desc"]
            by_id[iid] = it
            items.append(it)
            new_items.append(it)
            added += 1
        print(f"  {len(entries)}件取得 / 新着 {added}件", file=sys.stderr)

    # 2) メディア公式ランキング
    if not args.only:
        for rk in rankings:
            try:
                ranked = collect_ranking(rk)
            except Exception as e:
                errors[rk["name"]] = f"{type(e).__name__}: {e}"[:200]
                continue
            if not ranked:
                errors[rk["name"]] = "ランキングが見つかりませんでした（ページ構成が変わった可能性）"
                continue
            stamp = now.isoformat(timespec="minutes")
            for pos, (url, title) in enumerate(ranked, 1):
                iid = make_id(url)
                it = by_id.get(iid)
                if it is None and len(title) >= 6:
                    src = source_for_url(url, enabled)
                    it = {"id": iid, "title": title, "url": url, "source": src["name"] if src else rk["name"],
                          "category": src.get("category", "") if src else "", "published": stamp,
                          "found_at": stamp, "origin": "ranking", "date_unknown": True}
                    by_id[iid] = it
                    items.append(it)
                if it is None:
                    continue
                hist = it.setdefault("rank_hist", [])
                if not hist or hist[-1][0][:10] != stamp[:10] or hist[-1][1] != pos:
                    hist.append([stamp, pos, rk["label"]])
                it["rank_hist"] = hist[-30:]

    # 3) 概要・要約・タグの補完（新しい記事から）
    if not args.no_enrich:
        todo = [it for it in sorted(items, key=lambda x: x["published"], reverse=True) if needs_enrich(it, bool(api_key))]
        for it in todo[:ENRICH_PER_RUN]:
            enrich(it, api_key)

    # 4) 話題度
    hatena_error = None if args.only else update_hatena(items, now)
    compute_buzz(items, now)

    items = save_items(items)
    render_site(items, sources, rankings, {"errors": errors, "ai": bool(api_key), "hatena_error": hatena_error})

    if args.seed:
        print(f"既読として {len(new_items)} 件を登録しました（Slack投稿なし）")
        return 0
    if not new_items:
        print("新着はありません")
        return 0
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook and not args.dry_run:
        print(f"新着 {len(new_items)} 件（Slack未設定のためサイトのみ更新）")
        return 0
    new_items.sort(key=lambda x: x["published"], reverse=True)
    payload = build_slack_payload(new_items, os.environ.get("SITE_URL"))
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        post_to_slack(payload, webhook)
        print(f"Slackに {len(new_items)} 件を投稿しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())

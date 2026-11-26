"""人事系メディアの新着を集め、Slackに投稿し、ニュースサイト(docs/index.html)を生成する。

使い方:
  python collect.py                 # 収集 → 新着をSlackへ → サイト生成
  python collect.py --dry-run       # Slackには送らず、送る内容を表示
  python collect.py --seed          # 初回用：今ある記事を既読として登録だけする（Slackに流さない）

環境変数:
  SLACK_WEBHOOK_URL   Slack Incoming Webhook のURL（未設定ならSlack投稿をスキップ）
  SITE_URL            公開サイトのURL（Slackメッセージ末尾にリンクを付ける。任意）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.robotparser
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
ITEMS_PATH = ROOT / "data" / "items.json"
SITE_PATH = ROOT / "docs" / "index.html"
TEMPLATE_PATH = ROOT / "templates" / "index.html"

JST = timezone(timedelta(hours=9))
USER_AGENT = "HRNewsDigestBot/1.0 (+internal Slack digest; contact: your-team@example.com)"
REQUEST_INTERVAL_SEC = 2.0     # 同じ実行内でのリクエスト間隔（相手サイトへの配慮）
MAX_ITEMS_PER_SOURCE = 20      # 1ソースから1回に取り込む上限
KEEP_ITEMS = 1500              # items.json に保持する件数
SLACK_MAX_ITEMS = 40           # 1回のSlack投稿に載せる上限

DATE_RE = re.compile(r"(20\d{2})[./年-]\s*(\d{1,2})[./月-]\s*(\d{1,2})")


@dataclass
class Item:
    id: str
    title: str
    url: str
    source: str
    category: str
    published: str   # ISO8601 (JST)。不明なら初回検出時刻
    found_at: str


# ── 取得 ────────────────────────────────────────────
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_last_request = 0.0


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
    global _last_request
    wait = REQUEST_INTERVAL_SEC - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    r = _session.get(url, timeout=30)
    r.raise_for_status()
    return r


def normalize_url(url: str) -> str:
    """トラッキング用クエリ(?newstop=new 等)を落として重複判定を安定させる。"""
    parts = urlsplit(url)
    keep = "&".join(
        q for q in parts.query.split("&")
        if q and not q.startswith(("utm_", "newstop=", "fbclid=", "ref="))
    )
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


def collect_rss(src: dict, content: bytes) -> list[tuple[str, str, datetime | None]]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = (e.get("title") or "").strip()
        link = e.get("link")
        if not title or not link:
            continue
        st = e.get("published_parsed") or e.get("updated_parsed")
        dt = datetime(*st[:6], tzinfo=timezone.utc).astimezone(JST) if st else None
        out.append((title, link, dt))
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
    """リンクの周辺（小さめの祖先要素）から日付らしい文字列を拾う。"""
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


def collect_html(src: dict, content: bytes, base_url: str) -> list[tuple[str, str, datetime | None]]:
    soup = BeautifulSoup(content, "html.parser")
    pattern = re.compile(src["link_pattern"])
    best: dict[str, tuple[str, bool, datetime | None]] = {}
    order: list[str] = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"])
        if not pattern.search(url):
            continue
        key = normalize_url(url)
        title, from_heading = _anchor_title(a)
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 6:          # 画像だけのリンクや「続きを読む」は捨てる
            if key not in best:
                best.setdefault(key, ("", False, _nearby_date(a)))
                order.append(key)
            continue
        title = DATE_RE.sub("", title).strip(" |｜-") or title  # 先頭に日付が混ざる場合
        if len(title) > 140:
            title = title[:140] + "…"
        prev = best.get(key)
        dt = (prev[2] if prev else None) or _nearby_date(a)
        if key not in best:
            order.append(key)
        if prev is None or not prev[0] or (from_heading and not prev[1]) or (
            from_heading == prev[1] and len(title) < len(prev[0])
        ):
            best[key] = (title, from_heading, dt)
        else:
            best[key] = (prev[0], prev[1], dt)
    out = [(best[k][0], k, best[k][2]) for k in order if best[k][0]]
    return out[:MAX_ITEMS_PER_SOURCE]


def collect_source(src: dict) -> list[tuple[str, str, datetime | None]]:
    url = src["url"]
    if not allowed_by_robots(url):
        print(f"  [skip] robots.txt で禁止: {url}", file=sys.stderr)
        return []
    r = fetch(url)
    if src["type"] == "rss":
        entries = collect_rss(src, r.content)
    else:
        entries = collect_html(src, r.content, r.url)
    kws = src.get("include_keywords")
    if kws:
        entries = [e for e in entries if any(k in e[0] for k in kws)]
    return entries


# ── 保存・Slack・サイト ─────────────────────────────
def load_items() -> list[dict]:
    if ITEMS_PATH.exists():
        return json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    return []


def save_items(items: list[dict]) -> None:
    items.sort(key=lambda x: x["published"], reverse=True)
    ITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ITEMS_PATH.write_text(json.dumps(items[:KEEP_ITEMS], ensure_ascii=False, indent=1), encoding="utf-8")


def slack_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_slack_payload(new_items: list[Item], site_url: str | None) -> dict:
    today = datetime.now(JST).strftime("%-m/%-d")
    shown = new_items[:SLACK_MAX_ITEMS]
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📰 人事ニュース {today}（{len(new_items)}件）"}},
    ]
    by_source: dict[str, list[Item]] = {}
    for it in shown:
        by_source.setdefault(it.source, []).append(it)
    for source, its in by_source.items():
        lines = [f"*{slack_escape(source)}*"]
        for it in its:
            lines.append(f"• <{it.url}|{slack_escape(it.title)}>")
        text = "\n".join(lines)
        # section の text は 3000 文字まで
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
    return {
        "text": f"人事ニュース {today}（{len(new_items)}件）",
        "blocks": blocks[:50],  # Slackの上限
        "unfurl_links": False,
        "unfurl_media": False,
    }


def post_to_slack(payload: dict, webhook: str) -> None:
    r = requests.post(webhook, json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Slack投稿に失敗: {r.status_code} {r.text}")


def render_site(items: list[dict], sources: list[dict], errors: dict[str, str]) -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    meta = {
        "updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "sources": [
            {"name": s["name"], "category": s.get("category", ""), "url": s["url"],
             "enabled": s.get("enabled", True), "error": errors.get(s["name"])}
            for s in sources
        ],
    }
    data = json.dumps({"items": items, "meta": meta}, ensure_ascii=False)
    data = data.replace("</", "<\\/")  # </script> 対策
    SITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SITE_PATH.write_text(template.replace("/*__DATA__*/null", data), encoding="utf-8")


# ── メイン ──────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Slackに送らず内容を表示")
    ap.add_argument("--seed", action="store_true", help="既存記事を既読登録するだけ（Slackに流さない）")
    ap.add_argument("--only", help="このソース名を含むものだけ実行（調整用）")
    args = ap.parse_args()

    sources = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))["sources"]
    items = load_items()
    if not items and not args.seed:
        # 初回はまだ既読が何もない。全記事をSlackに流さないよう、自動で既読登録だけにする
        print("初回実行のため、既存記事を既読登録します（Slack投稿なし）", file=sys.stderr)
        args.seed = True
    known = {it["id"] for it in items}
    now = datetime.now(JST)
    new_items: list[Item] = []
    errors: dict[str, str] = {}

    for src in sources:
        if not src.get("enabled", True):
            continue
        if args.only and args.only not in src["name"]:
            continue
        print(f"- {src['name']}", file=sys.stderr)
        try:
            entries = collect_source(src)
        except Exception as e:  # 1サイトの失敗で全体を止めない
            errors[src["name"]] = f"{type(e).__name__}: {e}"[:200]
            print(f"  [error] {errors[src['name']]}", file=sys.stderr)
            continue
        if not entries:
            errors[src["name"]] = "記事が見つかりませんでした（ページ構成が変わった可能性）"
        added = 0
        for title, url, dt in entries:
            iid = make_id(url)
            if iid in known:
                continue
            known.add(iid)
            it = Item(
                id=iid, title=title, url=normalize_url(url) if src["type"] == "html" else url,
                source=src["name"], category=src.get("category", ""),
                published=(dt or now).isoformat(), found_at=now.isoformat(),
            )
            new_items.append(it)
            items.append(asdict(it))
            added += 1
        print(f"  {len(entries)}件取得 / 新着 {added}件", file=sys.stderr)

    new_items.sort(key=lambda x: x.published, reverse=True)
    save_items(items)
    render_site(sorted(items, key=lambda x: x["published"], reverse=True)[:KEEP_ITEMS], sources, errors)

    if args.seed:
        print(f"既読として {len(new_items)} 件を登録しました（Slack投稿なし）")
        return 0
    if not new_items:
        print("新着はありません")
        return 0

    payload = build_slack_payload(new_items, os.environ.get("SITE_URL"))
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if args.dry_run or not webhook:
        if not webhook and not args.dry_run:
            print("SLACK_WEBHOOK_URL が未設定のため投稿をスキップしました", file=sys.stderr)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        post_to_slack(payload, webhook)
        print(f"Slackに {len(new_items)} 件を投稿しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())

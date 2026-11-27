"""ネットワークなしで、収集・補完・話題度・サイト生成を確認するテスト。
実行: python -m pytest tests -q
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collect  # noqa: E402

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>HRog</title><link>https://hrog.net</link><description>x</description>
<item><title>現役介護職の調査</title><link>https://hrog.net/news/137448/</link>
<description><![CDATA[<p>介護職の7割が追加収入を望んでいる。</p>]]></description>
<pubDate>Wed, 25 Nov 2026 23:00:00 +0000</pubDate></item>
<item><title>Item B 採用の話</title><link>https://hrog.net/news/137424/</link><pubDate>Tue, 24 Nov 2026 23:00:00 +0000</pubDate></item>
</channel></rss>""".encode()

JINJIBU = """<html><body><ul>
<li><a href="/news/detl/27164/?newstop=new"><img src="a.jpg"></a>
    <a href="/news/detl/27164/?newstop=new"><p class="title">【書籍紹介】『新庄監督に学ぶ 弱い組織を強い組織に変える方法』</p>
    <p>本書は…という内容の長い抜粋文がここに入ります。</p></a>
    <span>2026/11/26</span></li>
<li><a href="/news/detl/27155/?newstop=new">【書籍紹介】『天職の科学』（初見 康行：著）</a><span>2026/11/25</span></li>
</ul>
<div><h3>ニュースアクセスランキング</h3><ol>
<li><a href="/news/detl/24888/?rclk=sd_nw_rank">転職動向調査2025年版（2024年実績）</a></li>
<li><a href="/news/detl/27155/?rclk=sd_nw_rank">【書籍紹介】『天職の科学』</a></li>
</ol></div></body></html>""".encode()

HROG_HOME = """<html><body><nav><a href="https://hrog.net/news/1/">ナビの記事リンク</a></nav>
<section><h2>人気記事ランキング</h2><ul>
<li><a href="https://hrog.net/news/137339/">28卒の内々定獲得率は23.7％</a></li>
<li><a href="https://hrog.net/news/137448/">現役介護職の調査</a></li>
<li><a href="https://hrog.net/report/137006/">ハローワークから民間サービスへ</a></li>
</ul></section></body></html>""".encode()

ARTICLE = """<html><head><meta property="og:description" content="記事の概要です。人的資本の開示が進む。">
<meta property="article:published_time" content="2025-03-01T10:00:00+09:00"></head>
<body><nav>メニュー</nav><article><h2>見出し</h2>""" + "<p>" + "本文の段落です。" * 80 + "</p></article></body></html>"


def test_rss_parse_has_desc():
    out = collect.collect_rss({}, RSS)
    assert out[0]["title"] == "現役介護職の調査"
    assert out[0]["desc"] == "介護職の7割が追加収入を望んでいる。"
    assert out[0]["dt"].isoformat().startswith("2026-11-26T08:00")


def test_html_parse_prefers_heading_and_dedupes():
    src = {"link_pattern": r"^https://jinjibu\.jp/news/detl/\d+/\?newstop"}
    out = collect.collect_html(src, JINJIBU, "https://jinjibu.jp/news/")
    assert [e["url"] for e in out] == ["https://jinjibu.jp/news/detl/27164/", "https://jinjibu.jp/news/detl/27155/"]
    assert out[0]["title"].startswith("【書籍紹介】『新庄監督") and "抜粋" not in out[0]["title"]
    assert out[1]["dt"].date().isoformat() == "2026-11-25"


def test_normalize_drops_tracking():
    assert collect.normalize_url("https://jinjibu.jp/news/detl/1/?search=rank") == "https://jinjibu.jp/news/detl/1/"
    assert collect.normalize_url("https://www.hrpro.co.jp/trend_news.php?news_no=3") == "https://www.hrpro.co.jp/trend_news.php?news_no=3"


class R:
    def __init__(self, c, u):
        self.content, self.url = c, u


def test_ranking_marker_and_section(monkeypatch):
    monkeypatch.setattr(collect, "allowed_by_robots", lambda u: True)
    monkeypatch.setattr(collect, "fetch", lambda u: R(JINJIBU if "jinjibu" in u else HROG_HOME, u))
    jr = collect.collect_ranking({"url": "https://jinjibu.jp/", "link_pattern": r"^https://jinjibu\.jp/news/detl/\d+/\?rclk=sd_nw_rank"})
    assert [u for u, _ in jr] == ["https://jinjibu.jp/news/detl/24888/", "https://jinjibu.jp/news/detl/27155/"]
    hr = collect.collect_ranking({"url": "https://hrog.net/", "section_heading": "人気記事ランキング", "link_pattern": r"^https://hrog\.net/[a-z]+/\d+/"})
    assert [u for u, _ in hr] == ["https://hrog.net/news/137339/", "https://hrog.net/news/137448/", "https://hrog.net/report/137006/"]


def test_extract_article_and_tags():
    art = collect.extract_article(ARTICLE.encode())
    assert art["desc"].startswith("記事の概要") and "メニュー" not in art["text"] and len(art["text"]) > 500
    assert "人的資本経営" in collect.guess_tags("人的資本の開示について")


def test_buzz_growth_and_rank():
    now = datetime(2026, 11, 27, 12, tzinfo=collect.JST)
    old = (now - timedelta(days=10)).isoformat()
    it = {"published": old, "hb_hist": [[(now - timedelta(days=3)).isoformat(), 10], [(now - timedelta(hours=30)).isoformat(), 12], [now.isoformat(), 40]],
          "hb": 40, "rank_hist": [[now.isoformat(), 2, "X"]]}
    collect.compute_buzz([it], now)
    assert it["signals"]["d1"] == {"hb": 28, "rank": 2}
    assert it["buzz"]["d1"] == 28 + 36
    assert it["signals"]["d7"]["hb"] == 30
    assert it["heat"] == 4


def test_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "ITEMS_PATH", tmp_path / "items.json")
    monkeypatch.setattr(collect, "SITE_DIR", tmp_path / "site")
    monkeypatch.setattr(collect, "allowed_by_robots", lambda u: True)

    def fake_fetch(u):
        if "feed" in u:
            return R(RSS, u)
        if u in ("https://jinjibu.jp/", "https://jinjibu.jp/news/"):
            return R(JINJIBU, u)
        if u == "https://hrog.net/":
            return R(HROG_HOME, u)
        return R(ARTICLE.encode(), u)
    monkeypatch.setattr(collect, "fetch", fake_fetch)

    class HB:
        def __init__(self, params): self.params = params
        def raise_for_status(self): pass
        def json(self): return {u: 7 for _, u in self.params}
    monkeypatch.setattr(collect._session, "get", lambda url, params=None, timeout=None: HB(params))
    monkeypatch.setattr(collect, "ai_summarize", lambda it, text, key: {"digest": "要点", "summary": ["a", "b", "c"], "tags": ["採用"]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)

    monkeypatch.setattr(sys, "argv", ["collect.py"])
    assert collect.main() == 0
    items = json.loads((tmp_path / "items.json").read_text())
    by_url = {i["url"]: i for i in items}
    assert "https://hrog.net/news/137448/" in by_url and "https://jinjibu.jp/news/detl/27164/" in by_url
    # ランキングにだけ出た古い記事も、話題として取り込まれ、記事ページから日付が補われる
    ranked = by_url["https://jinjibu.jp/news/detl/24888/"]
    assert ranked["origin"] == "ranking" and ranked["published"].startswith("2025-03-01") and "date_unknown" not in ranked
    assert by_url["https://hrog.net/news/137448/"]["rank_hist"][0][1] == 2
    assert all(i.get("summary") == ["a", "b", "c"] for i in items)
    assert all(i.get("hb") == 7 for i in items if i["published"] > "2026-10")

    # 2回目：新着ゼロ、件数変わらず
    n = len(items)
    assert collect.main() == 0
    assert len(json.loads((tmp_path / "items.json").read_text())) == n

    data = json.loads((tmp_path / "site" / "data.json").read_text())
    assert data["features"]["ai"] is True and data["items"][0]["buzz"]
    assert "text" not in json.dumps(data["items"][0])  # 本文は公開データに含めない
    html = (tmp_path / "site" / "index.html").read_text()
    assert "__DATA_VERSION__" not in html


def test_slack_payload_groups_by_source():
    items = [{"title": f"記事<{i}>", "url": f"https://e.com/{i}", "source": "HRog" if i % 2 else "HRプロ"} for i in range(5)]
    p = collect.build_slack_payload(items, "https://example.github.io/hr-news/")
    body = json.dumps(p, ensure_ascii=False)
    assert "記事&lt;1&gt;" in body and len(p["blocks"]) <= 50

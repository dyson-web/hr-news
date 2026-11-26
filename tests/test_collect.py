"""ネットワークなしで、パース・重複排除・Slackペイロード・サイト生成を確認するテスト。
実行: python -m pytest tests/ -q   （または python tests/test_collect.py）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collect  # noqa: E402

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>HRog</title><link>https://hrog.net</link><description>x</description>
<item><title>\xe7\x8f\xbe\xe5\xbd\xb9\xe4\xbb\x8b\xe8\xad\xb7\xe8\x81\xb7\xe3\x81\xae\xe8\xaa\xbf\xe6\x9f\xbb</title><link>https://hrog.net/news/137448/</link>
<pubDate>Wed, 25 Nov 2026 23:00:00 +0000</pubDate></item>
<item><title>Item B</title><link>https://hrog.net/news/137424/</link><pubDate>Tue, 24 Nov 2026 23:00:00 +0000</pubDate></item>
</channel></rss>"""

JINJIBU = """<html><body><ul>
<li><a href="/news/detl/27164/?newstop=new"><img src="a.jpg"></a>
    <a href="/news/detl/27164/?newstop=new"><p class="title">【書籍紹介】『新庄監督に学ぶ 弱い組織を強い組織に変える方法』</p>
    <p>本書は…という内容の長い抜粋文がここに入ります。本書は…という内容の長い抜粋文。</p></a>
    <span>2026/11/26</span> <span>書籍 - リーダーシップ</span></li>
<li><a href="/news/detl/27155/?newstop=new">【書籍紹介】『天職の科学』（初見 康行：著）</a><span>2026/11/25</span></li>
<li><a href="/news/">ニュース一覧</a></li>
</ul></body></html>""".encode()


def test_rss_parse():
    out = collect.collect_rss({}, RSS)
    assert len(out) == 2
    assert out[0][0] == "現役介護職の調査"
    assert out[0][2].isoformat().startswith("2026-11-26T08:00")  # UTC→JST


def test_html_parse_prefers_heading_and_dedupes():
    src = {"link_pattern": r"^https://jinjibu\.jp/news/detl/\d+/"}
    out = collect.collect_html(src, JINJIBU, "https://jinjibu.jp/news/")
    assert [u for _, u, _ in out] == ["https://jinjibu.jp/news/detl/27164/", "https://jinjibu.jp/news/detl/27155/"]
    assert out[0][0].startswith("【書籍紹介】『新庄監督")
    assert "抜粋" not in out[0][0]
    assert out[0][2].date().isoformat() == "2026-11-26"
    assert out[1][2].date().isoformat() == "2026-11-25"


def test_slack_payload_groups_by_source():
    items = [collect.Item(id=str(i), title=f"記事<{i}>", url=f"https://e.com/{i}", source="HRog" if i % 2 else "HRプロ",
                          category="x", published="2026-11-26T08:00:00+09:00", found_at="") for i in range(5)]
    p = collect.build_slack_payload(items, "https://example.github.io/hr-news/")
    assert p["blocks"][0]["type"] == "header"
    body = json.dumps(p, ensure_ascii=False)
    assert "記事&lt;1&gt;" in body and "ニュースサイトで一覧を見る" in body
    assert len(p["blocks"]) <= 50


def test_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "ITEMS_PATH", tmp_path / "items.json")
    monkeypatch.setattr(collect, "SITE_PATH", tmp_path / "index.html")
    monkeypatch.setattr(collect, "allowed_by_robots", lambda u: True)

    class R:
        def __init__(self, c, u): self.content, self.url = c, u
    monkeypatch.setattr(collect, "fetch", lambda u: R(RSS if "feed" in u else JINJIBU, u))
    monkeypatch.setattr(sys, "argv", ["collect.py", "--dry-run", "--only", "HRog"])
    assert collect.main() == 0
    first = json.loads((tmp_path / "items.json").read_text())
    assert len(first) == 2
    # 2回目は新着ゼロ（重複排除）
    assert collect.main() == 0
    assert len(json.loads((tmp_path / "items.json").read_text())) == 2
    html = (tmp_path / "index.html").read_text()
    assert "/*__DATA__*/null" not in html and "現役介護職の調査" in html


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

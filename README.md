# HR News Digest

人事系メディアの新着記事を定期的に集めて、**Slackに1日2回まとめて投稿**し、同じ内容を**ニュースサイト（GitHub Pages）**として一覧できるようにする仕組みです。サーバーは不要で、GitHub Actions だけで動きます。

```
GitHub Actions（平日 8:47 / 17:47 JST）
   └ collect.py
       ├ RSS があるメディア → RSS を読む
       ├ RSS がないメディア → 一覧ページから記事リンクを抽出
       ├ data/items.json と照合して新着だけ抽出（重複排除）
       ├ Slack Incoming Webhook に1メッセージで投稿（メディアごとにまとめる）
       └ docs/index.html（ニュースサイト）を再生成 → コミット → Pages で公開
```

## 収集対象（sources.yaml）

| メディア | 取得方法 | 状態 |
|---|---|---|
| 日本の人事部（ニュース／記事） | 一覧ページ | ✅ |
| HRプロ（トレンドニュース／コラム） | 一覧ページ | ✅ |
| HRog | RSS | ✅ |
| 労基旬報オンライン | RSS | ✅ |
| フライヤー（お知らせ） | 一覧ページ | ✅ |
| 令三社（Substackニュースレター） | RSS | ✅ |
| DIAMONDハーバード・ビジネス・レビュー | RSS（人事・組織系キーワードで絞り込み） | ✅ |
| 企業と人材（産労総合研究所のお知らせ） | 一覧ページ | ✅ |
| ひつじんじ／みんなの採用ラボ／人事のへや（note） | RSS | ✅ |
| RMS Message、HITO（パーソル総研） | 一覧がJavaScript描画 | ⏸ 停止中 |
| 労政時報 | 記事は会員制 | ⏸ 停止中 |

JSHRM・グローバル人事塾・壺中人事塾・人事図書館などのコミュニティは、ニュース配信というよりイベント告知中心のため入れていません。必要なら下の「メディアを追加する」の手順で足せます。

## セットアップ（15分ほど）

### 1. GitHub にリポジトリを作る
このフォルダの中身をそのまま push します（private でも動きます。ただし private で Pages を使うには有料プランが必要）。

### 2. Slack の Incoming Webhook を作る
1. https://api.slack.com/apps → **Create New App** → From scratch
2. **Incoming Webhooks** を On → **Add New Webhook to Workspace** → 投稿先チャンネルを選ぶ
3. 発行された `https://hooks.slack.com/services/...` をコピー

### 3. GitHub に設定を登録
リポジトリの **Settings → Secrets and variables → Actions**
- **Secrets** に `SLACK_WEBHOOK_URL` = 上の Webhook URL
- **Variables** に `SITE_URL` = `https://dyson-web.github.io/hr-news/`（任意。Slack投稿の末尾にサイトへのリンクが付きます）

### 4. GitHub Pages を有効化
**Settings → Pages** → Source: *Deploy from a branch* → Branch: `main` / フォルダ: `/docs`

### 5. 初回は「既読登録」だけ実行する
最初に普通に実行すると、各メディアの既存記事（数十〜百件以上）がまとめてSlackに流れてしまいます。
**Actions → HR news collect → Run workflow** で mode に `seed` と入れて実行してください。今ある記事を既読として登録し、サイトだけ生成します。

以降は平日 8:47 / 17:47 に自動で動き、新着だけがSlackに届きます。

> 投稿内容を事前に見たいときは mode に `dry-run` と入れて実行すると、Slackに送らずログに出力します。

## 運用

- **取得できなくなったメディアの確認**：サイト最下部の「収集元とステータス」にエラーが出ます。サイト側のURL構成が変わった場合は `sources.yaml` の `link_pattern` を直してください。
- **時刻を変える**：`.github/workflows/collect.yml` の `cron`（UTC表記。JST = UTC + 9時間）
- **件数が多すぎる**：`collect.py` の `SLACK_MAX_ITEMS`（1回の投稿の上限）や、ソースごとの `include_keywords` で絞れます。

### メディアを追加する
`sources.yaml` に追記するだけです。

```yaml
  # RSSがある場合（WordPressなら多くは https://サイト/feed/ 、noteなら https://note.com/ユーザー名/rss）
  - name: 表示名
    type: rss
    url: https://example.com/feed/
    category: 人事情報サイト

  # RSSがない場合：一覧ページURLと、記事URLの正規表現
  - name: 表示名
    type: html
    url: https://example.com/news/
    link_pattern: '^https://example\.com/news/\d+'
    category: 人事情報サイト
```

追加したソースだけ試すとき（ローカル）：
```bash
pip install -r requirements.txt
python collect.py --dry-run --only 表示名
```

## 注意点
- 各サイトの `robots.txt` を確認し、禁止されているページは取得しません。リクエストは2秒間隔、1日2回です。
- 取得するのは**タイトル・URL・日付だけ**で、本文は転載しません。サイトもSlackも元記事へのリンク集です。
- サイトによっては利用規約で機械的な取得を禁じている場合があります。社内利用でも、追加するメディアの規約は一度確認してください。RSSが提供されているメディアはRSSでの取得を優先しています。
- `collect.py` の `USER_AGENT` にある連絡先（`your-team@example.com`）は、自社の問い合わせ先に書き換えてください。

## テスト
```bash
pip install pytest && python -m pytest tests -q
```
ネットワークなしで、RSS/HTMLのパース、重複排除、Slackメッセージ、サイト生成を確認します。

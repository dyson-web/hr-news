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

## サイトでできること

- **新着**：日付ごとの記事一覧。トピック（採用・労務・法改正など）と媒体で絞り込み、未読だけ表示
- **ワンタップで中身を確認**：記事を押すとパネルが開き、一言まとめ・3点要約（AI）・媒体の概要・話題度が見られる。前後の記事へ移動もできる
- **話題**：24時間／7日間／30日間でよく読まれている記事のランキング
- **保存**：しおりを押した記事をまとめて見る（ブラウザに保存）
- キーボード操作：`/` 検索、`J` `K` 移動、`Enter` 開く、`O` 元記事、`S` 保存、`Esc` 閉じる

### 話題度の決め方
外部メディアの記事PVは公開されていないため、外から見える反応で代用しています。
- はてなブックマーク数の伸び（期間内の増加数）
- 各メディア公式のアクセスランキング順位（日本の人事部、HRog。`sources.yaml` の `rankings`）

記事の左端の色が話題度です（灰→黄→橙→赤→深紅）。

### AI要約を有効にする
Settings → Secrets and variables → Actions → Secrets に `ANTHROPIC_API_KEY` を登録すると、新しい記事から順に一言まとめ・3点要約・トピック分類が付きます（モデルは claude-haiku-5-5、1記事あたりの費用はごくわずか）。未設定のときは、媒体が出している概要文とキーワードによるトピック分類だけを表示します。記事本文はAIに渡すだけで、リポジトリやサイトには保存しません。

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

### 5. 初回実行（自動）
`sources.yaml` や `collect.py` を push すると自動で実行されます。初回（`data/items.json` が空のとき）は既存記事を既読登録するだけで、Slackには流しません。
以降は平日 8:47 / 17:47 に自動で動き、新着だけがSlackに届きます。Slackを使わずサイトだけで見る場合は、手順2・3は不要です。

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

## MCPサーバー（Claude から記事を引く）

`mcp/` に、このサイトの記事データ（`docs/data.json`）を Claude から使うための MCP サーバーがあります。
「今日の人事ニュースを教えて」「今週話題の採用関連記事は？」「人的資本開示の最近の記事をまとめて」のように頼めます。

| ツール | できること |
|---|---|
| `get_latest_hr_news` | 新着（日数・媒体・トピックで絞り込み） |
| `get_trending_hr_news` | 話題ランキング（24h / 7d / 30d） |
| `search_hr_news` | キーワード・媒体・トピック・期間で検索 |
| `get_hr_article` | 1件の詳細（要約・概要・話題度・元記事URL） |
| `list_hr_media_and_topics` | 媒体・トピック一覧と最終更新時刻 |

### A. claude.ai（Web・スマホ）から使う：Cloudflare Workers に公開
1. Cloudflare にログイン → My Profile → API Tokens → **Create Token** → テンプレート「Edit Cloudflare Workers」で作成
2. Cloudflare ダッシュボード右側の **Account ID** をコピー
3. GitHub の Settings → Secrets and variables → Actions → Secrets に
   `CLOUDFLARE_API_TOKEN` と `CLOUDFLARE_ACCOUNT_ID` を登録
4. Actions → **Deploy MCP server** → Run workflow（以降は `mcp/` を変更すると自動で再デプロイ）
5. ログに出る URL（`https://hr-news-mcp.<あなたのサブドメイン>.workers.dev`）の末尾に `/mcp` を付け、
   claude.ai の 設定 → コネクタ → **カスタムコネクタを追加** に登録

公開データしか扱わないため認証はありません（URLを知っていれば誰でも読めます）。

### B. Claude Desktop / Claude Code から使う：ローカルで起動
Node.js 18 以上があれば、追加のインストールは不要です。リポジトリを clone して、Claude Desktop の設定（`claude_desktop_config.json`）に追記します。

```json
{
  "mcpServers": {
    "hr-news": { "command": "node", "args": ["/path/to/hr-news/mcp/stdio.js"] }
  }
}
```

Claude Code なら `claude mcp add hr-news -- node /path/to/hr-news/mcp/stdio.js`。

テスト：`cd mcp && node --test`

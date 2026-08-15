# 熊本地震 支援情報ナビ（非公式）

令和8年熊本地震における国・熊本県・宇城市（小川町）の公的支援情報（中小企業・事業者向け／生活再建向け）を、
毎朝自動で収集・要約して公開する静的サイトです。

```
.
├── .github/workflows/daily-update.yml   # 毎朝の収集＆デプロイ用ワークフロー
├── scripts/
│   ├── update_data.py                   # 収集〜Gemini構造化〜JSON更新
│   └── requirements.txt
├── public/data/support_info.json        # 支援情報データ本体（自動更新される）
└── src/index.html                       # フロントエンド（検索・絞り込みUI）
```

## セットアップ手順

### 1. GitHub リポジトリの準備
1. このフォルダの中身をそのまま新しい GitHub リポジトリの直下に配置して push してください。
2. リポジトリの **Settings → Pages** で、`Source` を **GitHub Actions** に設定してください
   （`gh-pages` ブランチ運用ではなく、`actions/deploy-pages` を使う方式のため）。

### 2. Gemini API キーの登録
1. [Google AI Studio](https://aistudio.google.com/) で Gemini API キーを発行します。
2. リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で
   - Name: `GEMINI_API_KEY`
   - Secret: 発行したキー
   を登録してください。

### 3. 動作確認
- **Actions** タブ → `熊本地震支援情報 自動更新・デプロイ` → `Run workflow` で手動実行できます。
- 初回は既存の `public/data/support_info.json`（サンプルデータ）が Gemini の抽出結果とマージされ、
  実データに置き換わっていきます。
- 毎朝 7:00（JST）に自動実行されます（`.github/workflows/daily-update.yml` の cron 設定）。

## データ収集の仕組み（scripts/update_data.py）

1. `SOURCES` に定義した以下のソースから記事候補を収集します。
   - 宇城市 新着情報RSS（`https://www.city.uki.kumamoto.jp/rss/news/`）
   - 宇城市 地震関連情報ページ（小川町の情報を含む）
   - 熊本県 令和8年熊本地震に関する情報ページ
   - J-Net21 令和8年熊本地震に関する支援情報（経産省・中小機構系の事業者支援情報）
2. タイトル・抜粋にキーワード（`RELEVANT_KEYWORDS`）が含まれるものだけを候補として絞り込みます。
3. 候補を1件ずつ Gemini API に渡し、
   - 支援情報かどうかの判定
   - カテゴリ・対象地域・対象者・要約・期限の構造化
   を行います。
4. `source_url` をキーに既存データとマージし、`public/data/support_info.json` を更新します。

### ソースの追加・調整
`scripts/update_data.py` の `SOURCES` リストに `Source(...)` を追記すれば、他の自治体・省庁のRSSや
一覧ページも簡単に追加できます。HTML収集は `list_selector` の指定で対象サイトの構造に合わせて
CSSセレクタを調整してください（サイト構造が変わると取得できなくなる点に注意）。

## エラーの自動検知（Issue起票）

収集処理が以下のいずれかの状態になった場合、GitHub Actionsが自動でリポジトリに **Issue** を起票します。

- `GEMINI_API_KEY` が設定されていない
- 4つの情報源すべてへのアクセスに失敗した（サイト構造変更やネットワーク障害の可能性）

普段は何も通知されず、異常があったときだけ Issues タブに「⚠️ 支援情報の自動収集でエラーが発生しています」
というIssueが作成されます（同じ原因で毎日新規作成されるのではなく、既存のIssueが更新されます）。
対応してIssueをクローズすれば、次回エラーが起きたときに再度オープンされます。

なお「その日はたまたま新着の支援情報が0件だった」というだけでは異常とみなさず、Issueは作成されません。

Issueのテンプレートは `.github/workflow-templates/collection-failure-issue.md` にあり、
文面や通知先ラベルの変更もここで行えます。

## 注意事項
- 本サイトは非公式のまとめサイトです。**申請前には必ず一次情報（公式サイト）を確認する**旨を
  UI上に明記しています（`src/index.html` の「このサイトについて」セクション）。
- 各自治体・省庁サイトの利用規約・robots.txt に従い、過度な高頻度アクセスは避けてください
  （本実装は1日1回、各ソースへのリクエスト間に1秒のウェイトを入れています）。
- スクレイピング対象サイトの構造変更により、収集が失敗することがあります。Actions の実行ログで
  定期的に確認することをおすすめします。

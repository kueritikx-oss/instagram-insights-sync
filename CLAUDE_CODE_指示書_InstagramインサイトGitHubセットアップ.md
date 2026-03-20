# Claude Code 用 完全指示書 — Instagram インサイト GitHub Actions セットアップ

---

## 依頼するときにコピペする文（Claude Code への指示）

```
このファイル（タッキー/02_SNS集客/instagram-insights-scheduler/CLAUDE_CODE_指示書_InstagramインサイトGitHubセットアップ.md）の内容に従って、Instagram インサイトの GitHub Actions セットアップを完了してください。

【状況】
- GitHub リポジトリ: https://github.com/kueritikx-oss/instagram-insights-sync （Private、作成済み・空）
- Secrets 4つ: 登録済み（INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_IG_USER_ID, GOOGLE_CREDENTIALS_JSON, GOOGLE_TOKEN_JSON）
- 残り作業: ステップ1（push）→ ステップ2（手動実行で確認）だけ

指示書のステップ1から順に実行し、完了条件を満たしたら「セットアップ完了」と報告してください。
```

---

## 現在の状況（2026-03-13 時点）

| 項目 | 状態 |
|------|------|
| GitHub リポジトリ | **作成済み**: `kueritikx-oss/instagram-insights-sync` (Private) |
| Secrets 4つ | **登録済み**: INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_IG_USER_ID, GOOGLE_CREDENTIALS_JSON, GOOGLE_TOKEN_JSON |
| コードの push | **未実施** ← これが最優先 |
| 手動実行テスト | **未実施** |
| スケジュール確認 | **未実施** |

---

## ワークスペース情報

**ワークスペースルート**:
`/Users/taiki/Library/Mobile Documents/com~apple~CloudDocs/MacDocuments/01_事業/事業 Cursor`

**scheduler フォルダの絶対パス**:
`<ワークスペースルート>/タッキー/02_SNS集客/instagram-insights-scheduler`

**GitHub リポジトリ URL**:
`https://github.com/kueritikx-oss/instagram-insights-sync.git`

---

## ステップ 1: scheduler を GitHub に push する

### 1-1. scheduler フォルダに移動

```bash
cd "/Users/taiki/Library/Mobile Documents/com~apple~CloudDocs/MacDocuments/01_事業/事業 Cursor/タッキー/02_SNS集客/instagram-insights-scheduler"
```

### 1-2. リモートを設定して push

```bash
# origin が未設定の場合
git remote add origin https://github.com/kueritikx-oss/instagram-insights-sync.git

# origin が既にある場合（エラーが出たら）
# git remote remove origin
# git remote add origin https://github.com/kueritikx-oss/instagram-insights-sync.git

# push
git push -u origin main
```

### 1-3. push 成功の確認

以下のコマンドで確認するか、GitHub のリポジトリページを開いて確認する:

```bash
gh api repos/kueritikx-oss/instagram-insights-sync/contents/ --jq '.[].name' 2>/dev/null || echo "gh が使えない場合はブラウザで https://github.com/kueritikx-oss/instagram-insights-sync を確認"
```

**必須ファイルが push されていること**:
- `sync_instagram_insights.py`
- `requirements.txt`
- `.github/workflows/sync-instagram-insights.yml`
- `README.md`

### push で 403 / Permission denied が出た場合

1. `gh auth status` で認証状態を確認
2. 認証されていなければ `gh auth login` を実行
3. Personal Access Token が必要な場合: `https://github.com/settings/tokens` で `repo` スコープのトークンを作成し、push 時のパスワードとして使う
4. SSH を使う場合: `git remote set-url origin git@github.com:kueritikx-oss/instagram-insights-sync.git`

---

## ステップ 2: 手動で workflow を 1 回実行して確認する

### 方法 A: gh CLI で実行（推奨）

```bash
# workflow を手動実行
gh workflow run "Sync Instagram Insights" --repo kueritikx-oss/instagram-insights-sync

# 30秒ほど待ってから実行状態を確認
sleep 30
gh run list --repo kueritikx-oss/instagram-insights-sync --limit 1
```

実行が完了したら、ログを確認する:

```bash
# 最新の run ID を取得してログを見る
RUN_ID=$(gh run list --repo kueritikx-oss/instagram-insights-sync --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view $RUN_ID --repo kueritikx-oss/instagram-insights-sync --log
```

### 方法 B: ブラウザで実行

1. https://github.com/kueritikx-oss/instagram-insights-sync/actions を開く
2. 左の **Sync Instagram Insights** を選ぶ
3. 右側の **Run workflow** → **Run workflow** をクリック
4. 数分以内に実行が終わる。**緑のチェック**になれば成功

### 成功の確認ポイント

ログに次のような行が出ていれば正常:

```
API: メディア XXXX 件、パス照合用 XXXX 件
シート: URL が入っている行 XX 件
完了: 1日後 X 件、1週間後 Y 件を更新…
```

### 失敗した場合のトラブルシュート

| 現象 | 確認・対処 |
|------|------------|
| FileNotFoundError credentials | Secrets の `GOOGLE_CREDENTIALS_JSON` が JSON 全文で登録されているか確認。`gh secret list --repo kueritikx-oss/instagram-insights-sync` で 4 つあるか確認 |
| Google の refresh 失敗 | `GOOGLE_TOKEN_JSON` に `refresh_token` が含まれているか確認。Mac で `cat "<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/token.json"` して `refresh_token` キーがあるか見る |
| Instagram API エラー（190 など） | アクセストークン期限切れ。Phase 1 の手順で再取得し、`gh secret set INSTAGRAM_ACCESS_TOKEN --repo kueritikx-oss/instagram-insights-sync` で更新 |
| workflow が見つからない | push した内容に `.github/workflows/sync-instagram-insights.yml` が含まれているか確認 |

---

## ステップ 3: スケジュールの最終確認

- workflow の `sync-instagram-insights.yml` には `schedule: - cron: '0 21 * * *'` が設定されている
- これは **UTC 21:00 = 日本時間 翌日 6:00** の毎日実行
- 翌日、Actions タブで該当時刻頃に実行が 1 件記録されていれば、スケジュールは有効
- **重要**: GitHub Actions の scheduled は、リポジトリに 60 日以内にアクティビティがないと停止する。ステップ 2 で手動実行しておけば問題ない

---

## 完了条件（チェックリスト）

- [x] GitHub に private リポジトリが存在する（作成済み）
- [ ] scheduler の内容が push されている（sync_instagram_insights.py, requirements.txt, .github/workflows, README 等）
- [x] Secrets が 4 つ登録されている（登録済み）
- [ ] Actions で「Sync Instagram Insights」を手動実行し、ログに「完了: 1日後 X 件、1週間後 Y 件…」が出ている
- [ ] ユーザーに「セットアップ完了。毎日 6:00 JST に自動実行されます。トークンは約 60 日で期限切れなので、期限前に再取得して Secret を更新してください」と伝えた

**上記すべてにチェックが入ったら「セットアップ完了」と報告する。**

---

## なぜこれが必要か

Instagram インサイトは **投稿から正確に 24 時間後** と **7 日後** のデータが必要。Mac が閉じていても GitHub Actions がクラウドで毎日 6:00 JST に自動実行するので、データの取りこぼしがなくなる。

---

## 認証情報ファイルの場所（参考）

万が一 Secrets を再登録する必要がある場合:

| Secret 名 | ファイルパス | 値の取り方 |
|-----------|-------------|-----------|
| INSTAGRAM_ACCESS_TOKEN | `<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/instagram_insights_config.json` | `access_token` の値のみ |
| INSTAGRAM_IG_USER_ID | 同上 | `ig_user_id` の値のみ |
| GOOGLE_CREDENTIALS_JSON | `<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/credentials.json` | ファイル全文 |
| GOOGLE_TOKEN_JSON | `<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/token.json` | ファイル全文（refresh_token 必須） |

gh CLI で登録する場合:
```bash
gh secret set SECRET_NAME --repo kueritikx-oss/instagram-insights-sync < ファイルパス
# または
echo "値" | gh secret set SECRET_NAME --repo kueritikx-oss/instagram-insights-sync
```

---

## 参照ドキュメント

- 実装手順（Phase 1〜4）: `<ワークスペースルート>/タッキー/10_資料・リファレンス/Instagram_インサイト自動連携_実装手順.md`
- クラウド実行の設計: `<ワークスペースルート>/タッキー/10_資料・リファレンス/Instagram_インサイト_クラウド実行_設計.md`
- scheduler フォルダの README: `<scheduler フォルダ>/README.md`

---

*この指示書は Cowork で「リポジトリ作成 + Secrets 登録」まで完了させた状態で、Claude Code が「push → 手動実行 → 確認」を行うためのものです。*

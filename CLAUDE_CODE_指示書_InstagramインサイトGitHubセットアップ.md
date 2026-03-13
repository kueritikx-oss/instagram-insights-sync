# Claude Code 用 完全指示書 — Instagram インサイト GitHub Actions セットアップ

---

## 依頼するときにコピペする文（Claude Code への指示）

```
このファイル（CLAUDE_CODE_指示書_InstagramインサイトGitHubセットアップ.md）の内容に従って、Instagram インサイトの GitHub Actions セットアップを完了してください。ステップ 1 からステップ 5 まで上から順に実行し、完了条件のチェックリストをすべて満たしたら「セットアップ完了」と報告してください。リポジトリ作成はブラウザか gh で行い、Secrets は GitHub の Settings → Secrets and variables → Actions から登録します。認証情報の値は、指示書に書いたファイルパスから取得してください。
```

---

**目的**: この指示書の手順を**上から順にすべて実行**し、Instagram インサイト同期が「毎日 6:00 JST」に GitHub 上で自動実行される状態まで完了させる。

**ワークスペースルート**（この Mac での例）:  
`/Users/taiki/Library/Mobile Documents/com~apple~CloudDocs/MacDocuments/01_仕事/事業 Cursor`  
- 別の環境では「事業 Cursor」フォルダの絶対パスに読み替える。

**scheduler フォルダの絶対パス**:  
`<ワークスペースルート>/タッキー/02_SNS集客/instagram-insights-scheduler`

---

## 前提

- ユーザーの GitHub アカウントで **private リポジトリ** が作成できること。
- 認証情報は次の場所に既にあること（**絶対パス**）:
  - **Google credentials.json**: `<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/credentials.json`
  - **Google token.json**: `<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/token.json`
  - **Instagram config**: `<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/instagram_insights_config.json`  
    （中身は `access_token` と `ig_user_id` の JSON。Secrets ではこの 2 つを**別々に**登録する。）

---

## ステップ 1: GitHub に private リポジトリを作成する

### 方法 A: ブラウザで作成（推奨）

1. https://github.com/new を開く。
2. **Repository name**: `instagram-insights-sync` とする（任意の名前でも可。後で push の URL を合わせる）。
3. **Visibility**: **Private** を選ぶ。
4. **Add a README file** は**チェックしない**（中身はこちらの scheduler で push するため）。
5. **Create repository** をクリック。
6. 作成後、表示される **「…or push an existing repository from the command line」** の欄にある URL を控える。  
   例: `https://github.com/<ユーザー名>/instagram-insights-sync.git`

### 方法 B: GitHub CLI で作成（`gh` が入っている場合）

ターミナルで以下を実行する（`<ユーザー名>` は実際の GitHub ユーザー名に置き換える）:

```bash
gh repo create instagram-insights-sync --private --source="<ワークスペースルート>/タッキー/02_SNS集客/instagram-insights-scheduler" --remote=origin --push
```

- `--source` には **scheduler フォルダの絶対パス** を指定する。
- 成功したらステップ 2 は不要。ステップ 3 へ進む。

---

## ステップ 2: scheduler を GitHub に push する

**ステップ 1 の方法 A でリポジトリだけ作った場合**に実行する。

1. ターミナルで **scheduler フォルダ** に移動する:

   ```bash
   cd "<ワークスペースルート>/タッキー/02_SNS集客/instagram-insights-scheduler"
   ```

2. リモートを追加し、push する。  
   **`<GITHUB_REPO_URL>`** はステップ 1 で控えた URL（例: `https://github.com/<ユーザー名>/instagram-insights-sync.git`）に置き換える。

   ```bash
   git remote add origin <GITHUB_REPO_URL>
   git push -u origin main
   ```

   すでに `origin` が別の URL で設定されている場合は、先に削除してから追加する:

   ```bash
   git remote remove origin
   git remote add origin <GITHUB_REPO_URL>
   git push -u origin main
   ```

   **別の方法**: 同梱のスクリプトを使う場合:

   ```bash
   cd "<ワークスペースルート>/タッキー/02_SNS集客/instagram-insights-scheduler"
   export GITHUB_REPO_URL="<GITHUB_REPO_URL>"
   ./scripts/push-to-github.sh
   ```

3. push が成功したら、GitHub のリポジトリページで以下が存在することを確認する:
   - `sync_instagram_insights.py`
   - `requirements.txt`
   - `.github/workflows/sync-instagram-insights.yml`
   - `README.md`

---

## ステップ 3: GitHub Secrets を 4 つ登録する

リポジトリの **Settings → Secrets and variables → Actions** を開く。  
**New repository secret** で、以下の 4 つを**名前を一字一句合わせて**作成する。

### 3-1. INSTAGRAM_ACCESS_TOKEN

- **Name**: `INSTAGRAM_ACCESS_TOKEN`
- **Value**:  
  - ファイル `<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/instagram_insights_config.json` を開く。
  - 中の **`access_token`** の値（引用符で囲まれた長い文字列）を**そのまま**コピーして貼る。  
  - 引用符は含めない（値だけ）。

### 3-2. INSTAGRAM_IG_USER_ID

- **Name**: `INSTAGRAM_IG_USER_ID`
- **Value**:  
  - 上と同じ `instagram_insights_config.json` の **`ig_user_id`** の値（数字の文字列）をコピーして貼る。  
  - 引用符は含めない。

### 3-3. GOOGLE_CREDENTIALS_JSON

- **Name**: `GOOGLE_CREDENTIALS_JSON`
- **Value**:  
  - ファイル `<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/credentials.json` の**全文**をコピーする。
  - 改行を含めたまま 1 つの文字列として Secret の値に貼る（JSON として有効な形式のまま）。  
  - 先頭・末尾の空白や余分な改行は入れない。

### 3-4. GOOGLE_TOKEN_JSON

- **Name**: `GOOGLE_TOKEN_JSON`
- **Value**:  
  - ファイル `<ワークスペースルート>/タッキー/02_SNS集客/instagram-auto-post/token.json` の**全文**をコピーする。
  - **`refresh_token` が含まれていること**を確認する（含まれていないとクラウドで再認証できず失敗する）。
  - 全文を 1 つの文字列として Secret の値に貼る。

---

## ステップ 4: 手動で workflow を 1 回実行して確認する

1. リポジトリの **Actions** タブを開く。
2. 左の **Sync Instagram Insights** を選ぶ。
3. 右側の **Run workflow** → **Run workflow** をクリックする。
4. 数分以内に実行が終わる。**緑のチェック**になれば成功。
5. その実行をクリックし、**sync** ジョブを開いて **Run sync** のログを確認する。  
   - 次のような行が出ていれば正常:  
     `API: メディア XXXX 件、パス照合用 XXXX 件`  
     `シート: URL が入っている行 XX 件`  
     `完了: 1日後 X 件、1週間後 Y 件を更新…`
6. （可能なら）対象の Google スプレッドシート「🔴投稿毎データ①2026」を開き、該当行の 1日後・1週間後のセルに数値が入っているか確認する。

**失敗した場合**:
- **Google 認証エラー**: `GOOGLE_CREDENTIALS_JSON` と `GOOGLE_TOKEN_JSON` が全文コピーされているか、`token.json` に `refresh_token` が含まれているか再確認する。
- **Instagram API エラー**: `INSTAGRAM_ACCESS_TOKEN` が期限切れでないか確認する（約 60 日で失効）。必要なら [Instagram_インサイト自動連携_実装手順.md](../../10_資料・リファレンス/Instagram_インサイト自動連携_実装手順.md) の Phase 1 でトークンを再取得し、Secret を更新する。

---

## ステップ 5: スケジュールの確認

- workflow の **sync-instagram-insights.yml** には  
  `schedule: - cron: '0 21 * * *'` が設定されている。  
  これは **UTC 21:00 = 日本時間 翌日 6:00** の毎日実行である。
- 翌日、Actions の **Sync Instagram Insights** で、該当時刻頃に実行が 1 件記録されていれば、スケジュールは有効。

---

## トラブルシュート

| 現象 | 確認・対処 |
|------|------------|
| push で 403 / Permission denied | GitHub にログインしているか、そのアカウントにリポジトリの push 権限があるか確認。必要なら Personal Access Token を使う。 |
| Run sync で FileNotFoundError credentials | Secrets の `GOOGLE_CREDENTIALS_JSON` が正しく設定されているか確認。値は JSON 全文で、改行を含めてよい。 |
| Run sync で Google の refresh 失敗 | `token.json` に `refresh_token` が含まれているか確認。含まれていなければ、Mac で一度 OAuth をやり直して新しい token.json を取得し、`GOOGLE_TOKEN_JSON` を更新。 |
| Instagram API エラー（190 など） | アクセストークン期限切れの可能性。Phase 1 の手順で再取得し、`INSTAGRAM_ACCESS_TOKEN` を更新。 |
| スケジュールが動かない | GitHub Actions の scheduled はリポジトリに「ここ 60 日以内にアクティビティがある」とみなされないと止まることがある。手動で 1 回 Run workflow を実行しておく。 |

---

## 完了条件（チェックリスト）

- [ ] GitHub に private リポジトリが存在する
- [ ] scheduler の内容（sync_instagram_insights.py, requirements.txt, .github/workflows, README 等）がそのリポジトリに push されている
- [ ] Secrets が 4 つ（INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_IG_USER_ID, GOOGLE_CREDENTIALS_JSON, GOOGLE_TOKEN_JSON）登録されている
- [ ] Actions で「Sync Instagram Insights」を手動実行し、ログに「完了: 1日後 X 件、1週間後 Y 件…」が出ている
- [ ] （任意）スプレッドシートで該当行に数値が入っていることを確認した
- [ ] ユーザーに「セットアップ完了。毎日 6:00 JST に自動実行されます。トークンは約 60 日で期限切れなので、期限前に再取得して Secret を更新してください」と伝えた

---

## 参照ドキュメント

- 実装手順（Phase 1〜4）: `<ワークスペースルート>/タッキー/10_資料・リファレンス/Instagram_インサイト自動連携_実装手順.md`
- クラウド実行の設計: `<ワークスペースルート>/タッキー/10_資料・リファレンス/Instagram_インサイト_クラウド実行_設計.md`
- scheduler フォルダの README: `<scheduler フォルダ>/README.md`

---

*この指示書は Claude Code が「GitHub に接続してセットアップを完了する」ために必要な手順をすべて記載したものです。上から順に実行し、完了条件を満たした時点でセットアップ完了とします。*

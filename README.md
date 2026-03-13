# Instagram インサイト — クラウド毎日実行（GitHub Actions）

**目的**: Mac がなくても毎日 **6:00 JST** に `sync_instagram_insights.py` を実行し、24h後・7日後の数値を正確に取得する。

---

## セットアップ状況（2026-03-13）

| 項目 | 状態 |
|------|------|
| GitHub リポジトリ | **作成済み**: `kueritikx-oss/instagram-insights-sync` (Private) |
| Secrets 4つ | **登録済み** |
| push | **Claude Code で実行する** |
| 手動テスト | **push 後に実行する** |

### Claude Code に残りを任せる場合

**このファイルを開いて指示に従う**:
`CLAUDE_CODE_指示書_InstagramインサイトGitHubセットアップ.md`
（push → 手動実行 → 確認 だけ。リポジトリ作成と Secrets は完了済み。）

---

## 1. このフォルダを private リポジトリとして push する

1. GitHub で **private** リポジトリを新規作成（例: `instagram-insights-sync`）
2. このフォルダの中身をそのリポジトリに push する:

   ```bash
   cd "タッキー/02_SNS集客/instagram-insights-scheduler"
   git init
   git remote add origin https://github.com/<あなたのユーザー名>/instagram-insights-sync.git
   git add .
   git commit -m "Initial: Instagram insights daily sync"
   git push -u origin main
   ```

   （既にリポジトリがある場合は `git clone` してからこのフォルダの内容をコピーして push でも可。）

---

## 2. GitHub Secrets の登録

リポジトリの **Settings → Secrets and variables → Actions** で以下を追加する。

| Secret 名 | 中身 |
|-----------|------|
| `INSTAGRAM_ACCESS_TOKEN` | Meta の長期アクセストークン（60日で期限切れ、要再取得） |
| `INSTAGRAM_IG_USER_ID` | Instagram ビジネスアカウントの IG User ID（数字の文字列） |
| `GOOGLE_CREDENTIALS_JSON` | `credentials.json` の**全文**（1行にまとめた JSON 文字列で OK） |
| `GOOGLE_TOKEN_JSON` | `token.json` の**全文**（1行にまとめた JSON 文字列で OK） |

- **重要**: `token.json` は **refresh_token が入った有効なもの**を用意する。一度 Mac で OAuth を完了してできた `token.json` をそのままコピーして貼る。期限切れの場合は Mac で再ログインして新しい `token.json` を取得し、Secret を更新する。

---

## 3. 実行の流れ

- **自動**: 毎日 **6:00 JST**（UTC 21:00）に workflow が走る。
- **手動**: リポジトリの **Actions** タブ → **Sync Instagram Insights** → **Run workflow** で即時実行できる。

初回は手動で 1 回実行し、ログに「1日後 X 件、1週間後 Y 件を更新」と出るか、シートに数値が入るか確認すること。

---

## 4. ファイル構成（リポジトリに含めるもの）

- `sync_instagram_insights.py` … 本ワークスペースの `utils/sync_instagram_insights.py` のコピー
- `requirements.txt`
- `.github/workflows/sync-instagram-insights.yml`

README はこのファイル。認証情報はリポジトリに含めず、すべて Secrets で渡す。

---

## 5. トークン期限

- **Instagram**: 長期トークンは約 60 日で期限切れ。期限前に [実装手順書](../../10_資料・リファレンス/Instagram_インサイト自動連携_実装手順.md) の手順で再取得し、Secret `INSTAGRAM_ACCESS_TOKEN` を更新する。
- **Google**: `token.json` は refresh_token があれば自動更新される。再ログインが必要になったら Mac で 1 回 OAuth し直し、新しい `token.json` で Secret `GOOGLE_TOKEN_JSON` を更新する。

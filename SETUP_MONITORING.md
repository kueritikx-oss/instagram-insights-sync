# 監視セットアップ (5分で完了)

GitHub Actions の同期ワークフローが止まったら即スマホに通知が来る仕組みを入れる。

## なぜやるか

2026-04-04〜13、Instagram同期が10日間サイレントに止まっていて誰も気付けなかった。
これを二度と起こさないための**世界基準ソロプレナー監視**（healthchecks.io + Discord Webhook）。

## 何が起きるか

- ワークフロー失敗 → 数秒以内にDiscordに🔴通知(スマホPush)
- ワークフローが起動すらしなかった → healthchecks.io が検知して🔴通知
- Dead-man's switch パターン。沈黙=異常として扱う

## ステップ

### 1. Discord Webhook 取得 (3分)

1. [Discord](https://discord.com) で任意のサーバー → 任意チャンネル
2. チャンネル設定⚙️ → 連携サービス → ウェブフック → 新しいウェブフック
3. **ウェブフックURLをコピー**

### 2. Healthchecks.io でチェック作成 (2分)

1. [https://healthchecks.io](https://healthchecks.io) でサインアップ(無料)
2. + Add Check → 以下の設定:
   - **Name**: `Sync Instagram Insights`
   - **Schedule**: Cron → `0 0,6,12,15,18 * * *` (UTC)
   - **Grace Time**: 60 min
3. 作成後のページURL末尾の UUID をコピー
   - 例: `https://healthchecks.io/checks/abc123-def456-789.../details/`
   - `abc123-def456-789...` の部分
4. 右側 **Integrations** → Discord → 先ほどのWebhook URLを登録

### 3. Secret 登録 (1分)

ターミナルで:

```bash
cd ~/Projects/事業/タッキー/02_SNS集客/instagram-insights-scheduler
bash scripts/setup_monitoring.sh
```

対話で URL と UUID を貼り付けるだけ。自動でGitHub Secret登録+Discord通知テストまで実行。

## 完了後の動き

- 毎回の sync ワークフロー終了時に healthchecks.io に ping 送信
- 失敗時: Discord即時通知 + healthchecks.io側のDiscord統合も発火（二重防御）
- 起動失敗: healthchecks.io のGrace Period超過で自動通知

**これでもう10日間気付かず停止は起こらない。**

## 参考

- [healthchecks.io 公式ドキュメント](https://healthchecks.io/docs/)
- [Discord Webhook ドキュメント](https://support.discord.com/hc/ja/articles/228383668)

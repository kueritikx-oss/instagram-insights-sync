#!/bin/bash
# GitHub Actions 監視セットアップスクリプト
# Discord Webhook URL と healthchecks.io UUID を GitHub Secrets に登録する
#
# 事前準備:
#   1. Discord サーバー作成 → チャンネル設定 → 連携サービス → ウェブフック → URL取得
#   2. https://healthchecks.io でアカウント作成 → Add Check → UUID取得
#
# 使い方:
#   bash scripts/setup_monitoring.sh

set -e

REPO="kueritikx-oss/instagram-insights-sync"

echo "========================================"
echo "🔧 GitHub Actions 監視セットアップ"
echo "========================================"
echo
echo "このスクリプトは以下を実行:"
echo "  1. Discord Webhook URL を Secret 登録"
echo "  2. Healthchecks.io UUID を Secret 登録"
echo
echo "事前準備が必要:"
echo "  - Discord サーバーでウェブフック作成 (5分)"
echo "  - healthchecks.io でcheck作成 (5分)"
echo
read -p "続行する? (y/N): " confirm
if [[ "$confirm" != "y" ]]; then
  echo "中止。"
  exit 0
fi

echo
echo "----------------------------------------"
echo "Step 1/2: Discord Webhook URL"
echo "----------------------------------------"
echo
echo "取得方法:"
echo "  1. Discord サーバー(任意)の任意チャンネル"
echo "  2. チャンネル設定⚙️ → 連携サービス → ウェブフック"
echo "  3. 新しいウェブフック作成 → ウェブフックURLをコピー"
echo
read -p "Discord Webhook URL: " discord_url

if [[ -z "$discord_url" ]]; then
  echo "⚠️ URL空。Discord通知のセットアップをスキップ。"
else
  if [[ ! "$discord_url" =~ ^https://discord\.com/api/webhooks/ ]]; then
    echo "⚠️ URL形式が不正 (https://discord.com/api/webhooks/ で始まるはず)"
    read -p "このまま登録する? (y/N): " ok
    [[ "$ok" != "y" ]] && exit 1
  fi
  echo "$discord_url" | gh secret set DISCORD_WEBHOOK --repo "$REPO"
  echo "✅ DISCORD_WEBHOOK 登録完了"
fi

echo
echo "----------------------------------------"
echo "Step 2/2: Healthchecks.io UUID"
echo "----------------------------------------"
echo
echo "取得方法:"
echo "  1. https://healthchecks.io にログイン"
echo "  2. + Add Check で新規check作成"
echo "     推奨設定: Name='Sync Instagram Insights'"
echo "              Schedule='Cron' → '0 0,6,12,15,18 * * *' (UTC)"
echo "              Grace Time='60 min'"
echo "  3. 作成後のページ URL末尾がUUID"
echo "     例: https://healthchecks.io/checks/abc123-def456-.../details/"
echo "     この 'abc123-def456-...' 部分をコピー"
echo "  4. Integrations → Discord → 先ほどのWebhook URLを登録"
echo
read -p "Healthchecks UUID (sync-insights用): " hc_uuid

if [[ -z "$hc_uuid" ]]; then
  echo "⚠️ UUID空。healthchecks のセットアップをスキップ。"
else
  echo "$hc_uuid" | gh secret set HC_SYNC_INSIGHTS_UUID --repo "$REPO"
  echo "✅ HC_SYNC_INSIGHTS_UUID 登録完了"
fi

echo
echo "----------------------------------------"
echo "✅ セットアップ完了"
echo "----------------------------------------"
echo
echo "登録済み Secrets:"
gh secret list --repo "$REPO" | grep -E "DISCORD|HC_"
echo
echo "次のsync実行でping送信され、healthchecks.io側が監視を開始する。"
echo "手動テスト: gh workflow run sync-instagram-insights.yml --repo $REPO"
echo
echo "Discord通知テスト:"
if [[ -n "$discord_url" ]]; then
  echo "送信中..."
  curl -fsS -H "Content-Type: application/json" -d '{
    "username": "Setup Test",
    "embeds": [{
      "title": "✅ 監視セットアップ完了",
      "description": "このメッセージが届いたら Discord 通知は正常動作しています。",
      "color": 3066993
    }]
  }' "$discord_url" > /dev/null && echo "✅ Discord通知送信完了。Discordを確認してください。" || echo "⚠️ 送信失敗"
fi

#!/usr/bin/env bash
# GitHub に push するスクリプト。
# 使い方:
#   export GITHUB_REPO_URL="https://github.com/あなたのユーザー名/instagram-insights-sync.git"
#   ./scripts/push-to-github.sh
# または:
#   GITHUB_REPO_URL="https://github.com/あなたのユーザー名/instagram-insights-sync.git" ./scripts/push-to-github.sh

set -e
cd "$(dirname "$0")/.."

if [ -z "$GITHUB_REPO_URL" ]; then
  echo "エラー: GITHUB_REPO_URL を設定してください。"
  echo "例: export GITHUB_REPO_URL=\"https://github.com/あなたのユーザー名/instagram-insights-sync.git\""
  exit 1
fi

if git remote get-url origin 2>/dev/null; then
  git remote set-url origin "$GITHUB_REPO_URL"
else
  git remote add origin "$GITHUB_REPO_URL"
fi

git push -u origin main
echo "Push 完了: $GITHUB_REPO_URL"

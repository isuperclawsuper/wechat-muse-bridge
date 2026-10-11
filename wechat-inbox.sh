#!/usr/bin/env bash
# 微信 inbox 监视: ~/workspace/wechat-bridge/inbox/ 里出现新消息信封(.json)就叫醒 agent
# 注意: 只统计 *.json 信封, inbox/media/ 子目录(已解密媒体文件)不计入
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

INBOX="/home/hatch/workspace/wechat-bridge/inbox"  # 绝对路径, 不依赖 HOME
SIGFILE="$HOME/hooks/state/wechat-inbox.sig"
mkdir -p "$HOME/hooks/state" "$INBOX"

jsons=$(ls -1 "$INBOX" 2>/dev/null | grep '\.json$' || true)
sig=""
if [ -n "$jsons" ]; then
  sig=$(printf '%s\n' "$jsons" | sort | sha256sum | cut -d' ' -f1)
fi
prev=""
[ -f "$SIGFILE" ] && prev=$(cat "$SIGFILE")

if [ -n "$sig" ] && [ "$sig" != "$prev" ]; then
  count=$(printf '%s\n' "$jsons" | wc -l | tr -d ' ')
  if [ "$HATCH_HOOK_DRY_RUN" = "1" ]; then
    log "dry-run: would wake, $count json envelope(s) in inbox"
  else
    printf '%s' "$sig" > "$SIGFILE"
  fi
  wake "wechat-inbox" "{\"count\":$count}"
else
  silent "wechat inbox empty or unchanged"
fi

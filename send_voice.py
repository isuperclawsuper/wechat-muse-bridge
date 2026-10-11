#!/usr/bin/env python3
"""微信语音回复: 把文本合成 MP3, 走文件类型发给用户.

背景: 微信客户端会静默丢弃 bot 发的语音气泡(voice_item), 接口报 ok:true
但用户收不到(2026-10-11 实测 5 次 + wechat-cc 独立项目 2026-04-23 实测确认)。
解法: 语音一律发成可播放的音频文件, 用户点开即听。

用法:
  send_voice.py --to USER_ID --context-token TOKEN --text "要说的话"
  send_voice.py --to USER_ID --text-file /tmp/wechat_reply.txt   # token 自动取最新
  echo "你好" | send_voice.py --to USER_ID --text-stdin

用户偏好(2026-10-11): 微信每次回复默认"文字加语音", 本脚本即语音那一半。
"""
import glob
import json
import os
import subprocess
import sys
import tempfile

ROOT = "/home/hatch/workspace/wechat-bridge"
USER = "YOUR_WECHAT_USER_ID@im.wechat"  # 换成你自己的微信用户 ID(绑定后第一条消息的发送者)
SEND = os.path.join(ROOT, "send_reply.py")
TTS = "/opt/hatch/bin/tts"
# 语音读太长会生成大文件, 超过则截断(文字版不受影响)
MAX_VOICE_CHARS = 800


def latest_token(user):
    best = None
    for d in ("inbox", "processed"):
        for f in glob.glob(os.path.join(ROOT, d, "*.json")):
            try:
                m = json.load(open(f))
            except Exception:
                continue
            if m.get("user_id") != user or not m.get("context_token"):
                continue
            mt = os.path.getmtime(f)
            if best is None or mt > best[0]:
                best = (mt, m["context_token"])
    return best[1] if best else None


def main():
    args = sys.argv[1:]
    to = USER
    token = None
    text = None
    text_file = None
    use_stdin = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--to" and i + 1 < len(args):
            to = args[i + 1]; i += 2
        elif a == "--context-token" and i + 1 < len(args):
            token = args[i + 1]; i += 2
        elif a == "--text" and i + 1 < len(args):
            text = args[i + 1]; i += 2
        elif a == "--text-file" and i + 1 < len(args):
            text_file = args[i + 1]; i += 2
        elif a == "--text-stdin":
            use_stdin = True; i += 1
        else:
            i += 1

    if text_file:
        with open(text_file, encoding="utf-8") as f:
            text = f.read()
    elif use_stdin:
        text = sys.stdin.read()
    if not (text or "").strip():
        print("FAIL: no text given")
        sys.exit(1)
    text = text.strip()
    if len(text) > MAX_VOICE_CHARS:
        text = text[:MAX_VOICE_CHARS] + "……"

    if not token:
        token = latest_token(to)
    if not token:
        print("FAIL: no context_token found for user")
        sys.exit(1)

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    try:
        r = subprocess.run(
            [TTS, "--text", text, "--language", "zh",
             "--format", "mp3", "--output", tmp.name],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(tmp.name):
            print("FAIL: tts failed rc=%d err=%s" %
                  (r.returncode, (r.stderr or "").strip()[:200]))
            sys.exit(1)
        r = subprocess.run(
            ["/usr/bin/python3", SEND, "--to", to,
             "--context-token", token, "--media", tmp.name,
             "--media-type", "file"],
            capture_output=True, text=True, timeout=90)
        ok = r.returncode == 0 and "ok" in (r.stdout or "")
        print("rc=%d stdout=%s stderr=%s" %
              (r.returncode, (r.stdout or "").strip(),
               (r.stderr or "").strip()[:200]))
        sys.exit(0 if ok else 1)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


if __name__ == "__main__":
    main()

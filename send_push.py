#!/usr/bin/env python3
"""通用微信推送: 用该用户最新消息的 context_token 发一条纯文本.

供定时提醒等一次性推送调用. token 长期不用可能过期,
失败时打印原因并以非 0 退出, 由调用方决定如何处理.
"""
import glob
import json
import os
import subprocess
import sys

ROOT = "/home/hatch/workspace/wechat-bridge"
USER = "YOUR_WECHAT_USER_ID@im.wechat"  # 换成你自己的微信用户 ID(绑定后第一条消息的发送者)
SEND = os.path.join(ROOT, "send_reply.py")


def latest_token(user):
    best = None
    for d in ("inbox", "processed"):
        for f in glob.glob(os.path.join(ROOT, d, "*.json")):
            try:
                with open(f) as fh:
                    m = json.load(fh)
            except Exception:
                continue
            if m.get("user_id") != user or not m.get("context_token"):
                continue
            mt = os.path.getmtime(f)
            if best is None or mt > best[0]:
                best = (mt, m["context_token"])
    return best[1] if best else None


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("FAIL: no text given")
        sys.exit(1)
    text = sys.argv[1]
    token = latest_token(USER)
    if not token:
        print("FAIL: no context_token found for user")
        sys.exit(1)
    r = subprocess.run(
        ["/usr/bin/python3", SEND, "--to", USER,
         "--context-token", token, "--text", text],
        capture_output=True, text=True, timeout=60)
    print("rc=%d stdout=%s stderr=%s" %
          (r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()))
    sys.exit(0 if r.returncode == 0 and "ok" in (r.stdout or "") else 1)


if __name__ == "__main__":
    main()

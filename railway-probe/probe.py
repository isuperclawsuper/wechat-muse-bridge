#!/usr/bin/env python3
"""Railway 异地探测: 从 Railway 出口 IP 调一次 iLink getupdates, 打印原始返回.

环境变量:
  BOT_TOKEN  - iLink bot token (Railway dashboard 里设置, 不进代码仓库)
"""
import base64, json, os, random, struct, sys, time, urllib.request

BASE = "https://ilinkai.weixin.qq.com"

def one_try(token, cursor=""):
    uin = base64.b64encode(struct.pack(">I", random.getrandbits(32))).decode()
    body = json.dumps({"get_updates_buf": cursor}).encode()
    req = urllib.request.Request(
        BASE + "/ilink/bot/getupdates", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "AuthorizationType": "ilink_bot_token",
                 "Authorization": "Bearer " + token,
                 "X-WECHAT-UIN": uin,
                 "iLink-App-Id": "bot",
                 "iLink-App-ClientVersion": "131335"})
    t0 = time.time()
    try:
        raw = urllib.request.urlopen(req, timeout=40).read().decode()
        return "OK %.1fs raw=%s" % (time.time() - t0, raw[:200])
    except Exception as e:
        return "EXC %.1fs %s" % (time.time() - t0, e)

def main():
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        print("FATAL: BOT_TOKEN env not set", flush=True); sys.exit(1)
    print("probe start, token_tail=%s" % token[-6:], flush=True)
    for i in range(3):
        print("try %d: %s" % (i + 1, one_try(token)), flush=True)
        time.sleep(10)
    print("probe done", flush=True)

if __name__ == "__main__":
    main()

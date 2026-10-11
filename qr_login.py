#!/usr/bin/env python3
"""微信 iLink 扫码绑定助手.

用法:
  qr_login.py make   # 取二维码, 输出 qrcode id 与 liteapp 链接(JSON)
  qr_login.py wait --qrcode ID [--timeout 300]
                     # 长轮询等用户扫码确认, 成功后把 bot_token 写入
                     # account.json(0600), 并输出绑定信息(JSON)

由 agent 调用, 不要把输出里的 token 贴到聊天里.
"""
import argparse
import base64
import json
import os
import random
import struct
import sys
import time
import urllib.request
import urllib.parse

BASE_URL = "https://ilinkai.weixin.qq.com"
ROOT = "/home/hatch/workspace/wechat-bridge"  # 固定绝对路径: systemd 下 HOME=/root, 不能用 ~
ACCOUNT = os.path.join(ROOT, "account.json")
PENDING = os.path.join(ROOT, "qr_pending.json")


# 同一次登录流程内固定 UIN, 更像一个会话
_UIN = base64.b64encode(struct.pack(">I", random.getrandbits(32))).decode()


def make_headers(token=None):
    h = {"Content-Type": "application/json", "X-WECHAT-UIN": _UIN}
    if token:
        h["AuthorizationType"] = "ilink_bot_token"
        h["Authorization"] = "Bearer " + token
    return h


def req_json(method, path, body=None, token=None, timeout=40):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data,
                                 headers=make_headers(token), method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw[:500]}


def cmd_make():
    resp = req_json("POST", "/ilink/bot/get_bot_qrcode?bot_type=3", {},
                    timeout=20)
    if resp.get("ret") != 0:
        print(json.dumps({"ok": False, "resp": resp}, ensure_ascii=False))
        sys.exit(1)
    qrcode = resp.get("qrcode")
    link = resp.get("qrcode_img_content")
    with open(PENDING, "w", encoding="utf-8") as f:
        json.dump({"qrcode": qrcode, "link": link,
                   "created_at": time.time()}, f)
    print(json.dumps({"ok": True, "qrcode": qrcode, "link": link},
                     ensure_ascii=False))


def extract_token(resp):
    """从确认响应里抠 bot_token: 顶层优先, 其次 data 嵌套."""
    t = resp.get("bot_token")
    if t:
        return str(t)
    d = resp.get("data")
    if isinstance(d, dict):
        t = d.get("bot_token")
        if t:
            return str(t)
    return None


def cmd_wait(qrcode, timeout):
    deadline = time.time() + timeout
    saw_confirmed = False
    while time.time() < deadline:
        qs = urllib.parse.urlencode({"qrcode": qrcode})
        try:
            resp = req_json("GET", "/ilink/bot/get_qrcode_status?" + qs,
                            timeout=40)
        except Exception as e:
            print(json.dumps({"ok": False, "stage": "poll",
                              "error": str(e)}), flush=True)
            time.sleep(3)
            continue
        status = (resp.get("status") or resp.get("qrcode_status")
                  or resp.get("state") or "")
        token = extract_token(resp)
        if token:
            account = {}
            if os.path.exists(ACCOUNT):
                try:
                    with open(ACCOUNT, encoding="utf-8") as f:
                        account = json.load(f)
                except ValueError:
                    pass
            account.update({
                "bot_token": token,
                "ilink_bot_id": resp.get("ilink_bot_id"),
                "base_url": resp.get("baseurl") or BASE_URL,
                "bound_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            tmp = ACCOUNT + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(account, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, ACCOUNT)
            try:
                os.remove(PENDING)
            except OSError:
                pass
            # 绑定成功: 清除可能残留的过期标记(看门狗不再打扰用户)
            for flag in (os.path.join(ROOT, "token_expired.flag"),
                         os.path.join(ROOT, ".flag_reported")):
                try:
                    os.remove(flag)
                except OSError:
                    pass
            # 输出里隐去 token 全文
            print(json.dumps({"ok": True, "bound": True,
                              "token_tail": token[-6:],
                              "ilink_bot_id": resp.get("ilink_bot_id")},
                             ensure_ascii=False), flush=True)
            return
        if status == "confirmed":
            # 已确认但 token 还没下来: 加速轮询, 别错过窗口
            saw_confirmed = True
            print("confirmed but no token yet, fast re-poll",
                  file=sys.stderr, flush=True)
            time.sleep(0.5)
            continue
        if status in ("expired", "cancelled", "timeout"):
            print(json.dumps({"ok": False, "bound": False,
                              "reason": "qrcode %s" % status,
                              "saw_confirmed": saw_confirmed,
                              "resp_keys": list(resp.keys())},
                             ensure_ascii=False), flush=True)
            return
        # 还没确认: 继续轮询(把状态打到 stderr 避免污染 stdout JSON)
        print("poll status=%s keys=%s" % (status, list(resp.keys())),
              file=sys.stderr, flush=True)
        time.sleep(2)
    print(json.dumps({"ok": False, "bound": False,
                      "reason": "timeout waiting for scan",
                      "saw_confirmed": saw_confirmed}), flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("make")
    w = sub.add_parser("wait")
    w.add_argument("--qrcode", required=True)
    w.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    if args.cmd == "make":
        cmd_make()
    else:
        cmd_wait(args.qrcode, args.timeout)


if __name__ == "__main__":
    main()

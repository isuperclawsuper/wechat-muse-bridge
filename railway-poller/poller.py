#!/usr/bin/env python3
"""Railway 常驻轮询器: iLink getupdates 长轮询 -> 内存队列 -> HTTP 供云电脑拉取.

设计原则(优雅):
- 纯 ferry: 只做轮询 + 信封排队, 不做白名单/去重/媒体下载/转写,
  所有策略留在云电脑端, 两边职责单一.
- 信封格式与云电脑 forwarder 写的 inbox 信封完全一致,
  云电脑侧 hook/agent/reply 零改动.
- 游标本地文件持久化, 启动时可用 INIT_CURSOR 环境变量播种
  (迁移时从云电脑 state.json 拷过来, 不丢不重).
- errcode -14 走与 forwarder 相同的 1 小时冷却逻辑;
  连续 3 次冷却未恢复则向队列投递一个 _control 信封,
  云电脑拉取端据此立 token_expired.flag, 复用现有看门狗通知链.

环境变量:
  BOT_TOKEN   iLink bot token (必填)
  PULL_SECRET 拉取接口的 Bearer 密钥 (必填)
  INIT_CURSOR 初始游标 (可选, 迁移播种用)
  PORT        HTTP 端口 (Railway 自动注入)

接口:
  GET /pull    Authorization: Bearer <PULL_SECRET>
               -> {"envelopes": [...]} ; 投递成功后队列清空(at-least-once,
                  云电脑侧靠 seen_msg_ids 去重)
  GET /healthz -> {"ok": true, "queued": N, ...}
"""
import base64
import hashlib
import hmac
import json
import os
import random
import re
import struct
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_URL = "https://ilinkai.weixin.qq.com"
TOKEN = os.environ["BOT_TOKEN"]
PULL_SECRET = os.environ["PULL_SECRET"]
PORT = int(os.environ.get("PORT", "8080"))
INIT_CURSOR = os.environ.get("INIT_CURSOR", "")
HERE = os.path.dirname(os.path.abspath(__file__))
CURSOR_FILE = os.path.join(HERE, "cursor.json")

from media import parse_media_item  # noqa: E402  (stdlib only)


queue = []
queue_lock = threading.Lock()
status = {
    "last_poll_at": None,
    "last_ok_at": None,
    "last_error": None,
    "expired_strikes": 0,
    "cooling_until": None,
}


def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_cursor():
    try:
        with open(CURSOR_FILE, encoding="utf-8") as f:
            return json.load(f).get("get_updates_buf", "")
    except (OSError, ValueError):
        return INIT_CURSOR


def save_cursor(buf):
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"get_updates_buf": buf}, f)
    os.replace(tmp, tmp[:-4])


def make_headers(token):
    uin = base64.b64encode(struct.pack(">I", random.getrandbits(32))).decode()
    return {
        "Content-Type": "application/json",
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": "131335",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": "Bearer " + token,
        "X-WECHAT-UIN": uin,
    }


def api_post(path, body, timeout):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode("utf-8"),
        headers=make_headers(TOKEN),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _msg_id(m):
    mid = str(m.get("message_id") or m.get("msg_id") or m.get("id") or "").strip()
    if mid:
        return mid
    return hashlib.sha256(
        json.dumps(m, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def parse_msg(m):
    """与 forwarder.parse_msg(fetch_media=False) 等价的纯解析, 不下载媒体."""
    user_id = (
        m.get("from_user_id")
        or m.get("from_userid")
        or m.get("sender_id")
        or (m.get("from_user") or {}).get("id")
        or (m.get("sender") or {}).get("id")
        or m.get("user_id")
        or ""
    )
    context_token = m.get("context_token") or ""
    items = []
    texts = []
    for it in (m.get("item_list") or m.get("items") or []):
        if not isinstance(it, dict):
            continue
        p = parse_media_item(it)
        if p["type_name"] == "text":
            if p.get("text"):
                texts.append(p["text"])
        elif p.get("media"):
            p["media_pending"] = True  # 云电脑端补下载
        items.append(p)
    text = "\n".join(texts) if texts else (m.get("text") or m.get("content") or "")
    names = [p["type_name"] for p in items if p["type_name"] != "unknown"]
    if not names:
        msg_type = "text" if text else "unknown"
    elif all(n == "text" for n in names):
        msg_type = "text"
    elif len(set(names)) == 1:
        msg_type = names[0]
    else:
        msg_type = "mixed"
    return {
        "msg_id": _msg_id(m),
        "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": str(user_id),
        "msg_type": msg_type,
        "text": str(text),
        "items": items,
        "context_token": str(context_token),
        "raw": m,
    }


def enqueue(envelope):
    with queue_lock:
        # 队列上限 500, 防止云电脑长期失联把内存撑爆(最早的丢弃, 游标已过, 可接受)
        if len(queue) >= 500:
            queue.pop(0)
        queue.append(envelope)


def poll_loop():
    buf = load_cursor()
    log("poller 启动, 游标已载入(len=%d)" % len(buf))
    backoff = 5
    while True:
        # -14 冷却中: 睡到冷却结束
        if status["cooling_until"] and time.time() < status["cooling_until"]:
            time.sleep(30)
            continue
        status["cooling_until"] = None
        try:
            resp = api_post("/ilink/bot/getupdates", {"get_updates_buf": buf}, 45)
        except Exception as e:
            status["last_error"] = "network: %s" % e
            log("getupdates 网络异常: %s, %ss 后重试" % (e, backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue

        status["last_poll_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        errcode = resp.get("errcode") if isinstance(resp, dict) else None
        if errcode == -14:
            status["expired_strikes"] += 1
            n = status["expired_strikes"]
            if n > 3:
                enqueue({"_control": "token_expired",
                         "at": time.strftime("%Y-%m-%d %H:%M:%S")})
                log("token 过期(-14)连续3次冷却未恢复, 已投递 _control 信封")
                time.sleep(300)
                continue
            status["cooling_until"] = time.time() + 3600
            log("token 过期(-14), 进入1小时冷却 (第%d次)" % n)
            continue
        status["expired_strikes"] = 0

        msgs = resp.get("msgs") if isinstance(resp, dict) else None
        if not isinstance(msgs, list):
            keys = list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__
            raw = json.dumps(resp, ensure_ascii=False)[:200]
            status["last_error"] = "no-msgs: %s" % raw
            log("getupdates 异常(无 msgs): keys=%s raw=%s, %ss 后重试"
                % (keys, raw, backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue

        backoff = 5
        status["last_ok_at"] = status["last_poll_at"]
        status["last_error"] = None
        for m in msgs:
            if isinstance(m, dict):
                enqueue(parse_msg(m))
                log("入队 %s (队列=%d)" % (_msg_id(m), len(queue)))
        new_buf = resp.get("get_updates_buf", buf)
        if new_buf != buf:
            buf = new_buf
            save_cursor(buf)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:], PULL_SECRET)

    def do_GET(self):
        if self.path == "/healthz":
            with queue_lock:
                qlen = len(queue)
            self._json(200, {"ok": True, "queued": qlen, **status})
            return
        if self.path == "/pull":
            if not self._authorized():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            with queue_lock:
                envelopes = queue[:]
                queue.clear()
            self._json(200, {"ok": True, "envelopes": envelopes})
            log("/pull 投递 %d 个信封" % len(envelopes))
            return
        self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, *a):
        pass


def main():
    if not TOKEN or not PULL_SECRET:
        raise SystemExit("BOT_TOKEN / PULL_SECRET 必须设置")
    threading.Thread(target=poll_loop, daemon=True).start()
    log("HTTP 服务启动 :%d (/pull, /healthz)" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

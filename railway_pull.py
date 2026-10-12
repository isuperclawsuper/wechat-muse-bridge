#!/usr/bin/env python3
"""云电脑拉取端: 从 Railway 轮询器的 /pull 取信封, 落盘到 inbox/.

与旧 forwarder 的 inbox 信封格式完全一致, hook/agent/reply 零改动.
白名单、seen 去重、媒体补下载、token_expired.flag 全部保留在云电脑端.

配置: /home/hatch/workspace/wechat-bridge/pull_config.json (0600)
  {"pull_url": "https://<service>.up.railway.app/pull",
   "pull_secret": "<PULL_SECRET>"}
心跳: pull_heartbeat.json (看门狗检查新鲜度)
日志: logs/railway_pull.log
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

ROOT = "/home/hatch/workspace/wechat-bridge"  # 固定绝对路径
sys.path.insert(0, ROOT)
import forwarder as fw  # 复用 load_json/save_json/_atomic_write_json/_fill_pending_media/_safe_mid

CONFIG = os.path.join(ROOT, "pull_config.json")
STATE = os.path.join(ROOT, "state.json")
INBOX = os.path.join(ROOT, "inbox")
LOG = os.path.join(ROOT, "logs", "railway_pull.log")
HEARTBEAT = os.path.join(ROOT, "pull_heartbeat.json")
TOKEN_EXPIRED_FLAG = os.path.join(ROOT, "token_expired.flag")
INTERVAL = 30


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def heartbeat(ok, count=0, error=""):
    try:
        fw.save_json(HEARTBEAT, {
            "last_ok": time.strftime("%Y-%m-%d %H:%M:%S") if ok else None,
            "last_count": count,
            "last_error": error,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    except OSError:
        pass


def pull_once(pull_url, secret):
    req = urllib.request.Request(
        pull_url, headers={"Authorization": "Bearer " + secret}, method="GET")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def handle_control(env):
    if env.get("_control") == "token_expired":
        with open(TOKEN_EXPIRED_FLAG, "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        log("收到 _control token_expired, 已立 flag 等待重新扫码")
        return True
    return False


def handle_envelope(env, state):
    mid = str(env.get("msg_id") or "").strip()
    if not mid:
        log("跳过无 msg_id 的信封")
        return
    seen = state.get("seen_msg_ids") or []
    if mid in seen:
        return
    fname = "msg_%s.json" % fw._safe_mid(mid)
    fpath = os.path.join(INBOX, fname)

    uid = str(env.get("user_id") or "")
    allowed = state.get("allowed_user_ids", [])
    bootstrap = state.get("allowlist_bootstrap", True)
    if bootstrap and uid and uid not in allowed:
        allowed.append(uid)
        state["allowed_user_ids"] = allowed
        state["allowlist_bootstrap"] = False
        fw.save_json(STATE, state)
        log("白名单初始化: 已信任 %s" % uid)
    if uid not in allowed:
        log("忽略非白名单用户 %s 的消息" % (uid or "?"))
        seen.append(mid)
        state["seen_msg_ids"] = seen[-1000:]
        fw.save_json(STATE, state)
        return

    # 信封已是 forwarder 格式, 直接原子落盘; 再补媒体(与 forwarder 同序)
    fw._atomic_write_json(fpath, env)
    seen.append(mid)
    state["seen_msg_ids"] = seen[-1000:]
    fw.save_json(STATE, state)
    try:
        payload = fw.load_json(fpath, None)
        if isinstance(payload, dict) and fw._fill_pending_media(
                payload.get("items") or []):
            fw._atomic_write_json(fpath, payload)
    except Exception as e:
        log("媒体补下载失败 %s: %s" % (mid, e))
    log("收到 %s 的消息 -> inbox/%s (msg_type=%s, text_len=%d)"
        % (uid, fname, env.get("msg_type"), len(env.get("text") or "")))


def main():
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    log("railway_pull 启动")
    backoff = INTERVAL
    while True:
        cfg = fw.load_json(CONFIG, {})
        pull_url = cfg.get("pull_url") or ""
        secret = cfg.get("pull_secret") or ""
        if not pull_url or not secret:
            log("等待配置: pull_config.json 里还没有 pull_url/pull_secret, 60s 后重试")
            time.sleep(60)
            continue
        try:
            data = pull_once(pull_url, secret)
        except Exception as e:
            log("拉取失败: %s, %ss 后重试" % (e, backoff))
            heartbeat(False, error=str(e)[:200])
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        backoff = INTERVAL
        envelopes = data.get("envelopes") or []
        if envelopes:
            state = fw.load_json(STATE, {})
            for env in envelopes:
                if not isinstance(env, dict):
                    continue
                if handle_control(env):
                    continue
                try:
                    handle_envelope(env, state)
                except Exception as e:
                    log("处理信封异常: %s" % e)
        heartbeat(True, count=len(envelopes))
        if envelopes:
            log("本轮拉取 %d 个信封" % len(envelopes))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

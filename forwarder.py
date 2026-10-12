#!/usr/bin/env python3
"""微信 iLink 转发守护进程.

长轮询 ilink/bot/getupdates 收消息, 写入 inbox/ 目录;
由 hooks/wechat-inbox.sh 发现新文件后叫醒 agent 处理回复.
只认白名单里的用户, 其他人消息只记日志不处理.
"""
import base64
import hashlib
import json
import os
import random
import re
import struct
import sys
import time
import urllib.request
import urllib.error

BASE_URL = "https://ilinkai.weixin.qq.com"
ROOT = "/home/hatch/workspace/wechat-bridge"  # 固定绝对路径: systemd 下 HOME=/root, 不能用 ~
ACCOUNT = os.path.join(ROOT, "account.json")
STATE = os.path.join(ROOT, "state.json")
INBOX = os.path.join(ROOT, "inbox")
PROCESSED = os.path.join(ROOT, "processed")
LOG = os.path.join(ROOT, "logs", "forwarder.log")
TOKEN_EXPIRED_FLAG = os.path.join(ROOT, "token_expired.flag")
MEDIA_ROOT = os.path.join(ROOT, "inbox", "media")

from media import (  # noqa: E402
    parse_media_item,
    download_media,
    decrypt_media,
    save_media,
    transcribe_voice,
)


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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


def api_post(path, body, token, timeout):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode("utf-8"),
        headers=make_headers(token),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_media(p):
    """下载+解密+落盘(+语音转写), 结果写回 p. 失败记 error, 不抛异常."""
    media = p.get("media") or {}
    try:
        data = download_media(media)
        plain = decrypt_media(data, media.get("aes_key") or "")
        hint = p.get("file_name") if p["type_name"] == "file" else ""
        path = save_media(plain, p["type_name"],
                          os.path.join(MEDIA_ROOT, time.strftime("%Y%m%d")),
                          hint or "")
        p["file_path"] = path
        p["file_size"] = len(plain)
        if p["type_name"] == "voice":
            # 服务端下发的转写文本优先(实测可靠), 缺失时才走本地转写
            p["transcript"] = transcribe_voice(p.get("text") or "")
    except Exception as e:
        p["error"] = "media fetch failed: %s" % e
        log("媒体下载失败(type=%s): %s" % (p["type_name"], e))


def _fill_pending_media(items):
    """补齐标了 media_pending 的媒体下载, 原地更新 items. 返回是否有变化."""
    changed = False
    for p in items:
        if p.get("media_pending") and not p.get("file_path") and not p.get("error"):
            _fetch_media(p)
            p.pop("media_pending", None)
            changed = True
    return changed


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _msg_id(m):
    mid = str(m.get("message_id") or m.get("msg_id") or m.get("id") or "").strip()
    if mid:
        return mid
    return hashlib.sha256(
        json.dumps(m, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _safe_mid(mid):
    return re.sub(r"[^0-9a-zA-Z_-]", "_", mid)[:48]


def parse_msg(m, fetch_media=True):
    """防御式解析入站消息, 未知字段原样保留在 raw 里.

    items: 每个 item 的 {type, type_name, ...}; 媒体 item 会下载解密落盘,
    成功则带 file_path/file_size, 语音另带 transcript, 失败则带 error.
    fetch_media=False 时只做解析, 媒体 item 标 media_pending, 由调用方
    在信封落盘后再调 _fill_pending_media 补齐(崩溃不丢消息).
    """
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
            if fetch_media:
                _fetch_media(p)
            else:
                p["media_pending"] = True
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
        "user_id": str(user_id),
        "context_token": str(context_token),
        "msg_type": msg_type,
        "text": str(text),
        "items": items,
        "raw": m,
    }


def main():
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(PROCESSED, exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    log("forwarder 启动")

    backoff = 5
    expired_strikes = 0  # errcode -14 连续命中次数, 成功一次清零
    while True:
        account = load_json(ACCOUNT, {})
        token = account.get("bot_token") or ""
        if not token:
            log("等待绑定: account.json 里还没有 bot_token, 60s 后重试")
            time.sleep(60)
            continue

        state = load_json(STATE, {})
        buf = state.get("get_updates_buf", "")
        allowed = state.get("allowed_user_ids", [])
        bootstrap = state.get("allowlist_bootstrap", True)

        try:
            resp = api_post(
                "/ilink/bot/getupdates", {"get_updates_buf": buf}, token, timeout=45
            )
        except Exception as e:  # 网络错误等: 退避重试
            log("getupdates 网络异常: %s, %ss 后重试" % (e, backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue

        # errcode -14 = 会话过期: 先自动 1 小时冷却再恢复(借鉴 weixin-agent-sdk),
        # 连续 3 次冷却仍过期才立 flag 转人工扫码
        # 注: backoff 不在这里重置, 只有健康轮询(含 msgs 数组)才归零
        errcode = resp.get("errcode") if isinstance(resp, dict) else None
        if errcode == -14:
            expired_strikes += 1
            if expired_strikes > 3:
                open(TOKEN_EXPIRED_FLAG, "w").write(
                    time.strftime("%Y-%m-%d %H:%M:%S"))
                log("token 过期(errcode -14)连续3次冷却未恢复, 已立 flag, 等待重新绑定")
                time.sleep(60)
                continue
            log("token 过期(errcode -14), 进入1小时冷却后自动恢复 (第%d次)" % expired_strikes)
            for _ in range(60):
                time.sleep(60)
            continue
        expired_strikes = 0

        msgs = resp.get("msgs") if isinstance(resp, dict) else None
        if not isinstance(msgs, list):
            # 服务端显式错误(如 {"ret": -3})同样走指数退避, 避免显式错误把退避
            # 清零导致高频轰炸服务端(借鉴 weixin-agent-sdk 连续失败 30s 退避的思路)
            keys = list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__
            raw = json.dumps(resp, ensure_ascii=False)[:300]
            log("getupdates 异常(无 msgs): keys=%s raw=%s, %ss 后重试" % (keys, raw, backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue

        # 健康轮询(含 msgs 数组): 退避归零
        backoff = 5

        # token 有效: 若之前立过过期 flag, 现在清除
        if os.path.exists(TOKEN_EXPIRED_FLAG):
            try:
                os.remove(TOKEN_EXPIRED_FLAG)
                log("token 恢复有效, 清除过期标记")
            except OSError:
                pass

        msgs = resp.get("msgs") or []
        seen = state.get("seen_msg_ids") or []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            mid = _msg_id(m)
            fname = "msg_%s.json" % _safe_mid(mid)
            fpath = os.path.join(INBOX, fname)
            if mid in seen:
                # 重拉: 只补齐上次没下完的媒体, 不重建信封、不重复回复
                try:
                    payload = load_json(fpath, None)
                    if isinstance(payload, dict) and _fill_pending_media(
                            payload.get("items") or []):
                        _atomic_write_json(fpath, payload)
                        log("重拉补齐媒体 %s" % fname)
                except Exception as e:
                    log("重拉补齐失败 %s: %s" % (mid, e))
                continue
            p = parse_msg(m, fetch_media=False)
            uid = p["user_id"]

            if bootstrap and uid and uid not in allowed:
                # 绑定后第一条消息: 自动把发送者记为白名单(唯一信任的人)
                allowed.append(uid)
                state["allowed_user_ids"] = allowed
                state["allowlist_bootstrap"] = False
                save_json(STATE, state)
                log("白名单初始化: 已信任 %s (绑定后第一条消息的发送者)" % uid)

            if uid not in allowed:
                log("忽略非白名单用户 %s 的消息" % (uid or "?"))
                seen.append(mid)
                state["seen_msg_ids"] = seen[-1000:]
                save_json(STATE, state)
                continue

            payload = {
                "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": uid,
                "msg_type": p["msg_type"],
                "text": p["text"],
                "items": p["items"],
                "context_token": p["context_token"],
                "raw": p["raw"],
            }
            # 先落信封(原子写)再下载媒体: 任何时刻崩溃都不会丢消息;
            # seen 落盘后才算接住, 重拉靠 seen 去重
            _atomic_write_json(fpath, payload)
            seen.append(mid)
            state["seen_msg_ids"] = seen[-1000:]
            save_json(STATE, state)
            if _fill_pending_media(payload["items"]):
                _atomic_write_json(fpath, payload)
            detail = ",".join(
                "%s%s" % (it["type_name"],
                          ":ok" if it.get("file_path") else
                          (":err" if it.get("error") else ""))
                for it in p["items"]) or "empty"
            log("收到 %s 的消息 -> inbox/%s (msg_type=%s, items=[%s], text_len=%d)"
                % (uid, fname, p["msg_type"], detail, len(p["text"])))

        # 整批信封落盘后再推进游标: 崩溃重拉靠 seen 去重, 不丢不重
        new_buf = resp.get("get_updates_buf", buf)
        if new_buf != buf:
            state["get_updates_buf"] = new_buf
            save_json(STATE, state)


if __name__ == "__main__":
    main()

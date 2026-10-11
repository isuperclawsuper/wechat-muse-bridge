#!/usr/bin/env python3
"""发微信回复.

用法:
  send_reply.py --to USER_ID --context-token TOKEN --text TEXT
  send_reply.py --to USER_ID --context-token TOKEN --media PATH --media-type {image,video,file,voice}
  send_reply.py --to USER_ID --context-token TOKEN --image PATH   # = --media PATH --media-type image(兼容旧调用)

只发纯文本(微信不渲染 Markdown, 调用方负责组装纯文本).
bot_token 从 account.json(0600) 读取, 不经过命令行/日志.
媒体发送: getuploadurl -> PUT 加密媒体 -> sendmessage(流程对照 wx-bot 核验).
"""
import argparse
import base64
import json
import os
import random
import struct
import sys
import urllib.request

from media import upload_media, build_media_item

BASE_URL = "https://ilinkai.weixin.qq.com"
ROOT = "/home/hatch/workspace/wechat-bridge"  # 固定绝对路径: systemd 下 HOME=/root, 不能用 ~
ACCOUNT = os.path.join(ROOT, "account.json")
# 对齐官方插件/wx-bot 的客户端指纹, 缺了会被腾讯静默丢消息
ILINK_APP_ID = "bot"
ILINK_CLIENT_VERSION = "131335"
CHANNEL_VERSION = "2.1.7"


def make_headers(token):
    uin = base64.b64encode(struct.pack(">I", random.getrandbits(32))).decode()
    return {
        "Content-Type": "application/json",
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_CLIENT_VERSION,
        "AuthorizationType": "ilink_bot_token",
        "Authorization": "Bearer " + token,
        "X-WECHAT-UIN": uin,
    }


def api_post(path, body, token, timeout):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=make_headers(token),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _voice_kwargs(media_type, media_path):
    """语音发送: 从 SILK 文件解码算出时长(ms), 缺了 playtime 会被腾讯静默丢弃."""
    if media_type != "voice":
        return {}
    try:
        import io
        import pysilk
        with open(media_path, "rb") as f:
            silk_data = f.read()
        out = io.BytesIO()
        pysilk.decode(io.BytesIO(silk_data), out, 16000)
        ms = int(len(out.getvalue()) / (16000 * 2) * 1000)
        return {"voice_playtime_ms": ms, "voice_sample_rate": 16000}
    except Exception:
        return {"voice_playtime_ms": 0, "voice_sample_rate": 16000}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="目标用户 ID")
    ap.add_argument("--context-token", required=True, help="入站消息带的 context_token")
    ap.add_argument("--text", default=None, help="纯文本内容")
    ap.add_argument("--media", default=None, help="媒体文件路径")
    ap.add_argument("--media-type", default=None,
                    choices=["image", "video", "file", "voice"],
                    help="媒体类型")
    ap.add_argument("--image", default=None,
                    help="兼容旧调用: 等价于 --media PATH --media-type image")
    ap.add_argument("--voice-text", default="",
                    help="语音转写文本: 放进 voice_item 的 text 字段")
    args = ap.parse_args()
    if args.image and not args.media:
        args.media, args.media_type = args.image, "image"
    if args.text is None and args.media is None:
        ap.error("需要 --text 或 --media")
    if args.media and not args.media_type:
        ap.error("--media 需要配合 --media-type")

    try:
        with open(ACCOUNT, encoding="utf-8") as f:
            account = json.load(f)
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": "read account.json failed: %s" % e}),
              flush=True)
        sys.exit(1)
    token = account.get("bot_token") or ""
    if not token:
        print(json.dumps({"ok": False, "error": "no bot_token (not bound yet)"}),
              flush=True)
        sys.exit(1)
    bot_id = account.get("ilink_bot_id") or ""

    client_id = "muse-" + "".join(random.choice("0123456789abcdef")
                                  for _ in range(16))
    if args.media:
        try:
            with open(args.media, "rb") as f:
                data = f.read()
        except OSError as e:
            print(json.dumps({"ok": False, "error": "read media failed: %s" % e}),
                  flush=True)
            sys.exit(1)
        try:
            media, cipher_size = upload_media(
                data, args.media_type, args.to,
                lambda path, body, timeout: api_post(path, body, token, timeout))
        except Exception as e:
            print(json.dumps({"ok": False, "error": "upload media failed: %s" % e},
                             ensure_ascii=False), flush=True)
            sys.exit(1)
        item = build_media_item(
            args.media_type, media,
            file_name=os.path.basename(args.media),
            cipher_size=cipher_size, plain_size=len(data),
            voice_text=args.voice_text if args.media_type == "voice" else "",
            **_voice_kwargs(args.media_type, args.media))
    else:
        item = {"type": 1, "text_item": {"text": args.text}}
    body = {
        "msg": {
            "from_user_id": bot_id,
            "to_user_id": args.to,
            "client_id": client_id,
            "message_type": 2,
            "message_state": 2,
            "context_token": args.context_token,
            "item_list": [item],
        },
        "base_info": {"channel_version": CHANNEL_VERSION},
    }
    try:
        resp = api_post("/ilink/bot/sendmessage", body, token, timeout=20)
    except Exception as e:
        print(json.dumps({"ok": False, "error": "sendmessage failed: %s" % e}),
              flush=True)
        sys.exit(1)

    ok = resp.get("ret") == 0 or "message_id" in resp
    # 全量记录响应(不含 token), 便于排查"接口报成功但微信没收到"
    try:
        with open(os.path.join(ROOT, "logs", "send.log"), "a",
                  encoding="utf-8") as f:
            f.write("%s to=%s ok=%s resp=%s\n" % (
                __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                args.to, ok,
                json.dumps(resp, ensure_ascii=False)[:500]))
    except OSError:
        pass
    # 不打印 token, 只打印结果摘要
    print(json.dumps({"ok": ok, "ret": resp.get("ret"),
                      "errcode": resp.get("errcode"),
                      "errmsg": resp.get("errmsg"),
                      "message_id": resp.get("message_id")},
                     ensure_ascii=False),
          flush=True)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()

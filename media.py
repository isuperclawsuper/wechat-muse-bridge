#!/usr/bin/env python3
"""iLink 媒体收发共用逻辑.

收: item_list[].type 2/3/4/5 的 media 经 CDN 下载(AES-128-ECB 解密)落盘.
发: getuploadurl -> PUT 加密媒体 -> sendmessage 带对应 item.

协议细节对照 wx-bot 开源实现核验, 接收链路另用真实语音消息全程实测通过.
"""
import base64
import hashlib
import json
import os
import random
import time
import urllib.request
import urllib.error

from crypto_util import (
    aes128_ecb_encrypt,
    aes128_ecb_decrypt,
    unpad_pkcs7,
)

# item_list[].type
ITEM_TYPES = {1: "text", 2: "image", 3: "voice", 4: "file", 5: "video"}
# getuploadurl 的 media_type(注意和 ItemType 数字不一样)
UPLOAD_MEDIA_TYPES = {"image": 1, "video": 2, "file": 3, "voice": 4}
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
MAX_MEDIA_BYTES = 100 * 1024 * 1024
CHANNEL_VERSION = "2.1.7"


def parse_aes_key(s):
    """容忍多种 aes_key 下发格式, 返回 16 字节 key.
    实测: base64(32 位 hex 字符串). 兼容: 32 位 hex 明文 / 16 字节原文."""
    if not s:
        raise ValueError("empty aes_key")
    s = s.strip()
    if len(s) == 32 and all(c in "0123456789abcdefABCDEF" for c in s):
        return bytes.fromhex(s)
    if len(s) == 16:
        return s.encode("latin1")
    raw = base64.b64decode(s)
    if len(raw) == 16:
        return raw
    txt = raw.decode("ascii").strip()
    if len(txt) == 32 and all(c in "0123456789abcdefABCDEF" for c in txt):
        return bytes.fromhex(txt)
    raise ValueError("unrecognized aes_key format (len=%d)" % len(s))


def download_media(media, timeout=120):
    """从 CDN 下载加密媒体, 返回原始加密字节. media: {full_url?, encrypt_query_param?}."""
    url = (media.get("full_url") or "").strip()
    if not url:
        param = (media.get("encrypt_query_param") or "").strip()
        if not param:
            raise ValueError("media has neither full_url nor encrypt_query_param")
        from urllib.parse import quote
        url = "%s/download?encrypted_query_param=%s" % (CDN_BASE, quote(param))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise IOError("cdn download status %s" % r.status)
        data = r.read(MAX_MEDIA_BYTES + 1)
    if len(data) > MAX_MEDIA_BYTES:
        raise IOError("media too large (>100 MB)")
    if len(data) % 16 != 0:
        raise IOError("cdn data length not multiple of 16 (%d)" % len(data))
    return data


def decrypt_media(data, aes_key_b64):
    """解密并去 PKCS#7 填充, 返回明文字节."""
    key = parse_aes_key(aes_key_b64)
    return unpad_pkcs7(aes128_ecb_decrypt(key, data))


def sniff_extension(data, type_name, hint_name=""):
    """按魔数猜扩展名, 猜不出按类型给默认."""
    if hint_name and "." in hint_name:
        ext = hint_name.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5 and ext.isalnum():
            return "." + ext
    sig = data[:12]
    if sig[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if sig[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if sig[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if sig[:4] == b"RIFF" and sig[8:12] == b"WEBP":
        return ".webp"
    if sig[:4] == b"\x1a\x45\xdf\xa3":
        return ".mkv"
    if sig[4:8] == b"ftyp":
        return ".mp4"
    return {  # 默认
        "image": ".jpg",
        "voice": ".silk",
        "video": ".mp4",
        "file": ".bin",
    }.get(type_name, ".bin")


def save_media(data, type_name, media_dir, hint_name=""):
    """落盘, 返回绝对路径."""
    os.makedirs(media_dir, exist_ok=True)
    ext = sniff_extension(data, type_name, hint_name)
    fname = "%s_%s_%08x%s" % (
        time.strftime("%H%M%S"), type_name, random.getrandbits(32), ext)
    path = os.path.join(media_dir, fname)
    with open(path, "wb") as f:
        f.write(data)
    return path


def parse_media_item(item):
    """解析单个 item, 返回 {type, type_name, media?} 等. 非媒体返回 type_name 即可."""
    t = item.get("type")
    name = ITEM_TYPES.get(t, "unknown")
    out = {"type": t, "type_name": name}
    if name in ("image", "voice", "file", "video"):
        sub = item.get(name + "_item") or {}
        media = sub.get("media") or {}
        out["media"] = {
            "encrypt_query_param": media.get("encrypt_query_param") or "",
            "aes_key": media.get("aes_key") or "",
            "full_url": media.get("full_url") or "",
        }
        # 各类型特有字段(防御式提取, 有就带上)
        for k in ("text", "playtime", "file_name", "len", "mid_size", "video_size",
                  "encode_type", "sample_rate", "bits_per_sample"):
            if sub.get(k) not in (None, ""):
                out[k] = sub[k]
    elif name == "text":
        ti = item.get("text_item") or {}
        if ti.get("text"):
            out["text"] = ti["text"]
    return out


# ---------- 发送侧 ----------

def _rand_hex(nbytes):
    return "".join(random.choice("0123456789abcdef") for _ in range(nbytes * 2))


def upload_media(data, media_type_name, to_user_id, post_fn):
    """上传媒体到 CDN, 返回可直接放进 sendmessage item 的 media dict.

    post_fn(path, body_dict, timeout) -> resp dict, 由调用方提供(带鉴权头).
    流程: 随机 AES key 加密 -> getuploadurl -> PUT 密文 -> 取 x-encrypted-param.
    """
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("file too large (>100 MB)")
    if media_type_name not in UPLOAD_MEDIA_TYPES:
        raise ValueError("unknown media type: %s" % media_type_name)

    aes_key = bytes(random.getrandbits(8) for _ in range(16))
    filekey = _rand_hex(16)
    encrypted = aes128_ecb_encrypt(aes_key, data)  # 内部已 PKCS#7 补齐
    resp = post_fn("/ilink/bot/getuploadurl", {
        "filekey": filekey,
        "media_type": UPLOAD_MEDIA_TYPES[media_type_name],
        "to_user_id": to_user_id,
        "rawsize": len(data),
        "rawfilemd5": hashlib.md5(data).hexdigest(),
        "filesize": len(encrypted),
        "no_need_thumb": True,
        "aeskey": aes_key.hex(),
        "base_info": {"channel_version": CHANNEL_VERSION},
    }, timeout=30)
    if not isinstance(resp, dict):
        raise IOError("getuploadurl bad response: %r" % type(resp))
    if resp.get("errcode"):
        raise IOError("getuploadurl errcode=%s errmsg=%s" % (
            resp.get("errcode"), resp.get("errmsg")))
    upload_url = (resp.get("upload_full_url") or "").strip()
    if not upload_url:
        param = (resp.get("upload_param") or "").strip()
        if not param:
            raise IOError("getuploadurl: no upload_full_url/upload_param in %s"
                          % json.dumps(resp, ensure_ascii=False)[:300])
        from urllib.parse import quote
        upload_url = "%s/upload?encrypted_query_param=%s&filekey=%s" % (
            CDN_BASE, quote(param), quote(filekey))

    last_err = None
    for _ in range(3):
        try:
            req = urllib.request.Request(
                upload_url, data=encrypted,
                headers={"Content-Type": "application/octet-stream"},
                method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                if 400 <= r.status < 500:
                    raise IOError("cdn upload client error %s" % r.status)
                if r.status != 200:
                    last_err = IOError("cdn upload status %s" % r.status)
                    continue
                dl_param = r.headers.get("x-encrypted-param") or ""
                if not dl_param:
                    last_err = IOError("cdn response missing x-encrypted-param")
                    continue
                return {
                    "encrypt_query_param": dl_param,
                    "aes_key": base64.b64encode(aes_key.hex().encode()).decode(),
                    "encrypt_type": 1,
                }, len(encrypted)
        except Exception as e:
            last_err = e
    raise IOError("cdn upload failed: %s" % last_err)


def build_media_item(media_type_name, media, file_name="", cipher_size=0,
                     plain_size=0, voice_playtime_ms=0, voice_sample_rate=16000,
                     voice_text=""):
    """按类型组装 sendmessage 的 item."""
    t = {"image": 2, "voice": 3, "file": 4, "video": 5}[media_type_name]
    if media_type_name == "image":
        return {"type": t, "image_item": {"media": media, "mid_size": cipher_size}}
    if media_type_name == "video":
        return {"type": t, "video_item": {"media": media, "video_size": cipher_size}}
    if media_type_name == "file":
        return {"type": t, "file_item": {
            "media": media,
            "file_name": file_name or "file.bin",
            "len": str(plain_size),
        }}
    # voice: 语音气泡(voice_item)会被微信客户端静默丢弃 —— 接口返回 ok:true
    # 但用户收不到。独立项目 wechat-cc 已于 2026-04-23 用 4 组配置实测确认
    # (voice_item/encode_type=7 路径被客户端丢弃, openclaw-weixin 有 VoiceItem
    # 类型但无 sendVoice, Tencent 未开放该能力)。我方 2026-10-11 的 5 次实测
    # (含原样回发官方 SILK 文件)同样全部静默丢失, 结论一致。
    # 解法: 语音走 file 类型发可播放音频(WAV/MP3), 用户点开即听。
    # 此分支保留仅供将来 Tencent 开放后启用, 当前发送语音请用 media_type="file".
    return {"type": t, "voice_item": {
        "media": media,
        "playtime": int(voice_playtime_ms),
        "sample_rate": int(voice_sample_rate),
    }}


# ---------- 语音转写 ----------

def transcribe_voice(server_text="", wav_path=None):
    """语音转文字.

    1. 优先用微信服务端下发的转写文本(实测两条语音都有, 可靠).
    2. 服务端没给时, 尝试本地 faster-whisper(需自行 pip 安装, 未安装则跳过).
    3. 都拿不到返回 "".
    """
    if server_text and server_text.strip():
        return server_text.strip()
    if wav_path and os.path.exists(wav_path):
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel("small", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(wav_path, language="zh")
            return "".join(s.text for s in segments).strip()
        except Exception:
            pass
    return ""


def decode_silk_to_wav(silk_path, wav_path):
    """SILK -> WAV(16kHz mono). 尽力而为: 按顺序尝试可用解码器.

    返回 True/False. 目前为 stub: 常见 silk 解码器(pysilk 等)需编译安装,
    本机未预装; 如将来装好了解码器, 在此按优先级接入即可.
    """
    # 预留接入点: 若系统装有 silk 解码器可在此调用.
    # 例: subprocess.run(["silk-decoder", silk_path, wav_path])
    return False

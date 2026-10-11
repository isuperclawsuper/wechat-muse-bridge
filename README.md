# 微信 iLink 桥接

把微信(ClawBot 插件, 经腾讯 iLink 接口)接到 Muse。

## 链路

微信 → `ilink/bot/getupdates` 长轮询 → `forwarder.py` → `inbox/` →
hook `wechat-inbox` (15s) 叫醒 agent → 回复经 `send_reply.py` 发回微信 →
主聊天同步一份。

## 文件

- `forwarder.py` — 守护进程, systemd 常驻, 只认白名单用户;
  解析全部 5 种消息类型(text/image/voice/file/video), 媒体经 CDN 下载
  AES-128-ECB 解密后落盘到 `inbox/media/日期/`, inbox payload 里 `items`
  数组暴露真实类型、本地路径, 语音另带服务端转写 `transcript`
- `send_reply.py` — 发回复: `--to USER_ID --context-token TOKEN --text TEXT` (纯文本);
  发媒体: `--media PATH --media-type {image,video,file,voice}`
  (流程: getuploadurl → PUT 加密文件 → sendmessage; 语音发送格式未经实测)
- `qr_login.py` — 扫码绑定: `make` 取二维码, `wait --qrcode ID` 等确认
- `media.py` — 媒体收发共用逻辑(下载/解密/落盘/上传/转写接口)
- `crypto_util.py` — AES-128-ECB 加解密; 优先 pycryptodome, 没有则用内置
  纯 Python 实现(零依赖, 已用 NIST 向量 + 真实语音消息验证)
- `account.json` (0600) — bot_token, 绑定后写入
- `state.json` — get_updates_buf 游标、白名单、context_token 缓存
- `inbox/` / `processed/` / `failed/` — 消息流转目录
- `inbox/media/` — 下载解密后的媒体文件(按日期分子目录)
- `wechat-bridge.service` — systemd 单元(正本, /etc 下的是副本)

## 媒体说明(2026-10-11 扩展)

- 接收: item 的 `media` 字段含 `full_url`(或 `encrypt_query_param`) 和 `aes_key`
  (base64 的 hex 字符串); 下载→AES-128-ECB 解密→去 PKCS#7 填充→按魔数定扩展名落盘。
  已用真实语音消息全程实测(解密出合法 `#!SILK_V3` 文件)。
- 语音转写: 优先用微信服务端下发的 `voice_item.text`(实测两条都有, 可靠);
  缺失时 `transcribe_voice()` 尝试本地 faster-whisper(未安装则返回空)。
  SILK 解码(`decode_silk_to_wav`)为 stub, 需解码器时再接。
- 发送: `getuploadurl` 参数(media_type: image=1/video=2/file=3/voice=4,
  rawsize/rawfilemd5/filesize/aeskey hex/no_need_thumb)→PUT 密文到返回的
  `upload_full_url`(取响应头 `x-encrypted-param`)→sendmessage 带对应 item。
  实测: 真实文件上传 200 成功; 注: CDN 对 <1KB 的小文件 PUT 报 500
  (x-error-code -5102031), 真实媒体文件一般不受影响。

## 常用命令

- 状态: `systemctl status wechat-bridge`
- 重启: `systemctl restart wechat-bridge`
- 日志: `tail -f logs/forwarder.log`
- 手动重装(看门狗每 5 分钟会自动做, 一般不用手敲):
  `cp wechat-bridge.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now wechat-bridge`

## 故障

- `token_expired.flag` 出现 = errcode -14, 凭证过期 → 重新扫码绑定
  (`qr_login.py make` 取码 → 用户扫 → `wait` 确认)
- 会话约 24h 过期, 属腾讯侧限制, 到期重扫即可

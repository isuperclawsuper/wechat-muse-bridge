# 微信 iLink 桥接

把微信(ClawBot 插件, 经腾讯 iLink 接口)接到 Muse。

## 链路（2026-10-12 起：Railway 轮询方案）

微信 → Railway `railway-poller` (`getupdates` 长轮询) → 内存队列 →
云电脑 `railway_pull.py` (每 30s 拉取) → `inbox/` →
hook `wechat-inbox` (15s) 叫醒 agent → 回复经 `send_reply.py` 发回微信 →
主聊天同步一份。

迁移原因见 `railway-迁移说明.md`：云电脑出口 IP 被腾讯 iLink 限流
（`getupdates` 返回无文档的 `{"ret": -3}`），Railway 出口正常。
旧 `forwarder.py`（云电脑直连轮询）已停用保留。

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

## 语音发送(2026-10-11 实测结论)

微信客户端会**静默丢弃** bot 发的语音气泡(`voice_item`): 接口返回
`ok:true` + `message_id`, 但用户收不到。5 次实测(含原样回发官方
SILK 文件)全部丢失; 独立项目 wechat-cc 亦于 2026-04-23 经 4 组配置
实测确认(腾讯未开放 bot 语音气泡能力)。

解法: 语音一律发成**可播放的音频文件**。`send_voice.py` 把文本用 TTS
合成中文 MP3, 走 `--media-type file` 发送, 用户点开即听:

- `send_voice.py --to USER_ID --context-token TOKEN --text "要说的话"`
- `send_voice.py --to USER_ID --text-file /tmp/wechat_reply.txt`
  (token 为空时自动取该用户最新消息的 context_token)

不要再尝试 `--media-type voice`, 当前只会静默丢失。

## 默认文字加语音

`wechat-inbox` 钩子指令已改为每次回复默认"文字加语音": 文字经
`send_reply.py` 发出后, 再调 `send_voice.py` 把同一文本读出来发成 MP3
文件。语音失败不影响文字回复的判定。

## 每小时 500px 照片推送(2026-10-12 新增)

- `photo-push/photo-hourly-push-500px.md` — 定时任务体全文: 每天 8:00–23:00
  整点从 500px 挑一张图推送到微信, 国际站和国内站轮换选图,
  每轮发"一句话介绍 + 国内站链接 + 国际站链接"(双链接方案),
  用户打分(喜欢/还行/不喜欢)写回 `photo_taste.json` 指导下一轮选图。
- `photo-push/photo_taste.example.json` — 审美偏好文件结构示例
- 依赖桥本体的 `send_reply.py` 发微信。当前为纯文本形式:
  2026-10-13 实测两站匿名访问天花板 1024px、CDN 签名改尺寸参数即 403,
  在找到 500KB+ 新链路前只发介绍+链接, 不下载不发小图。
  任务体内占位符 `<BRIDGE_DIR>` / `<WECHAT_USER_ID>` / `<CHAT_ID>` 换成自己的。

## 每日推送(2026-10-11 新增)

- `daily_push.py` — 每天 12:00 / 18:00(上海时间)各推一条"高性价比人生指南"
  (eternity4719/HowToLiveBetter), 从 `lifeguide/items.json`(677 条)里按
  收益大/不花钱/少时间/不吃毅力筛选轮换, 进度记在 `lifeguide/push_state.json`
- `wechat-push-noon.service` / `wechat-push-noon.timer`、
  `wechat-push-evening.service` / `wechat-push-evening.timer` —
  systemd 定时单元(正本, /etc 下的是副本, 看门狗会自动恢复)
- `send_push.py` — 通用推送脚本: 用该用户最新消息的 context_token
  发一条纯文本(供定时提醒等一次性推送调用)

注意: 三个脚本里的 `USER` 常量是占位符, 换成你自己的微信用户 ID
(绑定后第一条消息的发送者)。

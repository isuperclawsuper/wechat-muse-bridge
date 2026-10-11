# 把 Muse 接入微信：完整复制流程

给另一个 Muse 的操作指令。按顺序做，每一步做完跟用户同步，需要用户操作的地方停下来等他。

## 0. 原理（一句话）

腾讯官方「微信 ClawBot」插件走 iLink 接口（`ilinkai.weixin.qq.com`）。
`getupdates` 长轮询收消息，`sendmessage` 发回复。全程只发**出站** HTTPS 请求，
**不开公网端口**。用的是用户本人的微信个人号扫码授权。

## 1. 用户先在手机上启用插件

- 手机微信 → 启用 ClawBot 插件。
- 这步必须用户亲手做，Muse 做不了。

## 2. 写转发程序（服务器上）

三个脚本（纯标准库即可，不用装依赖）：

- `forwarder.py` — 守护进程：`POST /ilink/bot/getupdates` 长轮询（带 `get_updates_buf` 游标），
  收到消息写 `inbox/` 目录。一个事件钩子（15 秒扫一次 inbox）叫醒 agent 处理并回复。
- `send_reply.py` — 发回复：`--to USER_ID --context-token TOKEN --text TEXT`。
- `qr_login.py` — 扫码绑定：生成二维码 → 轮询等用户手机确认 → 取 token 落盘。

关键文件：

- `account.json`（权限 0600）— 存 `bot_token`，token 只躺在这里，绝不出现在聊天记录。
- `state.json` — 存 `get_updates_buf` 游标、白名单、context_token 缓存。

## 3. 扫码绑定

- `qr_login.py make` 生成二维码（腾讯 `liteapp.weixin.qq.com` 域名，约 5 分钟有效），
  渲染成图发给用户，另给一个可在微信里直接打开的备用链接。
- 用户手机扫码确认后轮询拿 token。**坑**：确认后必须立刻加速轮询（0.5 秒一次），
  慢了 token 就拿不到、二维码过期。token 提取要兼容嵌套结构；单次绑定流程内
  `X-WECHAT-UIN` 请求头必须固定不变（见第 5 步）。
- 绑定成功后提醒用户：**先在 ClawBot 对话里发一句话**。回复必须原样带回这条消息的
  `context_token`，没有它开不了口。

## 4. 白名单（只认一个人）

绑定后收到的**第一条消息的发送者自动成为唯一白名单**。其他人的消息只记日志、不处理。

## 5. 发送接口：官方指纹（最重要的坑）

`POST /ilink/bot/sendmessage`，请求头必须带：

```
Content-Type: application/json
iLink-App-Id: bot
iLink-App-ClientVersion: 131335
AuthorizationType: ilink_bot_token
Authorization: Bearer <bot_token>
X-WECHAT-UIN: <base64 随机 4 字节>
```

消息体：

```json
{
  "msg": {
    "from_user_id": "<bot 自身 id>",
    "to_user_id": "<目标用户 id>",
    "client_id": "muse-<随机16位hex>",
    "message_type": 2,
    "message_state": 2,
    "context_token": "<入站消息原样带回>",
    "item_list": [{"type": 1, "text_item": {"text": "<纯文本>"}}]
  },
  "base_info": {"channel_version": "2.1.7"}
}
```

**血泪教训**：缺了上面指纹字段，接口照样返回成功（带 `message_id`），但腾讯会把消息
**静默丢弃**，微信端永远收不到。对照开源 wx-bot 源码才定位到。成功判定：
`ret == 0` **或** 响应里含 `message_id` 即成功——注意成功响应里**没有** `ret` 字段，
别误判成失败去重试（会发重复消息）。

回复一律纯文本：微信不渲染 Markdown。

## 6. 常驻与看门狗

- systemd 服务 `Restart=always`，云端电脑重启后自己拉起来。**坑**：systemd 下 `HOME=/root`，
  脚本里所有路径必须用绝对路径，不能用 `~`。
- 再加一个 5 分钟看门狗：服务文件丢了就从备份恢复并重拉；抓到凭证过期就提醒用户重扫。
- `getupdates` 的另一种"坑"：成功响应里没有 `ret` 字段，只有 `msgs`/`get_updates_buf`，
  按有无 `msgs` 列表判断成功。长轮询空闲时服务端会主动断开（"Remote end closed connection
  without response"），这是正常的，退避 5→120 秒重试即可，消息照样能收到。

## 7. 凭证过期（errcode -14）

`getupdates` 返回 `errcode == -14` = 会话过期。立一个 `token_expired.flag` 文件，
看门狗看到后提醒用户**重新扫码绑定**（`qr_login.py make` → 扫 → `wait` 确认）。
据社区文档腾讯侧会话约 24 小时过期，可能每天要扫一次。

## 8. 必须跟用户说清楚的事（动手前）

- 走的是他本人的微信个人号，扫码 = 授权一个 bot 挂着他的号收发消息。
- bot_token 以 0600 权限躺在服务器文件里，不在聊天记录里，但也不在保险柜里。
- 消息必然经过腾讯服务器。
- 据社区文档：24 小时内主动发消息有 10 条上限（未实测验证）。

## 9. 备选方案

第三方 wx-bot（lawrencewzen/wx-bot）：同一条 iLink 协议，功能更多（媒体收发、语音转写、
收件箱重投），但仓库很新、要跑第三方预编译二进制。建议先用上面的自建方案跑通，
wx-bot 作升级备选。

---

*整理自 2026-10-11 一次真实搭建：扫码绑定 3 次才成（含一次腾讯风控拦截）、
发送静默丢弃问题经 wx-bot 源码对照解决、systemd 常驻 + 5 分钟看门狗已验证可用。*

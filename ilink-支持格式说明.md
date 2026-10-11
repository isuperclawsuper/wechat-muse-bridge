# 微信 iLink 桥：当前支持的消息格式

> 自建实现，已实测。给别人的 Muse 照着接即可。

## 接收（getupdates 长轮询 + 游标）

| type | 类型 | 处理方式 |
|------|------|----------|
| 1 | 文本 | 直接取 text_item.text |
| 2 | 图片 | CDN 下载，AES-128-ECB 解密落盘 |
| 3 | 语音 | SILK 编码，CDN 下载解密；转写优先用微信服务端下发的文本 |
| 4 | 文件 | CDN 下载解密，保留原文件名 |
| 5 | 视频 | CDN 下载解密 |

- 单文件上限 100MB；下载失败自动标记 error，可让用户重发。
- 可靠性：先写消息信封（inbox JSON）再下载媒体，游标最后推进；崩溃后重拉靠消息 ID 去重，不丢不重。

## 发送（sendmessage）

- 文本：直接发送。
- 图片 / 语音 / 文件 / 视频：`getuploadurl` 取上传地址 → AES-128-ECB 加密 → PUT 上传 → `sendmessage` 携带对应 type 的 item。
- 必填字段（缺了会被腾讯静默丢弃，接口照常返回 message_id）：
  - 请求头：`iLink-App-Id: bot`、`iLink-App-ClientVersion: 131335`
  - body：`base_info.channel_version = 2.1.7`
  - msg：`from_user_id`、`client_id`、`message_type`、`message_state`

## 运维要点

- `errcode -14`：token 过期，需要用户重新扫码绑定。
- 白名单：绑定后第一条消息的发送者自动成为唯一白名单，非白名单消息只记日志不回复。
- 看门狗：每 5 分钟检查服务状态，云电脑重启后自动恢复服务文件并拉起，无需人工干预。

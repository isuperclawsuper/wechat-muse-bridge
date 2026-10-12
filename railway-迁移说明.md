# Railway 迁移说明（2026-10-12）

## 背景

2026-10-12 10:36 起，云电脑出口 IP 被腾讯 iLink 限流：`getupdates`
先被静默掐 TCP 连接，后返回无文档的 `{"ret": -3}`。换 token、重新扫码
均无效；同一 token 在 Railway（美国西海岸出口）上三次 `getupdates`
全部正常。结论：账号/会话/token 没问题，是**出口 IP 被限**。

## 新架构

```
微信 → Railway poller (getupdates 长轮询) → 内存队列
       → 云电脑 railway_pull.py (每 30s 拉取) → inbox/
       → hook wechat-inbox (15s) → agent 回复 → send_reply.py 发回
```

设计原则：

- **Railway 侧纯 ferry**：只做轮询 + 信封排队，不做白名单/去重/
  媒体下载/转写。信封格式与旧 `forwarder.py` 写的完全一致，
  hook/agent/reply 零改动。
- **所有策略留云电脑**：白名单、`seen_msg_ids` 去重、媒体 CDN 下载
  解密、语音转写、`token_expired.flag`，全部在 `railway_pull.py`。
- **-14 会话过期**：poller 沿用 forwarder 的 1 小时冷却逻辑；
  连续 3 次冷却未恢复则投递 `_control: token_expired` 信封，
  云电脑端据此立 flag，复用现有看门狗通知链。
- **游标**：poller 本地 `cursor.json` 持久化；冷启动可用
  `INIT_CURSOR` 环境变量播种。云电脑侧 `seen_msg_ids` 兜底去重。

## Railway 侧部署

仓库：`passingyears/wechat-muse-bridge`（fork），服务 `wechat-muse-bridge`

- Root Directory：留空（仓库根目录）
- Dockerfile Path：`railway-poller/Dockerfile`
  （构建上下文须为仓库根目录，`COPY media.py crypto_util.py` 才找得到）
- 环境变量：
  - `BOT_TOKEN` — iLink bot token（扫码绑定所得）
  - `PULL_SECRET` — 拉取接口 Bearer 密钥（自行生成，如 `openssl rand -base64 32`）
  - `INIT_CURSOR` — 可选，迁移时播种游标
- 开启 Public Networking，记下公网域名（如
  `https://wechat-muse-bridge-production.up.railway.app`）
- 接口：`GET /pull`（需 `Authorization: Bearer <PULL_SECRET>`，
  返回 `{"envelopes": [...]}` 并清空队列）；
  `GET /healthz`（健康检查）

文件：`railway-poller/poller.py`、`railway-poller/Dockerfile`
（`media.py`、`crypto_util.py` 均为标准库零依赖，构建时 COPY 进镜像）

## 云电脑侧

- `railway_pull.py` — 拉取守护进程（systemd 常驻）
- `pull_config.json` (0600，不进仓库)：
  `{"pull_url": "https://<域名>/pull", "pull_secret": "<PULL_SECRET>"}`
- `railway-pull.service` — systemd 单元（正本，/etc 下的是副本）
- `pull_heartbeat.json` — 拉取心跳，看门狗检查 `last_ok` 新鲜度
- 旧 `wechat-bridge.service` 已停用（出口 IP 被限），保留 unit 文件作历史，
  **不要再启动它**（双轮询会抢消息）

## 回滚

Railway 异常时：停 `railway-pull`，起 `wechat-bridge`
（云电脑直连轮询；若出口 IP 仍被限则无效）。

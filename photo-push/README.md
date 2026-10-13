# 500px 每小时照片推送(双链接方案)

把 500px 的摄影作品每小时整点推送到微信, 用户会回"喜欢/还行/不喜欢"打分,
打分写回 `photo_taste.json`, 越推越对审美。

## 当前方案(2026-10-13 起)

- 时间: 每天 8:00–23:00 整点推送, 0–7 点不打扰。若某轮被平台排队拖到 0 点后
  才开始执行, 直接跳过不补发。
- 选图: 国际站 500px.com 和国内站 500px.com.cn 轮换, 按 `photo_taste.json`
  里的审美偏好挑 `sent` 里没发过的一张。
- 配链: 每轮两个链接都发——国内站链接(用户能直接打开) + 国际站链接
  (用户挂 VPN 看)。国内站用新版 `https://500px.com.cn/photo/<短码>` 形式;
  国际站用 `https://500px.com/photo/<短码>` 形式。短码两站不互通:
  国际站选了图, 先在 500px.com.cn 站内搜到对应照片再取其国内站短码,
  搜不到就改从中国站榜单直接选。
- 形式: 目前是"一句话介绍 + 两个链接"纯文本(微信里不渲染 Markdown,
  纯文本即可)。

## 为什么暂不发图(2026-10-13 11:30 实测结论, 两站皆然)

- 匿名访问的天花板就是 1024px(约 60–110KB), CDN 签名覆盖全部 URL 参数,
  改尺寸参数直接 403, og:image 仅 600px。
- 旧的 img.500px.me + `!p5` 拿 2048px 的路子已随中国站改版失效。
- 在找到 500KB+ 新链路前, 每轮只发介绍+链接, 不下载不发小图。
- 恢复直发的两个可能前提: ① 登录 500px 账号(登录态可能解锁 2048px,
  待验证); ② 改用 Unsplash/Pexels 等免费高清图源。

## 取图元数据(无需开浏览器, GraphQL 直调)

curl -sS -A "<移动端 UA>" -H "x-500px-platform: Web" \
  -H "x-500px-device-id: <任意 UUID>" \
  "https://api-neo.500px.com.cn/graphql?operationName=getPhotoById&query=<URL编码>&variables=<URL编码的 {\"id\":\"<短码>\"}>"

其中 query 为:
query getPhotoById($id: ID!) {
  getPhotoById(id: $id) {
    id title uploader { displayName } urls { size_1024 } width height
  }
}

国际站把域名换成 api-neo.500px.com 即可。签名 URL 约 2 小时过期,
必须本轮内现取现用。

## 文件

- `photo-hourly-push-500px.md` — 定时任务体全文(含前端面的 frontmatter,
  interval 1h / timezone Asia/Shanghai); 占位符 `<BRIDGE_DIR>` /
  `<WECHAT_USER_ID>` / `<CHAT_ID>` 换成自己的)。
  完整执行步骤: 读审美文件 → 学反馈更新 taste → 轮换站点选图 →
  配双链接 → (直发链路打通前)纯文本介绍+链接 →
  用该用户最新消息的 context_token 经 `send_reply.py` 发微信 →
  把本轮记入 sent。
- `photo_taste.example.json` — `photo_taste.json` 的结构示例:
  taste(审美偏好描述) / liked_tags / disliked_tags / sent(已发记录, 防重复)。
- 定时任务依赖桥本体的脚本: `send_reply.py`(发微信),
  `daily_push.py` 里的 `latest_token` 逻辑(取该用户最新消息的 context_token)。

注意: 任务体内要求"不要改动 account.json、pull_config.json,
不要删除 inbox/ 里的文件"。

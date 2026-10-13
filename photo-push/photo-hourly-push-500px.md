---
id: photo-hourly-push-500px
title: 500px 每小时照片推送
enabled: true
owner: goal:500px
mode: task
schedule:
  kind: interval
  timezone: Asia/Shanghai
  at: 2026-10-12T19:00:00
  every: 1h
timeout_secs: 1800
delivery:
  - chat_id: <CHAT_ID>
metadata:
  originating_chat_context_json: '{"chat_id":"<CHAT_ID>","origin_provider":"main","chat_kind":"direct","event_kind":"message","require_mention":false}'
  presentation_locale: zh-CN
---
500px 每小时照片推送（用户在微信里定的需求）：

时间规则：以本轮实际开始执行的时刻为准（不要用计划时刻），看当前上海时间（Asia/Shanghai）的整点小时数；0–7 点直接跳过，什么都不做、不打扰用户、不报告。只有 8:00–23:00 才执行下面步骤。（用户 2026-10-12 22:45 明确：定时任务只保留发图，0 点到 7 点不发；这条规则就是该要求的实现。注意：若某轮被平台排队拖到 0 点后才开始执行，直接跳过，不要补发。）

执行步骤：
1. 读 <BRIDGE_DIR>/photo_taste.json：含 taste（审美偏好描述）、liked_tags / disliked_tags、sent（已发过的照片 page_url 列表，避免重复）。
2. 学反馈：扫描 <BRIDGE_DIR>/inbox/ 和 processed/ 里 user_id 为 <WECHAT_USER_ID> 的消息，找"喜欢/还行/不喜欢"这类对照片的打分（结合上下文判断评价的是哪张），更新 photo_taste.json 的 taste / liked_tags / disliked_tags。
3. 选图（用户 22:45 要求两个站轮换试喜好）：看 sent 里上一张用的是哪个站，本轮优先换另一个站——国际站 500px.com（Editors Choice / Popular / Upcoming 榜单）或中国站 500px.com.cn（榜单/热门）。按当前 taste 挑一张 sent 里没有的照片，拿到标题、摄影师、照片页 URL。题材轮换；避开用户明确不喜欢的：大场景风光（太阳/日落大场景兴趣一般）、猫及宠物肖像（直接 pass）、动态模糊/晃眼构图（看着头晕，直接 pass）。用户点名要自然风光花卉时优先这类。
4. 配链规则（用户 2026-10-13 12:25 明确：国内站 500px.com.cn 的链接他能直接打开，国际站 500px.com 的链接他自己挂 VPN 看）：发给用户的照片页链接每轮两个都发——国内站链接 + 国际站链接。找图仍按两个站轮换不变。国内站链接用新版 https://500px.com.cn/photo/<短码> 形式；国际站链接用 https://500px.com/photo/<短码> 形式。（2026-10-13 11:30 实测：短码两站不互通——国际站短码拿到国内站 API 查询直接返回 null；旧 community/photo-details/<32位hex> 链接已确认跳转至 /p/community 宣传页、打不开。若本轮在国际站选了图，先用标题/摄影师在 500px.com.cn 站内搜到对应照片再取其国内站短码拼国内站链接；站内搜不到则本轮改从中国站榜单直接选图。）
5. 下原图（2026-10-13 11:30 实测结论，两站皆然：匿名访问最大只给 1024px 签名图、约 60–110KB；GraphQL 的 urls.size_2048/size_4k 字段返回的与 size_1024 是同一张 1024px 图——500KB 硬标准当前结构性不可达。以下死路不要再试：改 CDN 签名 URL 参数（sig 覆盖全部查询参数，改即 403 / Invalid signature）、旧 img.500px.me+!p5 路子（已随中国站改版失效）、og:image（仅 600px）、requests 直抓照片页（只回 JS 空壳）。
   取图元数据（标题/摄影师/1024px 签名 URL）无需开浏览器，直接调 GraphQL GET：
   curl -sS -A "<移动端 UA>" -H "x-500px-platform: Web" -H "x-500px-device-id: <任意 UUID，如 a1b2c3d4-e5f6-7890-abcd-ef1234567890>" \
     "https://api-neo.500px.com.cn/graphql?operationName=getPhotoById&query=<URL编码>&variables=<URL编码的 {"id":"<短码>"}>"
   其中 query 为：query getPhotoById($id: ID!) { getPhotoById(id: $id) { id title uploader { displayName } urls { size_1024 } width height } }（国际站把域名换为 api-neo.500px.com 即可）。签名 URL 约 2 小时过期，必须本轮内现取现用，不可存留到下轮。
   在找到 500KB+ 新链路之前，每轮直接走"一句话介绍 + 照片页链接"纯文本形式，不下载图片、不发小图；把"链接"形式记入结果。（恢复直发的两个可能前提，需用户拍板：①登录 500px 账号（登录态可能解锁 2048px，未验证）；②改用 Unsplash/Pexels 等免费高清图源。）
6. 发微信：从 inbox/、processed/ 里找该用户最新消息的 context_token（逻辑参考 <BRIDGE_DIR>/daily_push.py 的 latest_token），调用 python3 <BRIDGE_DIR>/send_reply.py --to "<WECHAT_USER_ID>" --context-token "TOKEN" --text "一句话介绍（中文，不用 Markdown，100 字以内：标题、摄影师、为什么选这张）附两个照片页链接（国内站 + 国际站）"。成功（输出含 ok:true）后把本轮照片记入 photo_taste.json 的 sent。
7. 在结果里一句话说明：发了哪张、介绍摘录、直发还是链接形式、发送是否成功；失败则说明原因。

不要改动 account.json、pull_config.json；不要删除 inbox/ 里的文件。

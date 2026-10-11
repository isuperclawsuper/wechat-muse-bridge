#!/usr/bin/env python3
"""每天定时微信推送: 12:00 / 18:00(上海时间)各一条.
内容: 高性价比人生指南(eternity4719/HowToLiveBetter)里性价比"极高"的条目,
每天两条轮换,进度记在 push_state.json.
从 inbox/ + processed/ 里找该用户最新消息的 context_token 发推;
token 长期不用可能过期, 发推失败时输出直接写进 cron 日志.
"""
import glob
import json
import os
import subprocess
import sys

ROOT = "/home/hatch/workspace/wechat-bridge"
USER = "YOUR_WECHAT_USER_ID@im.wechat"  # 换成你自己的微信用户 ID(绑定后第一条消息的发送者)
SEND = os.path.join(ROOT, "send_reply.py")
ITEMS = os.path.join(ROOT, "lifeguide", "items.json")
STATE = os.path.join(ROOT, "lifeguide", "push_state.json")


def top_items():
    try:
        items = json.load(open(ITEMS, encoding="utf-8"))
    except Exception:
        return []
    top = [x for x in items
           if x.get("tag", {}).get("收益") == "大"
           and x.get("tag", {}).get("钱") == "0"
           and x.get("tag", {}).get("时间") == "少"
           and x.get("tag", {}).get("毅力") == "否"]
    return top


def next_item():
    items = top_items()
    if not items:
        return None, None
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        st = {"idx": 0}
    idx = int(st.get("idx", 0)) % len(items)
    item = items[idx]
    st["idx"] = idx + 1
    json.dump(st, open(STATE, "w"), ensure_ascii=False)
    return idx, item


def fmt(idx, total, item):
    plain = item.get("plain", "").strip()
    if len(plain) > 220:
        plain = plain[:220] + "…"
    ev = item.get("evidence", "")
    ev_txt = f" 证据等级{ev}。" if ev else ""
    return ("人生指南推送: 第%d条/%d条: %s。%s%s"
            "来自开源项目 eternity4719/HowToLiveBetter，医疗法律类请以医生和当地规定为准。"
            % (idx + 1, total, item.get("title", ""), plain, ev_txt))


def latest_token(user):
    best = None
    for d in ("inbox", "processed"):
        for f in glob.glob(os.path.join(ROOT, d, "*.json")):
            try:
                with open(f) as fh:
                    m = json.load(fh)
            except Exception:
                continue
            if m.get("user_id") != user or not m.get("context_token"):
                continue
            mt = os.path.getmtime(f)
            if best is None or mt > best[0]:
                best = (mt, m["context_token"])
    return best[1] if best else None


def main():
    slot = sys.argv[1] if len(sys.argv) > 1 else "noon"
    token = latest_token(USER)
    if not token:
        print("FAIL: no context_token found for user")
        sys.exit(1)
    items = top_items()
    if not items:
        print("FAIL: no guide items loaded")
        sys.exit(1)
    idx, item = next_item()
    text = fmt(idx, len(items), item)
    r = subprocess.run(
        ["/usr/bin/python3", SEND, "--to", USER,
         "--context-token", token, "--text", text],
        capture_output=True, text=True, timeout=60)
    print("slot=%s item=%s rc=%d stdout=%s stderr=%s" %
          (slot, item.get("id"), r.returncode, r.stdout.strip(), r.stderr.strip()))
    sys.exit(0 if r.returncode == 0 and "ok" in (r.stdout or "") else 1)


if __name__ == "__main__":
    main()

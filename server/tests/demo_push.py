# -*- coding: utf-8 -*-
"""推送链冒烟：**画像 → 事件 → 匹配 → 预览 → 接收 → 去重**。

用法（项目根目录）：

    D:\\python.3.10.11\\python.exe server/tests/demo_push.py

## 用真实事件流，不用样例

`data/out/events.jsonl` 现在有真数据了（90 条，其中 85 条 `is_backfill=true`）。
这个脚本**优先用真实事件流**，只有文件不存在时才退回演示样例，并且**会在输出里说明**
用的是哪一种 —— 回退可以说，但不能悄悄发生。

**核心断言**：`items` 里不许有任何一条 `publish_date` 早于 **60 天**前的。
2026-10-19（B22）把窗口从 30 放宽到 60：推免季 9 月发的公告 10 月底才截止，
45 天前的推免办法依然有效。更早的仍然不许进来。

## 和 `demo_rehearsal.py` 的分工

- `demo_rehearsal.py`：**上台那 5 步**，起真实例、打真 HTTP，验"会不会断"。
- 这个脚本：**今天这一条链**，用注入的实例（不占端口、不用后台线程）。

两个都是**能给演示者看的**，不是门禁。门禁是 `server/tests/run.py` 第 18 节。

## 隔离

状态用 `:memory:`。**不碰 `server/var/state.json`。**
"""
from __future__ import annotations

import io
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from fastapi.testclient import TestClient  # noqa: E402

from server import config  # noqa: E402
from server.app import create_app  # noqa: E402
from server.events import EventStore  # noqa: E402
from server.push import parse_day  # noqa: E402
from server.state import StateStore  # noqa: E402
from server.store import FacultyStore, NoticeStore  # noqa: E402

LINE = "─" * 78

# 演示画像：只填最小四字段（就是产品要用户填的那四个）
PROFILE = {
    "school": "浙江大学",
    "majors": ["物理学"],
    "preferred_schools": ["北京大学", "复旦大学", "浙江大学"],
    "interest_keywords": ["物理", "材料", "化学"],
}

FREE_USER = "u_demo"
SUB_USER = "u_demo_sub"

# 首屏窗口（B22，2026-10-19）：近 60 天。**不是 `is_backfill` 的 30 天**——
# 推免季 9 月发的公告 10 月底才截止，45 天前的推免办法依然有效（用户拍板）。
WINDOW_DAYS = 60


class Fail(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Fail(msg)


def _load_events():
    """优先真实事件流。文件不存在才退回样例，并把来源**打印出来**。"""
    path = config.EVENTS_PATH
    if os.path.exists(path):
        store = EventStore.load(path)
        return store, "真实事件流（%s）" % os.path.relpath(path, ROOT)
    store = EventStore.load_sample(
        os.path.join(ROOT, "contracts", "fixtures", "events.sample.json"))
    return store, "**演示样例**（真实事件流还不存在）"


def main():
    print()
    print(LINE)
    print("  推送链冒烟：画像 → 事件 → 匹配 → 预览 → 接收 → 去重")
    print(LINE)

    ev_store, ev_label = _load_events()
    app = create_app(
        NoticeStore.load(os.path.join(ROOT, "data", "out", "notices.jsonl")),
        FacultyStore.load(os.path.join(ROOT, "data", "out", "faculty.jsonl")),
        StateStore.load(":memory:"),          # **状态隔离**：不碰 server/var/
        events=ev_store,
    )
    c = TestClient(app)
    print("  事件来源：%s" % ev_label)
    print("  事件条数：%d（其中 is_backfill=%d）" % (
        len(ev_store.records),
        sum(1 for e in ev_store.records if e.get("is_backfill"))))
    print()

    steps = []
    today = date.today()
    try:
        # ① 填画像
        r = c.put("/api/profile", params={"user": SUB_USER}, json=PROFILE)
        need(r.status_code == 200, "写画像 HTTP %s" % r.status_code)
        body = r.json()
        need(body["complete"]["can_push"] is True, "画像存了但 can_push 是 false")
        steps.append(("① 填画像", "can_push=true", body["complete"]["message"][:46]))

        # ② 免费身份：前 3 条、无提醒
        c.put("/api/profile", params={"user": FREE_USER}, json=PROFILE)
        free = c.get("/api/push/preview", params={"user": FREE_USER}).json()
        need(free["items"], "免费层一条都没有 —— 那用户没法判断值不值得买")
        # 2026-10-19 免费层 3 -> 10（B16）。**写死 10 不引用常量**：引用常量等于让实现和自己比。
        need(len(free["items"]) <= 10, "免费层给了 %d 条，最多 10" % len(free["items"]))
        need(all(i["reminder"] is None for i in free["items"]), "免费层拿到了提醒")
        steps.append(("② 免费预览", "%d 条 / 截断=%s" % (
            len(free["items"]), free["tally"]["truncated_reason"]),
            "三态 + 原文链接照给，提醒归订阅"))

        # ③ 开通（**query 传 user**，和全项目一致；issue 008 第 2 条）
        r = c.post("/api/subscriptions", params={"user": SUB_USER},
                   json={"user_id": SUB_USER, "code": config.PUSH_ACTIVATION_CODE})
        need(r.status_code == 200, "开通 HTTP %s" % r.status_code)
        need(r.json()["method"] == "manual_code", "没标清是人工开通而不是支付")
        need(r.json()["user_id"] == SUB_USER, "开通到了别人头上：%s" % r.json()["user_id"])
        steps.append(("③ 开通订阅", r.json()["plan"]["reason"],
                      r.json()["plan"]["text"][:40]))

        # ④ 订阅身份：全量
        sub = c.get("/api/push/preview", params={"user": SUB_USER, "limit": 50}).json()
        need(sub["items"], "订阅身份一条都没有")
        # **只断言"没被免费层截断"，不断言 `truncated is False`。**
        # A 补了 196 条存量事件之后，非补抓命中 35 条 > 每日上限 20 ——
        # 被 `limit` 截断是**正确行为**，不是 bug。
        # 原来那句 `truncated is False` 绑死了"事件量刚好小于上限"，一加数据就红，
        # 而红的理由和代码无关（这一晚栽过五次的那类）。
        need(sub["tally"]["truncated_reason"] != "free",
             "订阅身份居然被免费层截断了：%s" % sub["tally"])

        # **核心：不许有窗口外的旧公告**（B22：近 60 天；原来按补抓的 30 天）
        cutoff = today - timedelta(days=WINDOW_DAYS)
        old = []
        for it in sub["items"]:
            pub = parse_day(it.get("publish_date"))
            if pub is None or pub < cutoff:
                old.append((it["event_id"], it.get("publish_date")))
        need(not old, "items 里有早于 %d 天前（或读不到发布日）的公告：%s" % (
            WINDOW_DAYS, old[:3]))

        # 三态说法（有哪态验哪态 —— 真实数据不保证三态齐全）
        by_state = {}
        for it in sub["items"]:
            by_state.setdefault(it["deadline"]["state"], []).append(it)
        absent_text = by_state.get("absent", [{}])[0].get("deadline", {}).get("text")
        unknown_text = by_state.get("unknown", [{}])[0].get("deadline", {}).get("text")
        if absent_text and unknown_text:
            need(absent_text != unknown_text, "absent 和 unknown 说了同一句话")
        for it in by_state.get("unknown", []):
            need(it["deadline"]["must_check_original"] is True,
                 "unknown 没标'要去看原文'")
            for bad in ("暂无", "——"):
                need(bad not in it["deadline"]["text"],
                     "unknown 用了省略说法：%s" % it["deadline"]["text"])

        # 每条都要能回链
        for it in sub["items"] + free["items"]:
            need(str(it.get("url", "")).startswith("http"), "缺原文链接：%s" % it["event_id"])

        steps.append(("④ 订阅预览", "%d 条 / 提醒 %s" % (
            len(sub["items"]),
            sorted({(i["reminder"] or {}).get("kind") for i in sub["items"]} - {None})),
            "无补抓旧公告；三态 %s" % "/".join(sorted(by_state))))

        # ⑤ 接收 → 去重
        posted = c.post("/api/push/preview", params={"user": SUB_USER}, json={}).json()
        need(posted["tally"]["committed"] is True, "POST 没有记录")
        again = c.get("/api/push/preview", params={"user": SUB_USER, "limit": 50}).json()
        need(again["items"] == [], "接收之后还能再看到同样的推送")
        reasons = {x["reason"] for x in again["suppressed"]}
        need("duplicate" in reasons, "接收后没看到 duplicate：%s" % reasons)
        need(all(x["text"] for x in again["suppressed"]), "压掉了却不说为什么")
        steps.append(("⑤ 接收 → 去重",
                      "%d 条被压（%s）" % (len(again["suppressed"]), "/".join(sorted(reasons))),
                      "压掉的都说了为什么"))

        # ⑥ 删画像 → 不再推送
        d = c.delete("/api/profile", params={"user": SUB_USER}).json()
        need(d["deleted"] is True, d)
        gone = c.get("/api/push/preview", params={"user": SUB_USER}).json()
        need(gone["items"] == [], "删了画像还在推")
        need(gone["empty_reason"] == "no_profile", gone["empty_reason"])
        need(gone["empty_text"], "空原因没有给人看的一句话")
        steps.append(("⑥ 删画像", "no_profile", gone["empty_text"][:44]))

    except Fail as e:
        steps.append(("✗ 断了", "", str(e)))
    except Exception as e:                              # noqa: BLE001
        steps.append(("✗ 崩了", type(e).__name__, str(e)[:120]))

    print()
    for no, note, detail in steps:
        flag = "✗" if no.startswith("✗") else "✓"
        print("  %-14s %s  %s" % (no, flag, note))
        if detail:
            print("  %-14s     %s" % ("", detail))
    print()
    print(LINE)

    ok = all(not s[0].startswith("✗") for s in steps) and len(steps) == 6
    if ok:
        print("  六步都通。这条链可以录进视频了。")
        if ev_label.startswith("**"):
            print("  ⚠️ 现在用的是**样例事件**（真实事件流还不存在）—— 录屏时画面里要标出来。")
        else:
            print("  用的是真实事件流。")
            print("  提醒：**60 天窗口内的公告即使补抓（is_backfill=true）也进首屏**")
            print("        （9 月发的推免办法，10 月底才截止，今天仍然有效）；")
            print("        窗口外的进 suppressed，reason=out_of_window。")
    else:
        print("  ★ 断在上面标 ✗ 的那一步。演示前必须修好。")
    print(LINE)
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

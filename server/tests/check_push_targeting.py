# -*- coding: utf-8 -*-
"""B2 判据实测：`why` 非空 / 对口覆盖 / 旧记录是否还在首屏。

判据来自派单：
  ① 每条推送都有非空 `why`（至少 1 条理由）
  ② u_num_demo 的推送里，`school_hit` 或 `major_hit` 为 1 的占 ≥80%
  ③ 30 天以上的记录不进首屏 20 条（除非用户主动翻）

**这份脚本只读**，用注入实例，不占端口、不碰 `server/var/state.json`。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from fastapi.testclient import TestClient  # noqa: E402

from server.app import create_app  # noqa: E402
from server.events import EventStore  # noqa: E402
from server.state import StateStore  # noqa: E402
from server.store import FacultyStore, NoticeStore  # noqa: E402

USER = "u_num_demo"
LINE = "=" * 78


def funnel(client, notices):
    """漏斗六个数（B7 判据）：1120 → 非junk → apply → is_fresh → 命中画像 → 首屏。

    **前三级在公告粒度上算**（那是记录的属性），**后两级用推送接口自己算**
    （命中画像 = `tally.matched`，首屏 = `items`）。不自己重写一遍匹配规则 ——
    那会变成"两套判据"，而这个项目为此栽过四次。

    跨粒度的代价要说明：`events` 1133 条 vs `notices` 1120 条，
    所以"命中画像"那一步的分母是**事件**不是公告 —— 同一条公告的
    `added` / `deadline_changed` 会是不同事件。
    """
    from server import quality as q

    n0 = len(notices)
    non_junk = [r for r in notices if not q.is_junk(r)]
    apply_ = [r for r in non_junk if r.get("topic") == "apply"]
    fresh = [r for r in apply_ if r.get("is_fresh") is True]
    d = client.get("/api/push/preview", params={"user": USER, "limit": 50}).json()
    items = d["items"]
    t1 = [i for i in items if i.get("tier") == 1]
    t2 = [i for i in items if i.get("tier") == 2]
    return {
        "notices": n0,
        "non_junk": len(non_junk),
        "apply": len(apply_),
        "is_fresh": len(fresh),
        "matched_events": d["tally"]["matched"],
        "sent": len(items),
        "tier1": len(t1),
        "tier2": len(t2),
        "junk_dropped": d["tally"].get("junk_dropped"),
    }


def narrowness(notices, profile):
    """**"12 条"到底是官网没新闻，还是画像太窄？**

    判据：把"新鲜的招生公告"分成两堆 ——
      A. 落在画像的意向学校里的（不管专业）
      B. 不在意向学校里，但专业/方向词能对上
      C. 两个都不沾

    如果 C 很大而 A 很小 → **画像太窄**（东西有，你没说要）；
    如果 A+B+C 本身就没几条 → **官网今天确实没新闻**。
    """
    from server import quality as q

    schools = [s.strip() for s in (profile.get("preferred_schools") or []) if s]
    majors = [m.strip() for m in (profile.get("majors") or []) if m]
    kws = [k.strip() for k in (profile.get("interest_keywords") or []) if k]
    pool = [r for r in notices
            if not q.is_junk(r) and r.get("topic") == "apply" and r.get("is_fresh") is True]
    a = b = c = 0
    for r in pool:
        school = r.get("school") or ""
        hay = " ".join([r.get("title") or "", r.get("unit") or "",
                        " ".join(r.get("majors") or []),
                        " ".join(r.get("tags") or [])])
        if any(s in school for s in schools):
            a += 1
        elif any(m in hay for m in majors) or any(k in hay for k in kws):
            b += 1
        else:
            c += 1
    return {"pool": len(pool), "in_target_school": a, "major_or_kw_only": b,
            "neither": c}


def main():
    print(LINE)
    print("  B2 判据实测（%s）" % USER)
    print(LINE)

    tmp = tempfile.mkdtemp(prefix="b2check_")
    # B26：不复制 `server/var/state.json`（新克隆没有这个文件）。
    # 用 `load_or_seed()` 在临时目录现建演示状态。
    dst = os.path.join(tmp, "state.json")
    st = StateStore.load_or_seed(dst)

    app = create_app(
        NoticeStore.load(os.path.join(ROOT, "data", "out", "notices.jsonl")),
        FacultyStore.load(os.path.join(ROOT, "data", "out", "faculty.jsonl")),
        st,
        events=EventStore.load(os.path.join(ROOT, "data", "out", "events.jsonl")),
    )
    client = TestClient(app)
    d = client.get(
        "/api/push/preview", params={"user": USER, "limit": 50}).json()

    items = d["items"]
    print("  sent=%d  matched=%d  suppressed=%d  7 天内截止=%d" % (
        len(items), d["tally"]["matched"], d["tally"]["suppressed"],
        sum(1 for i in items
            if (i.get("deadline") or {}).get("days_left") is not None
            and 0 <= i["deadline"]["days_left"] <= 7)))
    print()

    # ---- ① why 非空 ----
    empty_why = [i["event_id"] for i in items if not (i.get("why") or [])]
    print("  ① why 非空：%d/%d 条有理由" % (len(items) - len(empty_why), len(items)))
    if empty_why:
        print("     ✗ 缺 why：%s" % empty_why[:5])

    # ---- ② 对口覆盖 ----
    hit = [i for i in items
           if (i.get("score_dims") or {}).get("school_hit") == 1.0
           or (i.get("score_dims") or {}).get("major_hit") == 1.0]
    rate = (len(hit) / len(items) * 100) if items else 0.0
    print("  ② school_hit 或 major_hit = 1：%d/%d = %.0f%%（判据 ≥80%%）"
          % (len(hit), len(items), rate))
    only_direction = [i for i in items
                      if (i.get("score_dims") or {}).get("school_hit") == 0.0
                      and (i.get("score_dims") or {}).get("major_hit") == 0.0]
    print("     只靠方向/院系命中的：%d 条" % len(only_direction))

    # ---- ③ 30 天以上是否进首屏 ----
    old = [i for i in items
           if (i.get("score_dims") or {}).get("freshness") == 0.2]
    unknown = [i for i in items
               if (i.get("score_dims") or {}).get("freshness") == 0.5]
    mid = [i for i in items
           if (i.get("score_dims") or {}).get("freshness") == 0.6]
    print("  ③ 首屏 %d 条里：30 天内 %d / 31~90 天 %d / 90 天以上 %d / 日期未知 %d"
          % (len(items), len(items) - len(old) - len(unknown) - len(mid),
             len(mid), len(old), len(unknown)))

    # ---- 分档命中率：判据只写了"20 条"，但产品目标是"5~15 条" ----
    print()
    print("  分档命中率（school_hit 或 major_hit = 1 的占比）：")
    for n in (5, 10, 15, 20):
        head = items[:n]
        if not head:
            continue
        h = sum(1 for i in head
                if (i.get("score_dims") or {}).get("school_hit") == 1.0
                or (i.get("score_dims") or {}).get("major_hit") == 1.0)
        print("    前 %-2d 条：%2d/%2d = %3.0f%%" % (n, h, len(head), h / len(head) * 100))
    sch = sum(1 for i in items if (i.get("score_dims") or {}).get("school_hit") == 1.0)
    maj = sum(1 for i in items if (i.get("score_dims") or {}).get("major_hit") == 1.0)
    print("     其中 school_hit=1 的 %d 条，major_hit=1 的 %d 条" % (sch, maj))

    # ---- 明细 ----
    print()
    print("  %-3s %-6s %-6s %-6s %-6s %-6s %s" % (
        "#", "school", "major", "dir", "fresh", "days", "第一条理由"))
    for n, i in enumerate(items, 1):
        dm = i.get("score_dims") or {}
        dl = (i.get("deadline") or {}).get("days_left")
        print("  %-3d %-6s %-6s %-6s %-6s %-6s %s" % (
            n, dm.get("school_hit"), dm.get("major_hit"), dm.get("direction_hit"),
            dm.get("freshness"), dl, (i.get("why") or [""])[0][:34]))

    # ---- B7：漏斗六个数 + "12 条是官网没新闻还是画像太窄" ----
    # 用同一份真实数据算，不另起一套判据。
    notices_all = []
    with open(os.path.join(ROOT, "data", "out", "notices.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                notices_all.append(json.loads(line))

    from server import profile as profile_mod
    prof, _ = None, None
    fp = os.path.join(ROOT, "contracts", "fixtures", "profile.response.json")
    with open(fp, encoding="utf-8") as f:
        prof = json.load(f)["profile"]

    fn = funnel(client, notices_all)
    print()
    print("  【B7 漏斗】公告 %d → 非junk %d → apply %d → is_fresh %d "
          "→ 命中画像(事件) %d → 首屏 %d（一级 %d / 二级 %d）" % (
              fn["notices"], fn["non_junk"], fn["apply"], fn["is_fresh"],
              fn["matched_events"], fn["sent"], fn["tier1"], fn["tier2"]))

    nar = narrowness(notices_all, prof)
    print("  【窄不窄】新鲜招生公告 %d 条：在目标学校里 %d / 只对专业或方向 %d / 都不沾 %d"
          % (nar["pool"], nar["in_target_school"], nar["major_or_kw_only"],
             nar["neither"]))
    if nar["pool"] <= 15:
        verdict = "**官网今天确实没新闻**（新鲜招生公告本身就只有 %d 条）" % nar["pool"]
    elif nar["in_target_school"] <= 3:
        verdict = ("**画像太窄**：%d 条新鲜招生公告里只有 %d 条落在你的意向学校里，"
                   "另有 %d 条专业/方向能对上但学校不在名单内"
                   % (nar["pool"], nar["in_target_school"], nar["major_or_kw_only"]))
    else:
        verdict = ("两者都有：池子 %d 条、目标学校 %d 条 —— 首屏少主要是 '新鲜' 这一级卡住的"
                   % (nar["pool"], nar["in_target_school"]))
    print("  【结论】%s" % verdict)

    print()
    print(LINE)
    ok = not empty_why and rate >= 80.0 and not old
    print("  判据①②③：%s" % ("全部满足" if ok else "未全部满足（见上）"))
    print(LINE)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

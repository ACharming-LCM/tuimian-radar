# -*- coding: utf-8 -*-
"""推送量测量：换一份 events 文件，看用户**实际收到**的推送变不变。

## 为什么要它

`events.jsonl` 是推送链路的唯一输入。数据层补一批事件（比如 2026-10-18 补齐 655 条）
之后，"推送会暴增吗"是一个**必须实测、不能推理**的问题：
候选池变大不等于推送变多，中间夹着 `is_backfill` / `merged` / `duplicate` /
`rate_limited` / `limit` 五道闸，哪一道在起作用只有跑一遍才知道。

这个脚本用**注入实例**（不占端口、不碰 `server/var/state.json`），
把两份 events 文件各自的推送结果并排打出来。

## 用法

    # 和上一个提交比（最常用：量"这次数据变更"的影响）
    D:\\python.3.10.11\\python.exe server/tests/measure_push_volume.py --before-rev HEAD

    # 和任意一次提交比
    D:\\python.3.10.11\\python.exe server/tests/measure_push_volume.py --before-rev 0cccc6a^

    # 两份现成文件比
    D:\\python.3.10.11\\python.exe server/tests/measure_push_volume.py --before old.jsonl --after new.jsonl

## 一个我自己踩过的坑（所以脚本这么写）

**不要用 `git show rev:path | Out-File` 取基线。** PowerShell 5.1 会写 UTF-8 BOM，
而 `EventStore` 逐行 `json.loads` —— 带 BOM 的第一行解析失败会被**静默跳过**，
于是"476 条"读成"475 条"。两边都少 1，看起来像数据少了一条，
其实是取数方式吃掉了第一条事件。这里走 `subprocess` 取**字节**，不经过任何编码层。

## 判据（怎么读结果）

- `sent` 不该因为候选池变大而变大 —— 它卡在 `daily_limit`。
- `suppressed` 里 `backfill` 的增量应该吃掉大部分新增命中；剩下的是 `rate_limited`。
- 前 5 条是不是仍然"7 天内截止"，以及**整张列表**换了哪几条。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

LINE = "=" * 78
DEFAULT_USER = "u_num_demo"


def _git_blob_bytes(rev, relpath):
    """从 git 取文件的**字节**。不经过 shell、不经过编码层（理由见模块 docstring）。"""
    spec = "%s:%s" % (rev, relpath.replace("\\", "/"))
    r = subprocess.run(["git", "show", spec], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise SystemExit("git show 失败：%s\n%s" % (
            spec, r.stderr.decode("utf-8", "replace")[:400]))
    return r.stdout


def _ensure_bytes(events_spec, tmpdir, tag):
    """`events_spec` 可以是文件路径，也可以是 git rev（此时取 HEAD 那份 events.jsonl）。"""
    if events_spec is None:
        events_spec = os.path.join(ROOT, "data", "out", "events.jsonl")
    if os.path.exists(events_spec):
        return os.path.abspath(events_spec)
    data = _git_blob_bytes(events_spec, "data/out/events.jsonl")
    dst = os.path.join(tmpdir, "events_%s.jsonl" % tag)
    with open(dst, "wb") as f:
        f.write(data)
    return dst


def measure(label, events_path, notices, faculty, state_snapshot, user, limit):
    tmpdir = tempfile.mkdtemp(prefix="measure_%s_" % label)
    dst = os.path.join(tmpdir, "state.json")
    shutil.copyfile(state_snapshot, dst)
    ev = EventStore.load(events_path)
    app = create_app(notices, faculty, StateStore.load(dst), events=ev)
    c = TestClient(app)
    r = c.get("/api/push/preview", params={"user": user, "limit": limit})
    if r.status_code != 200:
        raise SystemExit("%s: HTTP %s\n%s" % (label, r.status_code, r.text[:400]))
    d = r.json()

    items = d["items"]
    rows = []
    for i, it in enumerate(items, 1):
        dl = it.get("deadline") or {}
        days = dl.get("days_left")
        rows.append({
            "n": i,
            "event_id": it["event_id"],
            "school": it.get("school"),
            "title": it.get("title"),
            "state": dl.get("state"),
            "days_left": days,
            "urgency": it.get("urgency_weight"),
            "relevance": it.get("relevance"),
            "within7": days is not None and 0 <= days <= 7,
            "expired": days is not None and days < 0,
            # 数据质量观察：标题就是站点名/栏目名时，卡片上等于没有标题
            "title_is_site_name": (it.get("title") or "").strip().endswith(
                ("招生网", "研究生院", "招生信息网", "（测试版）", "(测试版)")),
        })

    return {
        "label": label,
        "events_file": os.path.relpath(events_path, ROOT)
        if events_path.startswith(ROOT) else events_path,
        "events_total": len(ev.records),
        "events_backfill": sum(1 for e in ev.records if e.get("is_backfill")),
        "tally": d["tally"],
        "empty_reason": d.get("empty_reason"),
        "suppressed_by_reason": dict(Counter(x["reason"] for x in d["suppressed"])),
        "items": rows,
    }


def print_table(before, after):
    print()
    print("  %-26s %10s %10s %8s" % ("指标", "before", "after", "Δ"))
    print("  " + "-" * 58)
    keys = ["events", "matched", "backfill", "unmatched", "sent",
            "suppressed", "expired", "expired_matched", "used_today",
            "daily_limit", "free_limit"]
    for k in keys:
        b, a = before["tally"].get(k), after["tally"].get(k)
        delta = (a - b) if isinstance(a, int) and isinstance(b, int) else ""
        print("  %-26s %10s %10s %8s" % (k, b, a, delta))
    print()
    print("  抑制构成：")
    for k in sorted(set(before["suppressed_by_reason"]) | set(after["suppressed_by_reason"])):
        b = before["suppressed_by_reason"].get(k, 0)
        a = after["suppressed_by_reason"].get(k, 0)
        print("  %-26s %10d %10d %+8d" % (k, b, a, a - b))


def print_items(res, topn=20):
    print()
    print("  【%s】events=%d（backfill=%d）sent=%d matched=%d suppressed=%d" % (
        res["label"], res["events_total"], res["events_backfill"],
        len(res["items"]), res["tally"]["matched"], res["tally"]["suppressed"]))
    print("  %-4s %-5s %-5s %-8s %-7s %s" % ("#", "days", "urg", "rel*urg", "state", "school / title"))
    inversions = []
    prev = None
    for r in res["items"][:topn]:
        key = round((r["relevance"] or 0) * (r["urgency"] or 0), 4)
        if prev is not None and key > prev + 1e-9:
            inversions.append((r["n"], prev, key))
        prev = key
        flag = " ⚠标题是站点名" if r["title_is_site_name"] else ""
        print("  %-4d %-5s %-5s %-8s %-7s %s / %s%s" % (
            r["n"], r["days_left"], r["urgency"], key, r["state"],
            (r["school"] or "")[:8], (r["title"] or "")[:38], flag))
    top = res["items"][:topn]
    print("  → 7 天内截止 %d/%d；已截止 %d；排序键逆行 %s；标题是站点名 %d/%d" % (
        sum(1 for r in top if r["within7"]), len(top),
        sum(1 for r in top if r["expired"]),
        inversions or "无",
        sum(1 for r in top if r["title_is_site_name"]), len(top)))


def check_match(notices, faculty, state_snapshot):
    """`/api/match` 的 `dropped_expired` 默认必须是 0（已截止默认也返回）。"""
    tmpdir = tempfile.mkdtemp(prefix="measure_match_")
    dst = os.path.join(tmpdir, "state.json")
    shutil.copyfile(state_snapshot, dst)
    app = create_app(notices, faculty, StateStore.load(dst), events=None)
    c = TestClient(app)
    prof = {"school": "浙江大学", "majors": ["物理学"],
            "preferred_schools": ["北京大学", "复旦大学", "浙江大学"],
            "interest_keywords": ["物理", "材料", "化学"]}
    d = c.post("/api/match", json=dict(prof, limit=5)).json()
    st = d["stats"]
    print()
    print("  /api/match 默认（exclude_expired 关）：%s" % json.dumps(st, ensure_ascii=False))
    print("    恒等式 total == dropped_expired + dropped_mode + scored → %s"
          % (st["total"] == st["dropped_expired"] + st["dropped_mode"] + st["scored"]))
    print("    dropped_expired == 0 → %s" % (st["dropped_expired"] == 0))
    st2 = c.post("/api/match", json=dict(prof, exclude_expired=True, limit=5)).json()["stats"]
    print("    显式 exclude_expired=true → dropped_expired=%d（能力仍在：%s）"
          % (st2["dropped_expired"], st2["dropped_expired"] > 0))
    return st, st2


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--before", help="补之前的 events 文件路径")
    ap.add_argument("--after", help="补之后的 events 文件路径（默认 data/out/events.jsonl）")
    ap.add_argument("--before-rev", help="用某个 git rev 里的 events.jsonl 当 before")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--json", dest="json_out", help="把明细写成 JSON")
    args = ap.parse_args()

    before_spec = args.before or args.before_rev
    if not before_spec:
        raise SystemExit("要给 --before / --before-rev / 或 --after 两份文件之一（见 --help）")

    print(LINE)
    print("  推送量测量：%s" % args.user)
    print(LINE)

    notices = NoticeStore.load(os.path.join(ROOT, "data", "out", "notices.jsonl"))
    faculty = FacultyStore.load(os.path.join(ROOT, "data", "out", "faculty.jsonl"))

    tmpdir = tempfile.mkdtemp(prefix="measure_push_")
    state_snapshot = os.path.join(tmpdir, "state_snapshot.json")
    # B26：不复制 `server/var/state.json`（新克隆没有这个文件）。
    # 用 `load_or_seed()` 建一份带演示画像的临时状态，测量结果也可复现。
    StateStore.load_or_seed(state_snapshot)

    before_path = _ensure_bytes(before_spec, tmpdir, "before")
    after_path = _ensure_bytes(args.after, tmpdir, "after")

    print("  公告：%d 条（两次测量同一份）" % len(notices.records))
    print("  before: %s" % before_path)
    print("  after : %s" % after_path)

    before = measure("before", before_path, notices, faculty, state_snapshot,
                     args.user, args.limit)
    after = measure("after", after_path, notices, faculty, state_snapshot,
                    args.user, args.limit)

    print_table(before, after)
    print_items(before, args.topn)
    print_items(after, args.topn)

    # 整张列表换了哪几条
    idb = [r["event_id"] for r in before["items"]]
    ida = [r["event_id"] for r in after["items"]]
    print()
    print("  下发列表逐位相同：%s" % (idb == ida))
    if idb != ida:
        only_b = [x for x in idb if x not in ida]
        only_a = [x for x in ida if x not in idb]
        print("    换掉 %d 条；只在 before：%s" % (len(only_b), only_b[:5]))
        print("                   只在 after ：%s" % only_a[:5])

    m1, m2 = check_match(notices, faculty, state_snapshot)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"before": before, "after": after,
                       "notices": len(notices.records),
                       "match_default": m1, "match_exclude_expired": m2},
                      f, ensure_ascii=False, indent=2)
        print()
        print("  明细已写入 %s" % args.json_out)
    print(LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

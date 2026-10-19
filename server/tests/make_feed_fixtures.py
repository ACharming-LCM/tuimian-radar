# -*- coding: utf-8 -*-
"""重出 `/api/updates` 与 `/api/library` 的 fixture 形状基准。

## 为什么这两份 fixture 只钉**形状**，不钉数值

`push.preview.json` 也一样。这两份响应的内容**每次数据变化都会变**：
`updates` 是"最近 N 天"，`library` 是它的补集 —— 只要 A 跑一轮管道，
条数、标题、日期全变。把数值钉死，门禁就会天天红，而红的理由和代码无关
（这个项目栽过五次，`notices.list.json` 的 `total` 就是这一类）。

所以：**形状永久有效、逐字段比对；数值只反映生成那一刻。**
`_as_of` / `_note` 这些标注写进 responses 同级、不进响应体
（`match.response.json` 曾经塞过 `_` 键，把 B 的"键集合相等"断言弄红）。

## 用法

    D:\\python.3.10.11\\python.exe server/tests/make_feed_fixtures.py
    D:\\python.3.10.11\\python.exe server/tests/make_feed_fixtures.py --check
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

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
from server.state import StateStore  # noqa: E402
from server.store import FacultyStore, NoticeStore  # noqa: E402

FIX = os.path.join(ROOT, "contracts", "fixtures")

# 用**真实事件流**。理由：样例事件只有 5 条、且都算"新鲜"，
# 于是 library 是空的 —— 空 items 覆盖不到条目形状，fixture 就只钉住了外壳。
# 真实事件流两份都有内容。**只比形状**，所以内容随数据漂移不影响门禁。
TARGETS = [
    ("updates.response.json", "/api/updates", {"days": 7, "limit": 50}),
    ("library.response.json", "/api/library", {"size": 20}),
]


def build():
    app = create_app(
        NoticeStore.load(os.path.join(ROOT, "data", "out", "notices.jsonl")),
        FacultyStore.load(os.path.join(ROOT, "data", "out", "faculty.jsonl")),
        StateStore.load(":memory:"),
        events=EventStore.load(config.EVENTS_PATH),
    )
    c = TestClient(app)
    out = {}
    for name, path, params in TARGETS:
        r = c.get(path, params=params)
        if r.status_code != 200:
            raise SystemExit("%s -> HTTP %s\n%s" % (path, r.status_code, r.text[:400]))
        out[name] = r.json()
    return out


def key_paths(d, prefix=""):
    out = set()
    if isinstance(d, dict):
        for k, v in d.items():
            if k.startswith("_"):
                continue
            out.add(prefix + k)
            out |= key_paths(v, prefix + k + ".")
    elif isinstance(d, list):
        for x in d:
            out |= key_paths(x, prefix + "[].")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写")
    args = ap.parse_args()

    fresh = build()
    ok = True
    for name, body in fresh.items():
        path = os.path.join(FIX, name)
        if args.check:
            if not os.path.exists(path):
                print("✗ 缺 %s" % name)
                ok = False
                continue
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            g, f_ = key_paths(old), key_paths(body)
            if g == f_:
                print("✓ %s 形状一致（%d 个键路径）" % (name, len(g)))
            else:
                ok = False
                print("✗ %s 已落后：fixture 多 %s / 少 %s"
                      % (name, sorted(g - f_), sorted(f_ - g)))
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(body, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("已重出 %s（%d 个键路径，items=%d）"
              % (name, len(key_paths(body)), len(body.get("items") or [])))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

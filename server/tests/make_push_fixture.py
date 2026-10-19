# -*- coding: utf-8 -*-
"""重出 `contracts/fixtures/push.preview.json`。

## 为什么要有这个脚本

这份 fixture 一直是**手写**的（历任 B 直接编辑 JSON）。加一个键就要在 3 个
item 里各抄一遍，抄漏一个 → 门禁红在"键集合不一致"，而真正的原因是"人漏了"。
同一种错在 `make_fixtures.py` 那边已经栽过（`field_trust` / `matchable` 两次）。

所以改成**从真实响应生成**：跑一次 `?source=sample`，把响应当 fixture，
`_` 开头的元信息（说明、as_of、复现步骤）从旧 fixture 原样继承。
这样 fixture 永远等于"接口此刻真的返回什么"，想改形状只能改接口。

## 用法

    D:\\python.3.10.11\\python.exe server/tests/make_push_fixture.py
    D:\\python.3.10.11\\python.exe server/tests/make_push_fixture.py --check

`--check` 只比对、不写，用于提交前确认 fixture 没落后。
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

FIX = os.path.join(ROOT, "contracts", "fixtures", "push.preview.json")
PROFILE_FIX = os.path.join(ROOT, "contracts", "fixtures", "profile.response.json")
USER = "u_demo_sub"


def load_profile():
    """画像取 `profile.response.json` 里那份 —— **不是**门禁里的 `_PUSH_PROFILE`。

    依据是 fixture 自己写的 `_how_to_reproduce`：「画像用 profile.response.json 那份」。
    这不是随便挑的：门禁那份只填了浙江大学/中山大学，样例事件里的
    大连理工大学、中国科学技术大学都会判成"不匹配"，于是 fixture 只剩 1 条，
    `change: null`、`reminder: null` 这些形状就**从 fixture 里消失了** ——
    而形状 fixture 的价值恰恰在于覆盖到各种分支。
    """
    with open(PROFILE_FIX, encoding="utf-8") as f:
        return json.load(f)["profile"]


def build_response():
    app = create_app(
        NoticeStore.load(os.path.join(ROOT, "data", "out", "notices.jsonl")),
        FacultyStore.load(os.path.join(ROOT, "data", "out", "faculty.jsonl")),
        StateStore.load(":memory:"),
        events=EventStore.load_sample(config.EVENTS_SAMPLE_PATH),
    )
    c = TestClient(app)
    c.put("/api/profile", params={"user": USER}, json=load_profile())
    c.post("/api/subscriptions", params={"user": USER},
           json={"user_id": USER, "code": config.PUSH_ACTIVATION_CODE})
    r = c.get("/api/push/preview", params={"source": "sample", "user": USER})
    if r.status_code != 200:
        raise SystemExit("HTTP %s\n%s" % (r.status_code, r.text[:500]))
    return r.json()


def key_paths(d, prefix=""):
    out = set()
    if isinstance(d, dict):
        for k, v in d.items():
            out.add(prefix + k)
            out |= key_paths(v, prefix + k + ".")
    elif isinstance(d, list):
        for x in d:
            out |= key_paths(x, prefix + "[].")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = ap.parse_args()

    fresh = build_response()

    old = {}
    if os.path.exists(FIX):
        with open(FIX, encoding="utf-8") as f:
            old = json.load(f)

    # 元信息：`_` 开头的那几个键，从旧 fixture 继承（它们是给人看的说明，
    # 不是接口返回的东西 —— 门禁的 key_paths 也会跳过 `_`）。
    for k, v in old.items():
        if k.startswith("_") and k not in fresh:
            fresh[k] = v

    if args.check:
        g, f = key_paths(old), key_paths(fresh)
        if g == f:
            print("fixture 形状与当前接口一致（%d 个键路径）" % len(g))
            return 0
        print("**fixture 已落后**：")
        print("  fixture 多了（接口已删）：%s" % sorted(g - f))
        print("  fixture 少了（接口已加）：%s" % sorted(f - g))
        return 1

    with open(FIX, "w", encoding="utf-8", newline="\n") as f:
        json.dump(fresh, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("已重出 %s" % os.path.relpath(FIX, ROOT))
    print("  items=%d suppressed=%d 键路径=%d"
          % (len(fresh["items"]), len(fresh["suppressed"]), len(key_paths(fresh))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

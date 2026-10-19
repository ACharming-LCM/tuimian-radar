# -*- coding: utf-8 -*-
"""从公告和导师数据里派生出「院系清单」，给论坛的 facets 当初始种。

为什么需要：论坛的 `unit` 用自由文本（不强制固定清单，那样太脆），
但**没有建议值的话，「物理学院」和「物理系」会裂成两个版块**，
匿名社区最怕的就是内容被拆散。

用法：python tools/make_units.py
产出：data/out/units.json
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common        # noqa: E402


def load(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def main():
    notices = load(os.path.join(common.OUT_DIR, "notices.jsonl"))
    faculty = load(os.path.join(common.OUT_DIR, "faculty.jsonl"))

    agg = {}
    for r in notices:
        k = (r.get("school"), r.get("unit"))
        agg.setdefault(k, {"notice_count": 0, "faculty_count": 0})
        agg[k]["notice_count"] += 1
    for r in faculty:
        k = (r.get("school"), r.get("unit"))
        agg.setdefault(k, {"notice_count": 0, "faculty_count": 0})
        agg[k]["faculty_count"] += 1

    units = [{"school": s, "unit": u, **v} for (s, u), v in agg.items()
             if s and u]
    units.sort(key=lambda x: (-(x["notice_count"] + x["faculty_count"]), x["school"]))

    out = os.path.join(common.OUT_DIR, "units.json")
    json.dump({
        "total": len(units),
        "_note": "论坛 facets 的初始种。unit 仍用自由文本，这只是建议值 —— "
                 "不校正的话「物理学院」和「物理系」会裂成两个版块。",
        "units": units,
    }, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"院系 {len(units)} 个 -> {out}")
    for u in units[:8]:
        print(f"  {u['school']} / {u['unit']:22s} 公告 {u['notice_count']:>3}  导师 {u['faculty_count']:>3}")


if __name__ == "__main__":
    main()

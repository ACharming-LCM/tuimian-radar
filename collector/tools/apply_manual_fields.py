# -*- coding: utf-8 -*-
"""把 `data/out/manual_fields.json`（人工看图核出来的截止日）接进产物。

    cd collector && python tools/apply_manual_fields.py            # 预演
    cd collector && python tools/apply_manual_fields.py --write    # 落盘

## 为什么单独一个工具，而不是塞进 backfill_derived

人工核出来的值**不是算出来的**，它有两个必须留痕的东西：
`manual_status`（读没读、读到什么程度）和 `checked_at`（谁在什么时候看的）。
把它和"规则重算"混在一个脚本里，下次有人跑 backfill 就会把它覆盖掉 ——
人工的两小时不能这么丢。

所以规则是：**`deadline_source="manual"` 的记录，任何自动流程都不许改它的 deadline。**

## 当前状态（2026-10-18）

17 条含扫描版附件的记录里，人工读完的结论是：**没有一条能读出具体报名截止日** ——
它们是名单公示（南大强基、南大拟录取）、培养方案、宣传册、上级红头文件、学信网截图。
所以本工具这次是**空跑**（0 条被写入）。这不是失败：`manual_fields.json` 里
每条都有 status/evidence/pages_read，能证明"看过，不是没看"。

## 幂等

按 `id` 匹配；已经 `deadline_source=manual` 且值相同的记录跳过。
"""
import argparse
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common      # noqa: E402

NOTICES = os.path.join(common.OUT_DIR, "notices.jsonl")
MANUAL = os.path.join(common.OUT_DIR, "manual_fields.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真的落盘（默认只预演）")
    ap.add_argument("--out", default=NOTICES)
    ap.add_argument("--manual", default=MANUAL)
    a = ap.parse_args()

    with open(a.manual, encoding="utf-8") as f:
        manual = json.load(f)
    rows = {r["id"]: r for r in manual["records"]}

    with open(a.out, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]

    applied, kept, conflict = [], [], []
    for r in recs:
        m = rows.get(r.get("id"))
        if not m:
            continue
        if m["deadline"] is None:
            kept.append(r["id"])
            continue
        if r.get("deadline") and r.get("deadline") != m["deadline"] \
                and r.get("deadline_source") != "manual":
            conflict.append((r["id"], r.get("deadline"), m["deadline"]))
            continue
        if r.get("deadline") == m["deadline"] and r.get("deadline_source") == "manual":
            continue
        r["deadline"] = m["deadline"]
        r["deadline_source"] = "manual"
        r["deadline_checked_at"] = m.get("checked_at")
        r["field_state"] = dict(r.get("field_state") or {}, deadline="found")
        applied.append((r["id"], m["deadline"]))

    print(f"人工台账 {len(rows)} 条 / 产物 {len(recs)} 条")
    print(f"  应用 {len(applied)} 条；台账里 deadline=null 的 {len(kept)} 条（保持原状）")
    if conflict:
        print(f"  ⚠️ 冲突 {len(conflict)} 条（产物已有不同的自动值，人工值不覆盖）: {conflict[:3]}")
    for i, d in applied[:10]:
        print(f"    {i} -> {d}")
    print("  台账状态分布:", dict(Counter(r["manual_status"] for r in manual["records"])))

    if not a.write:
        print("\n（预演模式，未写盘。确认无误后加 --write）")
        return 0
    if not applied:
        print("\n没有需要写入的记录（人工台账里的截止日都是 null）。")
        return 0

    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, a.out)
    print(f"\n✅ 已写盘 {a.out}（{len(recs)} 条，人工改动 {len(applied)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

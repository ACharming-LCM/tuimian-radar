# -*- coding: utf-8 -*-
"""从 `notices.jsonl` 重建 `data/out/digest.md`（A22 的配套）。

    cd collector && python tools/rebuild_digest.py --write

`crawl.py` 只在**全量跑**时才重写 digest（分批跑会把它从"全库最新"覆盖成
"最后一所学校最新"，2026-10-18 真发生）。这里按同一套排版，从主库重建一份。

统计行的口径：栏目 = 出现过的 (school, unit, board) 组合数；列表条目 / 详情成功 = 记录数；
失败 = 0（重建看不到上一轮的失败，不编）。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)
sys.path.insert(0, COLLECTOR)

import common          # noqa: E402
import crawl           # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notices", default=os.path.join(common.OUT_DIR, "notices.jsonl"))
    ap.add_argument("--out", default=os.path.join(common.OUT_DIR, "digest.md"))
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    rows = []
    with open(a.notices, encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                rows.append(json.loads(ln))
    rows.sort(key=lambda r: (r.get("publish_date") or ""), reverse=True)

    stat = {
        "boards": len({(r.get("school"), r.get("unit"), r.get("board")) for r in rows}),
        "listed": len(rows),
        "detail_ok": len(rows),
        "detail_fail": 0,
    }
    print(f"记录 {len(rows)} 条 / 栏目 {stat['boards']} 个")
    if not a.write:
        print("（预演，未写盘。加 --write 落盘）")
        return 0
    crawl.write_digest(rows, stat, a.out)
    print(f"✅ 已重建 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

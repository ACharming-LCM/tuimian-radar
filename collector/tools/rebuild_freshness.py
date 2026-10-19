# -*- coding: utf-8 -*-
"""从 `data/out/notices.jsonl` 重建全库 `freshness.json`（A22）。

    cd collector && python tools/rebuild_freshness.py            # 预演
    cd collector && python tools/rebuild_freshness.py --write    # 落盘

## 为什么需要它

`crawl.py` 每次只写**本轮跑过的站点**的新鲜度。2026-10-18 A18 逐校分批跑之后，
最后一批（清华大学）把 `freshness.json` 覆盖成了"只有 2 条清华、records=0" ——
全库 29 所学校 / 2751 条记录的新鲜度视图全丢了。

分批跑本身没错（崩一批只丢一批），错的是**全库级派生文件被按批覆盖**。
所以重建能力必须独立于抓取过程存在：只要有 `notices.jsonl`，就能重建这张表。

## 哪些字段是重建的、哪些是推断的

| 字段 | 来源 |
|---|---|
| `records` | notices 里按 (school, unit) 数出来的，**精确** |
| `last_ok` | 该站点内最大的 `crawled_at`；一个站点一条记录都没有时为 `null`（不假装刚抓过） |
| `boards` | 该站点出现在 notices 里的不同 `board` 名个数（**下限**：只算有产出的栏目） |
| `boards_empty` | 恒 0 —— 重建看不到"空栏目"，这是已知局限 |
| `failed` | 恒 `[]` —— 重建不知道上一轮报了什么错 |
| `status` | `ok`（有记录）/ `no_records`（一条都没有，即"0 产出"） |

**所以重建出来的表不冒充"抓取时刻的事实"**：`last_ok` 用真实抓取时间，
`boards_empty` / `failed` 留空而不是编一个。要看失败原因得去看 `_scratch/batch/<校>.log`。
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)
sys.path.insert(0, COLLECTOR)

import common      # noqa: E402

NOTICES = os.path.join(common.OUT_DIR, "notices.jsonl")
SITES = os.path.join(COLLECTOR, "sites.json")
OUT = os.path.join(common.OUT_DIR, "freshness.json")


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                rows.append(json.loads(ln))
    return rows


def build(rows, sites, generated_at=None):
    """把 notices + sites.json 拼成 freshness 结构。纯函数，方便测试。"""
    by_site = defaultdict(list)
    for r in rows:
        by_site[(r.get("school"), r.get("unit"))].append(r)

    # 配置里所有站点都要有一行 —— 0 条产出的站点不能被"没有记录"悄悄抹掉，
    # 那正是 /api/freshness 存在的意义（哪所学校今天没抓到）。
    order = [(s["school"], s["unit"], s.get("homepage") or "") for s in sites]
    seen = {(s, u) for s, u, _ in order}
    for key in by_site:
        if key not in seen:
            order.append((key[0], key[1], ""))

    fresh = []
    for school, unit, homepage in order:
        recs = by_site.get((school, unit), [])
        last_ok = max((r.get("crawled_at") or "" for r in recs), default="") or None
        boards = len({r.get("board") for r in recs if r.get("board")})
        fresh.append({
            "school": school, "unit": unit, "homepage": homepage,
            "last_ok": last_ok,
            "records": len(recs),
            "boards": boards,
            "boards_empty": 0,
            "failed": [],
            "status": "ok" if recs else "no_records",
            "carried": 0,
            "freshness_hours": 0.0,
        })

    now = generated_at or datetime.now()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "schools": fresh,
        "totals": {
            "records": sum(s["records"] for s in fresh),
            "schools": len({s["school"] for s in fresh}),
            "sites": len(fresh),
            "failed_schools": 0,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notices", default=NOTICES)
    ap.add_argument("--sites", default=SITES)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    rows = load_jsonl(a.notices)
    with open(a.sites, encoding="utf-8") as f:
        cfg = json.load(f)
    built = build(rows, cfg["schools"])

    schools = built["totals"]["schools"]
    sites_n = built["totals"]["sites"]
    zero = [s for s in built["schools"] if s["status"] != "ok"]
    print(f"notices {len(rows)} 条 / {schools} 所学校 / {sites_n} 个站点")
    print(f"  records 合计 {built['totals']['records']}（应等于 {len(rows)}）")
    print(f"  0 产出的站点 {len(zero)} 个：{[(s['school'], s['unit']) for s in zero]}")
    bad = [s for s in built["schools"] if s["records"] and not s["last_ok"]]
    if bad:
        print(f"  ⚠️ 有记录但没有 crawled_at 的站点：{[(s['school'], s['unit']) for s in bad]}")
    if built["totals"]["records"] != len(rows):
        print("  ⚠️ records 合计和 notices 条数对不上 —— 有多余/缺失的 school+unit 归属")

    if not a.write:
        print("\n（预演，未写盘。加 --write 落盘）")
        return 0
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(built, f, ensure_ascii=False, indent=1)
    os.replace(tmp, a.out)
    print(f"\n✅ 已重建 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

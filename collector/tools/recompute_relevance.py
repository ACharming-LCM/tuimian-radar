# -*- coding: utf-8 -*-
"""按当前的 `fields.relevance_of()` 重算 `notices.jsonl` 里每条的 `relevance`。

    cd collector && python tools/recompute_relevance.py            # 预演，只报不改
    cd collector && python tools/recompute_relevance.py --write    # 落盘

## 为什么要有它

`relevance` 是**派生字段**（由 title / board / apply_mode 算出来），
它跑在**抓取时**。所以改了 `relevance_of()` 之后，磁盘上已有的记录**不会自动更新** ——
它们会一直带着旧判据的结果，直到下一次全量抓取。

而全量抓取要真去敲 37 所学校（约 10 分钟）。2026-10-17 这次只是收紧了"宣传材料"
的封顶，**没有任何抓取侧的东西变化**，为它跑一次全量完全不值得。
所以这里离线重算：只改 `relevance` 一个字段，其余原样保留。

## 什么时候该跑

**每次改完 `fields.relevance_of()` 或它依赖的那几个词表，都要跑一次**，
然后重出 fixtures（`tools/make_fixtures.py`）—— 因为
`contracts/fixtures/notices.detail.json` 是**完整记录**，里面带着 `relevance`。

## 安全

- 默认**预演**，`--write` 才落盘
- 落盘走临时文件 + `os.replace`（原子替换），中途崩掉不留半份文件
- 会把"哪几条从几档变成几档"打出来，改判据之后**对着这张表看一遍**再决定
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common      # noqa: E402
import fields      # noqa: E402

NOTICES = os.path.join(common.OUT_DIR, "notices.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真的落盘（默认只预演）")
    ap.add_argument("--out", default=NOTICES)
    args = ap.parse_args()

    with open(args.out, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]

    changed = []
    for r in recs:
        new = fields.relevance_of(r.get("title") or "", r.get("board"),
                                  r.get("apply_mode"))
        old = r.get("relevance")
        if new != old:
            changed.append((old, new, r.get("school"), r.get("title") or ""))
        r["relevance"] = new

    from collections import Counter
    dist = Counter(r["relevance"] for r in recs)
    print(f"记录 {len(recs)} 条：2 档 {dist.get(2, 0)} / 1 档 {dist.get(1, 0)} / "
          f"0 档 {dist.get(0, 0)}")
    print(f"变动 {len(changed)} 条：")
    for old, new, school, title in changed:
        print(f"   {old} → {new}  {school} | {title[:58]}")

    # 反向自检：**只该降档，不该升档**。
    # 收紧判据时出现升档，说明改错了方向（或者顺带放宽了别的分支）。
    ups = [c for c in changed if c[1] > c[0]]
    if ups:
        print(f"\n⚠️  有 {len(ups)} 条**升档**了（{ups[:3]}）—— 收紧判据却出现升档，"
              f"先确认这是预期内的再落盘")

    if not args.write:
        print("\n（预演模式，未写盘。确认无误后加 --write）")
        return 0

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, args.out)
    print(f"\n✅ 已写盘 {args.out}")
    print("   别忘了：python tools/make_fixtures.py --as-of <今天>，"
          "否则 notices.detail.json 里的 relevance 还是旧的")
    return 0


if __name__ == "__main__":
    sys.exit(main())

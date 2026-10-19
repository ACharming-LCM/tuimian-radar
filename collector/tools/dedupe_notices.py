# -*- coding: utf-8 -*-
"""同校同标题去重（A30）。

    cd collector && python tools/dedupe_notices.py            # 预演
    cd collector && python tools/dedupe_notices.py --write    # 落盘

## 背景

全库 118 组 / 305 条记录 `(school, title)` 完全相同（占 11.1%），首屏就出现了
一字不差的两张卡片。A23 查过：**没有一组是同一个 URL** —— 去重键（canonical URL）
没坏，坏的是"同一份公告挂了多个 URL"（分册、多栏目、不同年份的同一标题）。

## 保留规则

- **URL 相同**（理论上不会出现，A23 实测 0 组）→ 保留 `body_len` 最大的；
- **URL 不同** → 保留**最新发布**的那条；同日期时取 `body_len` 大的。
- 落选的记录**不删除**：追加 `duplicate_of`（保留下来的那条 id）后移到
  `data/out/notices-duplicates.jsonl`，可追溯、可复查。
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)
sys.path.insert(0, COLLECTOR)

import common      # noqa: E402

NOTICES = os.path.join(common.OUT_DIR, "notices.jsonl")
DUPES = os.path.join(common.OUT_DIR, "notices-duplicates.jsonl")


def norm_title(t):
    return re.sub(r"\s+", "", (t or "")).strip()


def _score(r):
    """越大越该保留：新发布的优先，其次正文长的。"""
    return ((r.get("publish_date") or ""), len(r.get("body_text") or ""))


def dedupe(rows):
    """返回 (保留的记录, 被折叠的记录[带 duplicate_of])。"""
    groups = defaultdict(list)
    for r in rows:
        key = (r.get("school") or "", norm_title(r.get("title")))
        if not key[1]:                 # 空标题不参与去重（分组没有意义）
            key = (key[0], "__no_title__" + (r.get("id") or ""))
        groups[key].append(r)

    keep_ids = set()
    folded = []
    for key, recs in groups.items():
        if len(recs) == 1:
            keep_ids.add(id(recs[0]))
            continue
        urls = {common.canonical_url(r.get("url") or "") for r in recs}
        if len(urls) == 1:
            winner = max(recs, key=lambda r: len(r.get("body_text") or ""))
            reason = "same_url_longest_body"
        else:
            winner = max(recs, key=_score)
            reason = "different_url_newest"
        keep_ids.add(id(winner))
        for r in recs:
            if r is winner:
                continue
            d = dict(r)
            d["duplicate_of"] = winner.get("id")
            d["duplicate_reason"] = reason
            folded.append(d)

    kept = [r for r in rows if id(r) in keep_ids]
    return kept, folded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notices", default=NOTICES)
    ap.add_argument("--dupes", default=DUPES)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    with open(a.notices, encoding="utf-8") as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]

    kept, folded = dedupe(rows)
    # 落选记录按"保留下来的那条"排序，方便人看
    folded.sort(key=lambda r: (r.get("school") or "", r.get("duplicate_of") or ""))

    print(f"输入 {len(rows)} 条 → 保留 {len(kept)} 条 / 折叠 {len(folded)} 条")
    groups = len(rows) - len(kept)
    print(f"  被折叠的记录来自 {groups} 个重复组（同校同标题）")
    if folded:
        print("  样例：")
        for r in folded[:5]:
            print(f"    {r['school']} | {(r.get('title') or '')[:28]} → keep {r['duplicate_of']}"
                  f" ({r['duplicate_reason']})")

    if not a.write:
        print("\n（预演，未写盘。加 --write 落盘）")
        return 0

    # 主库原子写（A27 的规矩）
    tmp = a.notices + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, a.notices)

    tmp = a.dupes + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in folded:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, a.dupes)

    print(f"\n✅ 主库 {a.notices}（{len(kept)} 条）")
    print(f"✅ 折叠档 {a.dupes}（{len(folded)} 条，带 duplicate_of）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

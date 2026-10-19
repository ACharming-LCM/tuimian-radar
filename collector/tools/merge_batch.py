# -*- coding: utf-8 -*-
"""把一批抓取结果合并进主文件（A18 分批跑用）。

    python tools/merge_batch.py _scratch/batch/北京大学.jsonl
    python tools/merge_batch.py _scratch/batch/北京大学.jsonl --write

为什么需要它：`crawl.py --school X` 只会产出 X 的记录（其它学校不在本轮 `sites` 里，
`retain_unrefreshed` 也不会带它们）。所以分批跑必须"每批合并进主文件"，
否则跑第二批就把第一批覆盖掉了。

合并规则：按 canonical URL 去重，**批次里的记录优先**（它是刚抓的）。
主文件里其它学校的记录原样保留 —— 这就是"崩一批只丢一批"的落点。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common      # noqa: E402

NOTICES = os.path.join(common.OUT_DIR, "notices.jsonl")


def load(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("batch")
    ap.add_argument("--out", default=NOTICES)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    master = load(a.out)
    batch = load(a.batch)
    by_key = {common.canonical_url(r.get("url") or ""): r for r in master}
    before = len(by_key)
    added = replaced = 0
    for r in batch:
        k = common.canonical_url(r.get("url") or "")
        if not k:
            continue
        if k in by_key:
            if json.dumps(by_key[k], sort_keys=True) != json.dumps(r, sort_keys=True):
                replaced += 1
            by_key[k] = r
        else:
            added += 1
            by_key[k] = r

    recs = sorted(by_key.values(), key=lambda r: (r.get("publish_date") or ""), reverse=True)
    schools = sorted({(r.get("school") or "") for r in batch})
    print(f"批次 {os.path.basename(a.batch)}：{len(batch)} 条（{','.join(schools)}）")
    print(f"  主文件 {before} → {len(recs)}（新增 {added} / 更新 {replaced}）")

    if not a.write:
        print("（预演，未写盘。加 --write 落盘）")
        return 0
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, a.out)
    print(f"✅ 已合并到 {a.out}（{len(recs)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

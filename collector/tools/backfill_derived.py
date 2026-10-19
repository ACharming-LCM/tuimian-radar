# -*- coding: utf-8 -*-
"""离线补派生字段：`record_quality` / `quality_hits`（A1），以及后续的 topic、时效字段。

    cd collector && python tools/backfill_derived.py            # 预演
    cd collector && python tools/backfill_derived.py --write    # 落盘

## 为什么不用重跑 crawl

这些字段**完全由记录自己的标题/正文决定**，不依赖网络。重跑一次要 17 分钟、
还会把"字段计算"和"数据是否变新"两件事混在一起。仓库里已有
`backfill_field_state.py` / `backfill_attachment_extract.py` 两个先例，这里沿用同一套
"读产物 → 用同一个函数重算 → 原子写回"的做法。

## 关键约束

- **只用 `quality.py` 里那一个 `classify()`**。下游和回填必须同源，否则会出现
  "爬虫判 junk、回填判 ok" 两套真相。
- 预演是默认行为；`--write` 才落盘，且用临时文件 + `os.replace` 原子替换。
- 打印三档分布和每档的判据命中数 —— 这就是验收数字。
"""
import argparse
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common      # noqa: E402
import fields      # noqa: E402
import kind        # noqa: E402
import quality     # noqa: E402
import timeliness  # noqa: E402
import topic       # noqa: E402

NOTICES = os.path.join(common.OUT_DIR, "notices.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真的落盘（默认只预演）")
    ap.add_argument("--out", default=NOTICES)
    a = ap.parse_args()

    with open(a.out, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]

    before = Counter(r.get("record_quality") for r in recs)
    dist, hits = Counter(), Counter()
    topic_dist, topic_hits = Counter(), Counter()
    kind_dist = Counter()
    dated_before = sum(1 for r in recs if (r.get("publish_date") or "").strip())
    recovered = Counter()
    recovered_titles = 0
    relevance_fixed = 0
    retitled = 0
    for r in recs:
        q, h = quality.classify(r)
        r["record_quality"], r["quality_hits"] = q, h
        r["title_source"] = r.get("title_source") or "detail"
        # 索引页（A14）不是"标题坏了"，是"这一页根本不是公告"—— 不能靠回退标题救它。
        if q == "junk" and not any(x.startswith("body_is_index") for x in h):
            fixed, src = quality.title_fallback(r)
            r["title_source"] = src
            if fixed:
                r["title"] = fixed
                r["record_quality"], r["quality_hits"] = quality.classify(r)
                recovered_titles += 1
        # 标题只是机构名（issues/011）：先用 title_from_body 从缓存页 <title> / 正文
        # 捞真标题；捞到就换上并重算质量（很可能从 junk 变回 ok）。
        # 捞不到才保持 junk（原因写在 quality_hits 里，如 title_is_org_only / body_nav_list）。
        new_title = quality.title_from_body(r)
        if new_title:
            r["title"] = new_title
            r["title_source"] = "fallback_body"
            r["record_quality"], r["quality_hits"] = quality.classify(r)
            retitled += 1
        dist[r["record_quality"]] += 1
        for x in r["quality_hits"]:
            hits[x] += 1
        t, th = topic.classify(r)
        r["topic"], r["topic_hits"] = t, th
        topic_dist[t] += 1
        for x in th:
            topic_hits[x] += 1
        # notice_kind（A29）**必须在 topic 之后**：它只在 apply 上分四档。
        k, kh = kind.classify(r)
        r["notice_kind"], r["kind_hits"] = k, kh
        kind_dist[k] += 1
        pub, src = timeliness.recover_publish_date(r)
        if pub and not (r.get("publish_date") or "").strip():
            r["publish_date"], r["publish_date_source"] = pub, src
            recovered[src] += 1
        r.update(timeliness.time_fields(r))
        # ---- relevance：**必须放在最后**，并且用**刷新后的** title/board/apply_mode 算 ----
        #
        # 坑（已踩，B 报的 issues/010）：A6 把 38 条标题从"天津大学研究生招生网"改成真标题，
        # 但 relevance 还是按旧标题算的 2 分。而 relevance 是首屏**第一排序键**，
        # 于是这些记录排在错误位置，门禁 188/1。
        # 根因不是"忘了一处"，是**派生字段没有统一刷新这一环**：
        # 只要标题/栏目/正文能被后处理改掉，所有从它们派生出来的字段就必须跟着重算。
        old_rel = r.get("relevance")
        new_rel = fields.relevance_of(r.get("title"), r.get("board"), r.get("apply_mode"))
        if old_rel != new_rel:
            r["relevance"] = new_rel
            relevance_fixed += 1
        # ---- 时效分层（A24）：live / recent / archive ----
        # 必须在 time_fields 之后算（要用刷新后的 age_days / publish_date）。
        # **字段名是 `age_tier` 不是 `tier`**：`tier` 已经被推送层占了（1/2 匹配档），
        # 同一个名字存两种含义，读的人分不清 —— 2026-10-18 改的名。
        r["age_tier"] = timeliness.tier_of(r)
        r.pop("tier", None)

    dated_after = sum(1 for r in recs if (r.get("publish_date") or "").strip())
    print(f"记录 {len(recs)} 条")
    print(f"  机构名标题补成真标题 {retitled} 条")
    print(f"  relevance 修正 {relevance_fixed} 条")
    print(f"  有发布日: {dated_before} → {dated_after}（补回 {dated_after - dated_before}，"
          f"来源 {dict(recovered)}）")
    fresh = Counter(str(r.get("is_fresh")) for r in recs)
    print(f"  is_fresh 分布: {dict(fresh)}")
    print(f"  record_quality: {dict(dist)}（标题回退救回 {recovered_titles} 条）")
    tier_dist = Counter(r.get("age_tier") for r in recs)
    print(f"  age_tier 分布: {dict(tier_dist)}")
    print(f"  notice_kind 分布（全库）: {dict(kind_dist)}")
    apply_rows = [r for r in recs
                  if r.get("topic") == "apply" and r.get("record_quality") != "junk"]
    print(f"  notice_kind 分布（apply 且非 junk，{len(apply_rows)} 条）: "
          f"{dict(Counter(r.get('notice_kind') for r in apply_rows))}")
    print(f"  判据命中: {dict(hits)}")
    print(f"  topic（含 junk，全部记录）: {dict(topic_dist)}")
    clean = Counter(r["topic"] for r in recs if r["record_quality"] != "junk")
    print(f"  topic（剔除 junk，{sum(clean.values())} 条）: {dict(clean)}")
    print(f"  topic_hits 前 10: {topic_hits.most_common(10)}")
    ay = Counter(r.get("admission_year") for r in recs if r.get("admission_year"))
    print(f"  admission_year 分布: {dict(sorted(ay.items()))}")
    k = [r for r in recs
         if r.get("admission_year") == 2027 and r.get("topic") == "apply"]
    print(f"  admission_year=2027 且 topic=apply: {len(k)} 条"
          f"（其中 quality=ok {sum(1 for r in k if r['record_quality'] == 'ok')} 条、"
          f"is_fresh=true {sum(1 for r in k if r.get('is_fresh') is True)} 条）")

    junk = [r for r in recs if r["record_quality"] == "junk"]
    print(f"\njunk {len(junk)} 条，前 10 条：")
    for r in junk[:10]:
        print(f"  {r['id']} [{','.join(r['quality_hits'])}] "
              f"{r['school']} | {(r.get('title') or '')[:28]} | "
              f"body={len(r.get('body_text') or '')}")

    # 自检：正文非空却被判 junk 的，必须在 hits 里说得出理由
    bad = [r for r in junk if (r.get("body_text") or "").strip() and not r["quality_hits"]]
    print(f"\n自检：junk 但无判据理由 = {len(bad)} 条（必须为 0）")

    if not a.write:
        print("\n（预演模式，未写盘。确认无误后加 --write）")
        return 0

    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, a.out)
    print(f"\n✅ 已写盘 {a.out}（{len(recs)} 条）")

    # 主库被换掉了，就**立刻重建 freshness**（A22/A24 的教训）：
    # 门禁有一条"freshness 不许比 notices.jsonl 旧超过 300 秒"，
    # 改完数据忘了刷 freshness 会直接红，而且报错看起来像"新鲜度坏了"。
    # rebuild_freshness 只读主库、不抓网，几毫秒，没有理由不顺手做掉。
    if os.path.abspath(a.out) == os.path.abspath(common.NOTICES if hasattr(common, "NOTICES")
                                                 else os.path.join(common.OUT_DIR, "notices.jsonl")):
        try:
            import rebuild_freshness
            sites = json.load(open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "sites.json"), encoding="utf-8"))["schools"]
            built = rebuild_freshness.build(recs, sites)
            fpath = os.path.join(common.OUT_DIR, "freshness.json")
            with open(fpath + ".tmp", "w", encoding="utf-8") as f:
                json.dump(built, f, ensure_ascii=False, indent=1)
            os.replace(fpath + ".tmp", fpath)
            print(f"新鲜度已同步重建：{built['totals']['records']} 条 / "
                  f"{built['totals']['schools']} 校 / {built['totals']['sites']} 站")
        except Exception as e:                       # noqa: BLE001
            print(f"⚠️ freshness 重建失败（门禁可能会报'过期快照'）：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

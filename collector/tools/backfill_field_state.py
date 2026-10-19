# -*- coding: utf-8 -*-
"""离线回填 `body_how` / `field_state` —— **不联网、不新增抓取**。

    cd collector && python tools/backfill_field_state.py            # 预演，只报不改
    cd collector && python tools/backfill_field_state.py --write    # 落盘

## 为什么需要它，而不是重跑一次 crawl

`run_daily.py` 是带 `--refresh` 的 —— 也就是说"重跑一次全量"**会真的去敲 37 所学校的
服务器**（实测跑了 8 分钟、新写了 50 多个缓存文件、`notices.jsonl` 都还没动）。
而这次要补的两个字段（`body_how` / `field_state`）**完全由磁盘上已有的东西决定**：
`data/raw/<sha1(url)>.html` 里存着原始的详情页 HTML，`data/raw/files/` 里存着附件。
没有任何理由为此再去打扰学校站点。

所以走回填：**用同一套解析函数重放一遍**，算出新字段，只把它们并回去。
关键约束是**逐字复现** —— 回填出来的 `field_state` 必须和"真跑一次 crawl"
会得到的完全一样，否则这份数据就有了两种真相。

## 怎么保证一致（三条，缺一条就会悄悄分叉）

1. **不重抄任何判据**：调用 `detail.parse_detail` / `fields.extract_fields` /
   `fields.state_for_record` —— 也就是 crawl.py 自己调的那几个函数。
2. **正文用回放出来的那份，不用产物里的**：产物的 `body_text` 已经混进了附件正文
   （`pdf.enrich` 追加的「【附件正文：…】」），拿它量"选择器取到的正文有多长"会量错。
   实测 190/190 条可以从缓存逐字复现出**未合并附件前**的正文。
3. **落盘前先对账**：回放抽出来的 `deadline` / `majors` 必须和产物里已有的一致；
   正文必须真的能拼回产物那份。对不上就**中止并报出来**，不猜、不覆盖。
   这条是防"我以为我在重放，其实我在改数据"。
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common      # noqa: E402
import crawl       # noqa: E402  只取 EXTRACT_VERSION —— 版本号只能有一个来源
import detail      # noqa: E402
import fields      # noqa: E402
import pdf         # noqa: E402

NOTICES = os.path.join(common.OUT_DIR, "notices.jsonl")
FILE_EXTS = (".pdf", ".docx")


def base_body_of(rec):
    """回放一条记录的**附件合并之前**的正文，并给出它的来源。

    返回 (body_how, body_text, pdf_stats) —— 三者就是 `state_for_record` 要的输入。
    `pdf_stats` 对网页记录从附件的 `note` 反推（见 `fields._scanned_from_record`），
    对文件型记录要真的解一遍那份文件才能知道它是不是扫描版。
    """
    url = rec.get("url") or ""

    # 文件型记录：整条公告本身就是一个 PDF/DOCX，正文从那份文件里抽出来。
    # 不能走 HTML 解析（实测 16 个"详情解析失败"里有 5 个是这一类）。
    if os.path.splitext(url.split("?")[0])[1].lower() in FILE_EXTS:
        raw, _ = common.fetch_binary(url)          # 走本地缓存，不联网
        if not raw:
            return None
        text, note, _pages = pdf.extract(raw, url)
        scanned = 1 if (note and "扫描版" in note) else 0
        return "file", text, {"scanned": scanned}

    p = common.cache_path(url)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        d = detail.parse_detail(f.read(), url)
    if d is None:
        return None
    return d.get("body_how"), (d.get("body_text") or ""), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真的落盘（默认只预演）")
    ap.add_argument("--out", default=NOTICES)
    args = ap.parse_args()

    with open(args.out, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]

    filled, skipped, mismatch = [], [], []
    for r in recs:
        got = base_body_of(r)
        if got is None:
            skipped.append((r["id"], "无缓存可回放"))
            continue
        how, base, pdf_stats = got
        stored = r.get("body_text") or ""

        # ---- 对账：缓存里的正文必须是产物正文的**前缀** ----
        #
        # 产物的 `body_text` 有两种形态：① 就是 HTML 正文；② HTML 正文后面拼了
        # `pdf.enrich` 追加的附件正文（「【附件正文：…】」）。所以"前缀"是正确的关系。
        #
        # **这里第一版写错过，值得记下来**：我原来要求"产物 = 回放正文 + 我自己按
        # 《附件正文：》拼出来的那串"，于是 28 条被判成对不上 —— 而它们其实没问题：
        # 分隔符不止一种写法。**校验条件本身写错，会把好数据报成坏数据**；
        # 所以判据要挑**真正的不变量**（前缀关系），不要挑"我复现出的那种具体拼法"。
        if not stored.startswith(base):
            mismatch.append((r["id"], f"缓存正文不是产物的前缀（缓存 {len(base)} / 产物 {len(stored)}）"))
            continue

        # ---- 对账：我重放的确实是**同一份正文** ----
        #
        # 拿**产物自己那份**正文抽一遍字段，必须和产物里存的一致。
        # 这一条才是"我理解这条记录"的证明；上一条只证明"缓存没跑偏"。
        # （不能用只含 HTML 的那半截去比 —— 附件里抽出来的字段本来就该在那儿。）
        publish = r.get("publish_date") or ""
        ex = fields.extract_fields(r.get("title") or "", stored, publish=publish)
        for k in ("deadline", "majors", "apply_mode", "degree_types"):
            if ex[k] != r.get(k):
                mismatch.append((r["id"], f"{k} 回放={ex[k]!r} 产物={r.get(k)!r}"))
                break
        else:
            r["body_how"] = how
            # 抽取器版本跟着 crawl.py 一起升（**只有一个来源**，见 crawl.EXTRACT_VERSION）。
            # 版本变了，下一轮 diff 会把这次的字段变化归成 `reparsed`（我们改了代码），
            # 而不是 `changed`（学校改了通知）—— 后者会污染用户提醒。
            r["extract"] = dict(r.get("extract") or {}, version=crawl.EXTRACT_VERSION)
            # `field_state` 仍然按**附件合并之前**的正文算 —— 这正是它要判的东西
            # （"选择器取到的正文够不够格、有没有框错元素"）。
            # 合并后的长度会被附件字数撑上去，用它量会把这个判据整个废掉。
            r["field_state"] = fields.state_for_record(
                r, body=base, body_len=len(base), pdf_stats=pdf_stats)
            filled.append(r)

    print(f"记录 {len(recs)} 条：回填 {len(filled)} / 无缓存跳过 {len(skipped)} / "
          f"**对不上 {len(mismatch)}**")
    for i, why in skipped[:5]:
        print(f"  跳过 {i}：{why}")
    for m in mismatch[:10]:
        print(f"  ❌ 对不上 {m}")

    if mismatch:
        print("\n❌ 有记录对不上 —— **不写盘**。"
              "先查清为什么回放和产物不一致，别猜。")
        return 1

    import collections
    c = collections.Counter((r.get("field_state") or {}).get("body") for r in filled)
    d = collections.Counter((r.get("field_state") or {}).get("deadline") for r in filled)
    print(f"  body        : {dict(c)}")
    print(f"  deadline    : {dict(d)}")
    absent_ok = sum(1 for r in filled
                    if (r.get("field_state") or {}).get("body") == "ok")
    print(f"  敢判 absent 的正文（body=ok）：{absent_ok}/{len(filled)}")

    if not args.write:
        print("\n（预演模式，未写盘。确认无误后加 --write）")
        return 0

    # 先算完再落盘 —— 顺序反了的话，中途出错就会留下一份半成品
    # （crawl.py 里那条"崩溃发生在写文件之前，所以已有数据没被破坏"的教训）。
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in filled:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, args.out)
    print(f"\n✅ 已写盘 {args.out}（{len(filled)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

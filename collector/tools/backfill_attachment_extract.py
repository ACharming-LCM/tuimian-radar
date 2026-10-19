# -*- coding: utf-8 -*-
"""离线重抽附件正文并重算字段 —— **不联网、不新增抓取**。

    cd collector && python tools/backfill_attachment_extract.py            # 预演
    cd collector && python tools/backfill_attachment_extract.py --write    # 落盘

## 为什么需要它

`pdf.py` 这回改的是"附件正文怎么拼接"（合并软换行），`fields.py` 改的是"截止日
怎么认"（开放时间/第N轮报名/全角连接号/带时分的区间）。这两件事要生效，必须
**把已有附件的正文重新抽一遍**。

重跑 `crawl.py --refresh` 当然可以，但它会真的去敲 37 所学校——而这次改动
**完全由磁盘上已有的字节决定**：`data/raw/files/<sha1(url)>.<ext>` 里就是那些附件。
没有理由为了一次纯离线规则调整去打扰学校站点（WAF 预算也是预算）。

所以走回填：用 crawl.py 自己调的那几个函数（`pdf.enrich` / `fields.extract_fields` /
`fields.state_for_record`）重放一遍，只把**由规则决定的部分**更新回记录。

## 记录是怎么重算的

1. 用 `backfill_field_state.base_body_of()` 拿回**附件合并之前**的正文（`base`）。
2. `body_text = base`，再跑 `pdf.enrich()` —— 附件全部命中本地缓存；
   `text_len` / `note` / `parsed` 以及 `【附件正文：…】` 拼接结果按新代码重算。
3. `fields.extract_fields(title, body_text, publish)` 重算全部字段。
4. `fields.state_for_record(..., body=base)` 重算三态 —— 判据看的是**合并前**正文。
5. `content_hash` 跟着 `body_text` 重算。

文件型记录（整条公告就是一个 PDF/DOCX）不走 `enrich`（crawl 里也是这样：
`build_file_record` 传 `attachments=False`），直接更新它那条附件的 `text_len`。

## 安全阀

- 预演是默认行为，`--write` 才落盘；写盘用临时文件 + `os.replace`。
- 缓存里没有正文的记录**原样保留**并报数，不猜、不删。
- 结尾打印 deadline 三态的变化，以及指定 id 的前后值 —— 这就是验收数字。
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common      # noqa: E402
import crawl       # noqa: E402  版本号只有一个来源
import fields      # noqa: E402
import pdf         # noqa: E402
from tools.backfill_field_state import base_body_of   # noqa: E402

NOTICES = os.path.join(common.OUT_DIR, "notices.jsonl")
FILE_EXTS = (".pdf", ".docx")
WATCH = ("u57dcf3ee-47863-845753", "u57dcf3ee-47868-844955", "ud1ddf924-12939-789932",
         "uf44a71c0-17352-399886", "u821fddd7-f9945c0975c2551e")


def reextract(rec):
    """重算一条记录。返回 (rec, pdf_stats) 或 (None, 原因)。"""
    got = base_body_of(rec)
    if got is None:
        return None, "无缓存可回放"
    how, base, _ = got
    url = rec.get("url") or ""
    ext = os.path.splitext(url.split("?")[0])[1].lower()

    rec["body_how"] = how
    if ext in FILE_EXTS:
        # 文件型：base 就是那份文件抽出来的正文，附件列表里只有它自己。
        rec["body_text"] = base
        pdf_stats = {"tried": 1, "ok": 1 if base else 0,
                     "scanned": 1 if not base else 0, "failed": 0, "chars": len(base)}
        for a in rec.get("attachments") or []:
            if os.path.splitext((a.get("url") or "").split("?")[0])[1].lower() in FILE_EXTS:
                a["text_len"] = len(base)
                a["parsed"] = bool(base)
                break
    else:
        rec["body_text"] = base
        pdf_stats = pdf.enrich(rec, refresh=False, verbose=False)

    rec["body_len"] = len(rec["body_text"])
    ex = fields.extract_fields(rec.get("title") or "", rec["body_text"],
                               publish=rec.get("publish_date") or "")
    rec.update({
        "deadline": ex["deadline"], "target_year": ex["target_year"],
        "degree_types": ex["degree_types"], "apply_mode": ex["apply_mode"],
        "majors": ex["majors"], "materials": ex["materials"],
        "contact": ex["contact"], "tags": ex["tags"],
        "extract": dict(rec.get("extract") or {}, method="rule",
                        version=crawl.EXTRACT_VERSION, confidence=ex["confidence"]),
        "content_hash": "sha1:" + hashlib.sha1(rec["body_text"].encode("utf-8")).hexdigest(),
        "relevance": fields.relevance_of(rec.get("title"), rec.get("board"),
                                         ex["apply_mode"]),
    })
    rec["field_state"] = fields.state_for_record(
        rec, body=base, body_len=len(base), pdf_stats=pdf_stats)
    return rec, pdf_stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真的落盘（默认只预演）")
    ap.add_argument("--out", default=NOTICES)
    args = ap.parse_args()

    with open(args.out, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]

    before = {r["id"]: r for r in recs}
    # **先抄一份状态再重抽**：reextract 是原地改 dict 的，事后读 before 会读到新值，
    # 那样"修复前"就永远是假数字（第一次跑就踩了这个坑）。
    snap = {r["id"]: ((r.get("field_state") or {}).get("deadline"), r.get("deadline"),
                      r.get("body_len"), len(r.get("body_text") or "")) for r in recs}
    out, skipped = [], []
    for r in recs:
        new, why = reextract(r)
        if new is None:
            skipped.append((r["id"], why))
            out.append(r)
        else:
            out.append(new)

    dl_before = Counter(v[0] for v in snap.values())
    dl_after = Counter((r.get("field_state") or {}).get("deadline") for r in out)
    flipped = [(i, snap[i][1], r.get("deadline"))
               for r in out for i in [r["id"]]
               if snap[i][0] == "unknown"
               and (r.get("field_state") or {}).get("deadline") == "found"]

    print(f"记录 {len(out)} 条：重抽 {len(out) - len(skipped)} / 无缓存保留 {len(skipped)}")
    for i, why in skipped[:5]:
        print(f"  跳过 {i}：{why}")
    print(f"  deadline 三态  前 {dict(dl_before)}")
    print(f"                 后 {dict(dl_after)}")
    print(f"  unknown 减少   {dl_before['unknown'] - dl_after['unknown']}")
    print(f"  unknown → found {len(flipped)} 条")
    for i, old, new in flipped[:15]:
        print(f"    {i}  {old!r} → {new!r}")

    print("\n指定 id：")
    after = {r["id"]: r for r in out}
    for i in WATCH:
        if i not in after:
            print(f"  {i}  不在本次产物里")
            continue
        b, a = before[i], after[i]
        print(f"  {i}  {snap[i][1]!r} → {a.get('deadline')!r}    "
              f"state {snap[i][0]} → {(a.get('field_state') or {}).get('deadline')}")

    if not args.write:
        print("\n（预演模式，未写盘。确认无误后加 --write）")
        return 0

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, args.out)
    print(f"\n✅ 已写盘 {args.out}（{len(out)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

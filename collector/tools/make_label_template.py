# -*- coding: utf-8 -*-
"""给远程伙伴生成"人工标注模板" —— 他们只需要填表，不需要理解我们的代码。

为什么需要这个：我实测过**关键词规则做不了"这条公告跟招生有关吗"这个判断**
（两个方向都错，见 README）。而这件事**只有人能可靠地做**。
所以把 78 条摊成一张表，让人 20 分钟标完，我才能测出规则的真实精度。

用法：python tools/make_label_template.py
产出：data/out/label-template.tsv（UTF-8，制表符分隔，Excel 直接能开）
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common        # noqa: E402

HEADER = ["id", "school", "unit", "publish_date", "title", "url",
          # ↓↓↓ 下面三列是给伙伴填的，模板里留空
          "admission_related(yes/no/unsure)", "deadline_correct(yes/no/na)",
          "note"]


def main():
    src = os.path.join(common.OUT_DIR, "notices.jsonl")
    with open(src, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]
    recs.sort(key=lambda r: (r.get("school") or "", r.get("publish_date") or ""))

    out = os.path.join(common.OUT_DIR, "label-template.tsv")
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write("\t".join(HEADER) + "\n")
        for r in recs:
            row = [
                r["id"], r.get("school") or "", r.get("unit") or "",
                r.get("publish_date") or "",
                # 标题里的制表符会毁掉 tsv 结构，必须清掉
                (r.get("title") or "").replace("\t", " ").replace("\n", " "),
                r.get("url") or "",
                "", "", "",          # 三列留空给伙伴填
            ]
            f.write("\t".join(row) + "\n")

    # 单独出一份"带 deadline 的子集"给精度抽查用（那才是更值钱的测量）
    dl = [r for r in recs if r.get("deadline")]
    out2 = os.path.join(common.OUT_DIR, "label-deadline-sample.tsv")
    with open(out2, "w", encoding="utf-8", newline="") as f:
        f.write("\t".join(["id", "school", "unit", "title", "url",
                           "machine_deadline", "correct_date_or_blank",
                           "note"]) + "\n")
        for r in dl:
            f.write("\t".join([
                r["id"], r.get("school") or "", r.get("unit") or "",
                (r.get("title") or "").replace("\t", " ").replace("\n", " "),
                r.get("url") or "", r["deadline"], "", "",
            ]) + "\n")

    print(f"全部 {len(recs)} 条 -> {out}")
    print(f"其中有截止日的 {len(dl)} 条 -> {out2}（这是精度抽查，比覆盖率更值钱）")
    print("\n给伙伴的话：")
    print("  · 打开 tsv（Excel 直接双击），只填最后三列，别动前面的列")
    print("  · admission_related 填 yes / no / unsure —— **拿不准就填 unsure**，")
    print("    不要猜。我需要的是你的真实判断，不是一致的答案")
    print("  · deadline_correct 只对'有截止日'那份填：机器抽的日期对不对")


if __name__ == "__main__":
    main()

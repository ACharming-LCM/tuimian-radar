# -*- coding: utf-8 -*-
"""评估导师解析器的**泛化能力**：同一套解析器能覆盖几所学校的师资页。

**为什么必须量这个**：导师解析器是照着浙江大学物理学院那一套调出来的
（`li.list-item` + `h3` + 带标签的 `p.over`）。而浙大的师资页有个特殊性：
它统一挂在 `person.zju.edu.cn` 上，全校园一致的模板。
**别的学校没有这个系统。**

所以现在的 50 位导师到底是"一个能力"还是"一个样本"，必须测出来 ——
不测就写进 PPT，等于把样本当能力卖。这个教训今晚已经吃过两次：
"加学校=加一行"是假的（真实覆盖率 74%→ 栏目精度 31%），
"栏目精度 60%"是错的（实测 31%）。

**度量方法（不靠人工判断）**：
  分母 = 页面上 `//h3` 的数量（一张人员卡片基本就是一个 h3 标题）
  分子 = 解析器抽出的人数
  回收率 = 分子 / 分母 → 反映"结构对不上"的程度
另外统计研究方向/邮箱覆盖率 —— 抽到人但没方向，对匹配没用。

用法（在 collector 目录下）：
    python tools/eval_faculty.py
    python tools/eval_faculty.py --refresh

归属：A。产出 data/out/faculty-eval.md
"""
import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common         # noqa: E402
import faculty as FAC  # noqa: E402

# 候选院系。域名全部来自**真实数据**（data/out/notices.jsonl 里出现过的 host）
# 或已探测可达的站点，不是猜的 —— 猜域名的失败率实测约 46%。
CANDIDATES = [
    {"school": "浙江大学", "school_slug": "zju", "unit": "物理学院",
     "homepage": "https://physics.zju.edu.cn/", "baseline": True},
    {"school": "浙江大学", "school_slug": "zju", "unit": "材料科学与工程学院",
     "homepage": "https://mse.zju.edu.cn/"},
    {"school": "北京大学", "school_slug": "pku", "unit": "物理学院",
     "homepage": "https://www.phy.pku.edu.cn/"},
    {"school": "北京大学", "school_slug": "pku", "unit": "化学与分子工程学院",
     "homepage": "https://www.chem.pku.edu.cn/"},
    {"school": "中国科学技术大学", "school_slug": "ustc", "unit": "物理学院",
     "homepage": "https://physics.ustc.edu.cn/"},
    {"school": "复旦大学", "school_slug": "fudan", "unit": "物理学系",
     "homepage": "https://phys.fudan.edu.cn/"},
]


def verdict(res):
    """判定只依据**可信的两个绝对量**：抽出人数、其中有研究方向的比例。

    坑（已踩，两次）：我一开始想算「回收率 = 抽出人数 / 页面上疑似人数」，
    结果两种分母都不成立：
      · 用 `//h3` 数 → 很多学校的人员列表是 `<a>张三</a>`，不用标题标签，分母塌成 0；
      · 改用"姓名样式的链接" → `NAME_RE` 只认 2-4 个汉字，
        **「本院简介」「办公指南」「学术活动」这些导航词全被算成人**，
        浙大物理那一页明明只有 6 个人，分母却数出 38，回收率显示成 16%。
    **一个自己不可信的指标比没有指标更危险 —— 它会制造假结论。**
    所以这里只报绝对量，不报比率。
    """
    rows = [r for r in res["pages"] if "error" not in r]
    got = sum(r["parsed"] for r in rows)
    with_res = sum(r["with_research"] for r in rows)
    if got >= 10 and with_res / got >= 0.5:
        return "✅ 可用", got, with_res
    if got >= 5:
        return "⚠️ 部分可用", got, with_res
    return "❌ 不可用", got, with_res


def evaluate_unit(cand, refresh=False, max_pages=4):
    pages = FAC.discover_faculty_pages(cand["homepage"])
    rows = []
    for pg in pages[:max_pages]:
        raw, _ = common.fetch(pg["url"], refresh=refresh)
        if not raw:
            rows.append({"url": pg["url"], "label": pg["label"], "error": "抓不到"})
            continue
        recs = FAC.parse_faculty_page(raw, pg["url"], cand["school"],
                                      cand["school_slug"], cand["unit"], group=pg["label"])
        rows.append({
            "url": pg["url"], "label": pg["label"],
            "parsed": len(recs),
            "with_research": sum(1 for r in recs if r["research_areas"]),
            "with_email": sum(1 for r in recs if r["email"]),
            "sample": (recs[0]["name"] + " / " + "、".join(recs[0]["research_areas"][:2]))
                      if recs else None,
        })
    return {"cand": cand, "pages": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    results = []
    for cand in CANDIDATES:
        print(f"\n=== {cand['school']} / {cand['unit']}  {cand['homepage']}")
        r = evaluate_unit(cand, refresh=args.refresh)
        v, got, wr = verdict(r)
        r["verdict"], r["got"], r["with_res"] = v, got, wr
        results.append(r)
        for pg in r["pages"]:
            if pg.get("error"):
                print(f"  {pg['error']:>6}  {pg['label'][:18]}")
            else:
                print(f"  抽出={pg['parsed']:>3} 方向={pg['with_research']:>3} "
                      f"{pg['label'][:24]}")
        print(f"  → {v}（共 {got} 人，有方向 {wr}）")

    buf = io.StringIO()
    p = lambda *a: print(*a, file=buf)      # noqa: E731
    ok = [r for r in results if r["verdict"].startswith("✅")]
    part = [r for r in results if r["verdict"].startswith("⚠️")]
    bad = [r for r in results if r["verdict"].startswith("❌")]
    p("# 导师解析器泛化评估\n")
    p(f"候选院系 **{len(results)}** 个 → 可用 **{len(ok)}** / 部分可用 **{len(part)}** / 不可用 **{len(bad)}**\n")
    p("> **为什么必须量这个**：解析器是照着浙大物理学院那一套调的，")
    p("> 而浙大的师资页统一挂在 `person.zju.edu.cn` 上，全校园同一模板 ——")
    p("> **别的学校没有这个系统。** 不测就写进 PPT，等于把样本当能力卖。\n")
    p("> 度量不靠人工判断，但**只报绝对量、不报比率** —— 试过两种分母都不成立：")
    p("> 用 `//h3` 数会塌成 0（很多学校用 `<a>张三</a>`，没有标题标签）；")
    p("> 改用「姓名样式的链接」又会把「本院简介」「办公指南」这类导航词算成人。")
    p("> **一个自己不可信的指标比没有指标更危险，它会制造假结论。**\n")

    p("## 汇总\n")
    p("| 学校 | 院系 | 抽出人数 | 有研究方向 | 判定 |")
    p("|---|---|---|---|---|")
    for r in results:
        mark = "（基线）" if r["cand"].get("baseline") else ""
        wr = f"{r['with_res']}/{r['got']}" if r["got"] else "—"
        p(f"| {r['cand']['school']}{mark} | {r['cand']['unit']} | {r['got']} | {wr} | {r['verdict']} |")

    p("\n## 逐页明细\n")
    for r in results:
        p(f"### {r['cand']['school']} · {r['cand']['unit']}  `{r['verdict']}`\n")
        p(f"首页：{r['cand']['homepage']}\n")
        if not r["pages"]:
            p("_没有发现任何疑似师资栏目_ —— 说明 `discover_faculty_pages` 在这所学校上失效\n")
            continue
        p("| 栏目 | url | 抽出 | 有方向 | 样例 |")
        p("|---|---|---|---|---|")
        for pg in r["pages"]:
            if pg.get("error"):
                p(f"| {pg['label'][:16]} | {pg['url'][:52]} | — | — | {pg['error']} |")
                continue
            p(f"| {pg['label'][:16]} | {pg['url'][:52]} | {pg['parsed']} | "
              f"{pg['with_research']} | {(pg['sample'] or '')[:34]} |")
        p("")

    p("## 结论怎么用\n")
    p("- **可用 ≥ 4 所** → 导师层是一个**能力**，可以写进 PPT 当差异化")
    p("- **可用 1~2 所** → 它只是一个**样本**，要么老实说「已跑通浙大」，「其他学校需要逐校适配」")
    p("- 不管结果如何，**`recycle` 低的那些栏目名要记下来** —— 它们就是解析器要补的结构")

    report = os.path.join(common.OUT_DIR, "faculty-eval.md")
    if not args.no_write:
        with open(report, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
    print("\n" + "=" * 70)
    print(buf.getvalue()[:1200])
    if not args.no_write:
        print(f"报告 -> {report}")


if __name__ == "__main__":
    main()

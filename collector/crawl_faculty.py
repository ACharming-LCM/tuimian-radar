# -*- coding: utf-8 -*-
"""导师名单抓取入口。

用法（在 collector 目录下）：
    python crawl_faculty.py                    # 按 faculty_sites.json 抓
    python crawl_faculty.py --discover         # 只探测各院系有哪些师资栏目，不抓
    python crawl_faculty.py --refresh          # 强制重抓
    python crawl_faculty.py --only 物理学院

归属：A。产出 data/out/faculty.jsonl（符合 contracts/faculty.schema.json）
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common         # noqa: E402
import faculty as FAC  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--only", default=None, help="只跑 unit 名包含该字符串的站点")
    ap.add_argument("--discover", action="store_true",
                    help="只列出各院系首页上疑似师资栏目的链接，用来填配置")
    ap.add_argument("--no-level2", action="store_true",
                    help="不做二级抓取（不访问个人主页补研究方向）")
    ap.add_argument("--max-level2", type=int, default=120,
                    help="二级抓取最多访问几个个人主页（默认 120，上限保护）")
    ap.add_argument("--allow-drop", action="store_true",
                    help="允许**质量下降**仍覆盖写盘。默认那个闸是为了防止"
                         "'这一轮没补到研究方向'把上一轮补好的数据覆盖掉")
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(HERE, "faculty_sites.json"), encoding="utf-8"))

    if args.discover:
        for site in cfg["units"]:
            print(f"\n=== {site['school']} / {site['unit']}  {site['homepage']}")
            for pg in FAC.discover_faculty_pages(site["homepage"]):
                print(f"  {pg['label'][:20]:22s} {pg['url']}")
        return

    units = cfg["units"]
    if args.only:
        units = [u for u in units if args.only in u["unit"]]

    all_recs, stat = [], {"pages": 0, "parsed": 0, "empty": 0}
    for site in units:
        print(f"\n=== {site['school']} / {site['unit']}")
        for page in site["pages"]:
            stat["pages"] += 1
            raw, _ = common.fetch(page["url"], refresh=args.refresh)
            recs = FAC.parse_faculty_page(
                raw, page["url"], site["school"], site["school_slug"],
                site["unit"], group=page.get("group"))
            if not recs:
                stat["empty"] += 1
                print(f"  --  0 人  {page.get('group') or page['url']}")
                continue
            stat["parsed"] += 1
            print(f"  OK  {len(recs):>3} 人  {page.get('group') or page['url']}")
            all_recs.extend(recs)

    # ---- 二级抓取：去个人主页补研究方向 ----
    #
    # 为什么需要：很多院系的**列表页只有姓名和职称**，一条方向都没有。
    # 浙大材料就是：`朱笑东高级实验师 卢洋藩高级实验师 …` 全挤在一行里，没有方向。
    # 不补的话这一层只能当名册用，"专业契合"的匹配无从谈起。
    # 实测：16 个人里 4/5 的个人主页能抽到方向（person.zju.edu.cn 上正文写着「研究方向：…」）。
    if not args.no_level2:
        todo = [r for r in all_recs if not r["research_areas"] and r["homepage"]]
        # 上限保护：万一某页解析出一堆外链，别把学校站点打挂。
        todo = todo[:args.max_level2]
        print(f"\n--- 二级抓取：{len(todo)} 人有主页但没方向")
        got = 0
        for r in todo:
            try:
                raw2, _ = common.fetch(r["homepage"], refresh=args.refresh)
                areas, email, title = FAC.person_research(raw2, r["homepage"])
            except Exception as e:                             # noqa: BLE001
                print(f"      取不到 {r['name']}：{type(e).__name__}")
                continue
            if areas:
                r["research_areas"] = areas
                got += 1
            if not r["email"] and email:
                r["email"] = email
            if not r["title"] and title:
                r["title"] = title
            # 补了信息就得重算内容哈希，否则变更检测看不出差异
            r["content_hash"] = FAC.rec_hash(r)
        stat["level2_tried"] = len(todo)
        stat["level2_got"] = got
        print(f"      补到方向 {got}/{len(todo)}")

    # 同一人可能挂多个栏目，按 id 去重；重复时保留信息更全的那条
    uniq = {}
    for r in all_recs:
        key = r["id"]
        if key not in uniq or len(r.get("research_areas") or []) > len(
                uniq[key].get("research_areas") or []):
            uniq[key] = r
    recs = sorted(uniq.values(), key=lambda r: (r["unit"], r["group"] or "", r["name"]))

    if not recs:
        print("\n没有抓到任何导师记录，检查 faculty_sites.json 里的栏目 URL")
        return

    # 坑（已踩）：覆盖率必须在**去重之后**的列表上算。
    # 一开始我在抓取循环里累加，结果出现 "有邮箱 54/50 (108%)" 这种丢人的数字 ——
    # 分子是去重前的、分母是去重后的。
    n = len(recs)
    stat["with_email"] = sum(1 for r in recs if r["email"])
    stat["with_research"] = sum(1 for r in recs if r["research_areas"])

    out = os.path.join(common.OUT_DIR, "faculty.jsonl")

    # ---- 闸：**质量下降不许静默覆盖** ----
    #
    # 坑（已踩，就在 2026-10-17 第一次跑日更的时候）：这一轮 `--refresh` 让个人主页
    # 缓存全部失效，而二级抓取有 `--max-level2 120` 的上限，于是只有 116 人补到了
    # 研究方向 —— 而上一轮是 **379 人**。人数还是 557（名单本身没变），
    # 所以**没有任何现有闸会拦它**：文件照样写，门禁照样绿，
    # 只是"研究方向覆盖 68% → 21%"，而匹配层的地基就没了。
    #
    # 这就是"网络不好不等于数据变少"的导师版，而且更隐蔽：**数量没变，质量掉了**。
    # 所以这里的判据是**质量**（有方向的人数），不是条数。
    def _coverage(rows):
        return sum(1 for x in rows if x.get("research_areas"))

    prev_rows = []
    if os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as f:
                prev_rows = [json.loads(ln) for ln in f if ln.strip()]
        except (json.JSONDecodeError, OSError):
            prev_rows = []

    prev_n, prev_res = len(prev_rows), _coverage(prev_rows)
    new_n, new_res = len(recs), _coverage(recs)
    if prev_res and not args.allow_drop:
        # 条数降、或者"有方向的人数"降超过 20% → 拒绝
        bad = []
        if prev_n and new_n < prev_n * common.MIN_RETAIN_RATIO:
            bad.append(f"人数 {prev_n} → {new_n}")
        if new_res < prev_res * common.MIN_RETAIN_RATIO:
            bad.append(f"有研究方向 {prev_res} → {new_res}")
        if bad:
            print(f"\n=== 统计: {stat} ===")
            print(f"❌ {'；'.join(bad)}（低于 {common.MIN_RETAIN_RATIO:.0%} 的下限）")
            print("   **不写盘** —— 上一轮的导师数据比这一轮好，覆盖它等于丢掉已经补到的方向。")
            print("   先查二级抓取为什么没补到（`--max-level2` 上限 / 个人主页抓取失败）；"
                  "确认是数据源真的变少，再显式传 --allow-drop。")
            sys.exit(2)

    with open(out, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n=== 统计: {stat}")
    print(f"导师 {n} 人 -> {out}")
    print(f"    有邮箱     {stat['with_email']}/{n}  ({stat['with_email']/n:.0%})")
    print(f"    有研究方向 {stat['with_research']}/{n}  ({stat['with_research']/n:.0%})")

    # 人类可读名册：答辩截图、以及 C 做"导师列表页"时的参照
    md = os.path.join(common.OUT_DIR, "faculty.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# 导师名册  {datetime.now():%Y-%m-%d %H:%M}\n\n")
        f.write(f"共 **{n}** 人 ｜ 有研究方向 {stat['with_research']}/{n} ｜ "
                f"有邮箱 {stat['with_email']}/{n}\n\n")
        f.write("> 这一层是产品承诺里「**专业契合**的实验室」唯一的支撑数据。\n"
                "> 校级研招网只发招生章程和拟录取名单，方向和导师只能从院系师资页拿。\n\n")
        cur_unit = cur_group = None
        for r in recs:
            if r["unit"] != cur_unit:
                cur_unit, cur_group = r["unit"], None
                f.write(f"\n# {r['school']} · {cur_unit}\n")
            if r["group"] != cur_group:
                cur_group = r["group"]
                f.write(f"\n## {cur_group or '未分组'}\n\n")
            marks = []
            if r["is_phd_supervisor"]:
                marks.append("博导")
            if r["email"]:
                marks.append("有邮箱")
            f.write(f"- **{r['name']}**（{r['title'] or '—'}）"
                    f"{' ｜ ' + ' ｜ '.join(marks) if marks else ''}  \n")
            f.write(f"  方向：{' · '.join(r['research_areas']) or '—'}  \n")
            if r["email"]:
                f.write(f"  邮箱：{r['email']}  \n")
            if r["homepage"]:
                f.write(f"  主页：{r['homepage']}  \n")
    print(f"    名册 -> {md}")

    # 研究方向覆盖率是这个数据层能不能用的关键指标 —— 没有方向就没法匹配
    if stat["with_research"] / n < 0.5:
        print("    ⚠️ 研究方向覆盖率低于 50%，这一层还不足以支撑匹配")


if __name__ == "__main__":
    main()

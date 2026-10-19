# -*- coding: utf-8 -*-
"""985 高校物理/材料/力学院系站点批量勘察。

用法（在 collector 目录下）：
    python tools/recon_985.py                    # 全跑（会访问网络，约十几分钟）
    python tools/recon_985.py --only 清华,北大    # 只跑名字里含这些词的
    python tools/recon_985.py --domain-limit 6   # 只跑前 6 所学校（调试用）
    python tools/recon_985.py --report-only      # 不抓，只按已有结果出报告

产出：
    data/out/recon-985.json     每所学校的验证结果（机器可读）
    data/out/recon-985.md       给人看的报告

**为什么要"模式假设 + 实测验证"，而不是直接写 URL 清单**：
中国高校的院系域名没有统一规则（`physics.` / `phys.` / `www.phy.` / `wlxy.` 都有），
而我没有一份权威的院系站点清单。所以做法是：
  1. 985 的**主域名**是稳定事实（写在下面 `UNIVERSITIES` 里）；
  2. 院系子域名**按常见模式批量假设**；
  3. **只有真的抓得通、而且页面标题确实是院系站的才算数**。
第 3 步是关键：假设可以随手写，**留下的必须有实测支撑**。
猜错一个域名不会污染结果，只会多一次 404 —— 这跟"把猜的 URL 写进配置"是两件事。

礼貌与边界：
  · 全部走 `common.fetch`，有本地缓存（重跑不重复打站点）、有随机间隔；
  · 只抓首页和栏目列表页，**不抓个人主页**（那是 crawl_faculty.py 二级抓取的事）；
  · 失败不重试到底，DNS 不通就直接放弃这所学校。

归属：A。这一层的产出是**名单**（哪些院系能跑通），不是最终数据。
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common          # noqa: E402
import faculty as FAC  # noqa: E402

OUT = common.OUT_DIR

# 39 所 985 的主域名。这一层是稳定事实（学校不会换主域名）。
UNIVERSITIES = [
    ("北京大学", "pku.edu.cn"), ("清华大学", "tsinghua.edu.cn"),
    ("中国人民大学", "ruc.edu.cn"), ("北京航空航天大学", "buaa.edu.cn"),
    ("北京理工大学", "bit.edu.cn"), ("中国农业大学", "cau.edu.cn"),
    ("北京师范大学", "bnu.edu.cn"), ("中央民族大学", "muc.edu.cn"),
    ("南开大学", "nankai.edu.cn"), ("天津大学", "tju.edu.cn"),
    ("大连理工大学", "dlut.edu.cn"), ("东北大学", "neu.edu.cn"),
    ("吉林大学", "jlu.edu.cn"), ("哈尔滨工业大学", "hit.edu.cn"),
    ("复旦大学", "fudan.edu.cn"), ("同济大学", "tongji.edu.cn"),
    ("上海交通大学", "sjtu.edu.cn"), ("华东师范大学", "ecnu.edu.cn"),
    ("南京大学", "nju.edu.cn"), ("东南大学", "seu.edu.cn"),
    ("浙江大学", "zju.edu.cn"), ("中国科学技术大学", "ustc.edu.cn"),
    ("厦门大学", "xmu.edu.cn"), ("山东大学", "sdu.edu.cn"),
    ("中国海洋大学", "ouc.edu.cn"), ("武汉大学", "whu.edu.cn"),
    ("华中科技大学", "hust.edu.cn"), ("湖南大学", "hnu.edu.cn"),
    ("中南大学", "csu.edu.cn"), ("中山大学", "sysu.edu.cn"),
    ("华南理工大学", "scut.edu.cn"), ("四川大学", "scu.edu.cn"),
    ("电子科技大学", "uestc.edu.cn"), ("重庆大学", "cqu.edu.cn"),
    ("西安交通大学", "xjtu.edu.cn"), ("西北工业大学", "nwpu.edu.cn"),
    ("兰州大学", "lzu.edu.cn"), ("国防科技大学", "nudt.edu.cn"),
    ("西北农林科技大学", "nwsuaf.edu.cn"),
]

# 院系子域名的常见模式。`{d}` = 主域名去掉 .edu.cn 的部分。
# 物理 / 材料 / 力学 三个方向，都是这个用户要读的方向。
SUB_PATTERNS = [
    "physics.{d}.edu.cn", "phys.{d}.edu.cn", "www.phy.{d}.edu.cn",
    "phy.{d}.edu.cn", "wlxy.{d}.edu.cn", "wlx.{d}.edu.cn",
    "mse.{d}.edu.cn", "www.mse.{d}.edu.cn", "clxy.{d}.edu.cn",
    "material.{d}.edu.cn", "me.{d}.edu.cn", "www.me.{d}.edu.cn",
]

# 页面标题里出现这些词，才认为"这确实是物理/材料/力学的院系站"
DEPT_TITLE = re.compile(r"物理|材料|力学")

# 栏目名里出现这些词，才是疑似师资栏目
FACULTY_LABEL = re.compile(
    r"师资|教师|导师|队伍|教授|人才|团队|名录|人员|faculty|teacher|people")


def candidates(domain):
    d = domain[:-len(".edu.cn")] if domain.endswith(".edu.cn") else domain
    return [p.format(d=d) for p in SUB_PATTERNS]


def probe_homepage(url):
    """抓一个候选首页。返回 None（不通/不是院系站）或 {title, url}。"""
    try:
        raw, cached = common.fetch(url, refresh=False, sleep=(0.5, 1.1), retries=1, timeout=12)
    except Exception:                                          # noqa: BLE001
        return None
    if not raw or len(raw) < 500:
        return None
    doc = common.parse(raw)
    if doc is None:
        return None
    t = doc.xpath("//title")
    title = " ".join(common.text(t[0]).split()) if t else ""
    unknown = title if not title else ""
    m = DEPT_TITLE.search(title)
    if not m:
        return None
    # 从标题里判断方向，用于分组（不猜，标成"标题推断"）
    kind = "物理" if "物理" in title else ("材料" if "材料" in title else "力学")
    return {"url": url, "title": title, "kind": kind}


def scan_faculty_pages(homepage):
    """用已有的发现函数找师资栏目，并**逐个真解析试一遍**，看能出多少人。"""
    try:
        pages = FAC.discover_faculty_pages(homepage)
    except Exception as e:                                     # noqa: BLE001
        return [], f"discover 失败 {type(e).__name__}"
    out = []
    for pg in pages[:8]:                       # 一个院系最多试 8 个栏目，别没完
        url = pg["url"]
        if not FACULTY_LABEL.search(pg.get("label") or "") and \
           not FACULTY_LABEL.search(url):
            continue
        try:
            raw, _ = common.fetch(url, refresh=False, sleep=(0.5, 1.1),
                                  retries=1, timeout=15)
        except Exception:                                      # noqa: BLE001
            continue
        recs = FAC.parse_faculty_page(raw, url, "", "", "")
        if not recs:
            continue
        withres = sum(1 for r in recs if r["research_areas"])
        out.append({"label": pg.get("label") or "", "url": url,
                    "people": len(recs), "with_research": withres,
                    "sample": recs[0]["name"]})
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑名字里含这些词的学校，逗号分隔")
    ap.add_argument("--domain-limit", type=int, default=None)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    out_json = os.path.join(OUT, "recon-985.json")
    results = []
    if os.path.exists(out_json):
        try:
            results = json.load(open(out_json, encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            results = []
    done = {r["school"] for r in results}

    if not args.report_only:
        unis = UNIVERSITIES
        if args.only:
            keys = [k.strip() for k in args.only.split(",") if k.strip()]
            unis = [u for u in unis if any(k in u[0] for k in keys)]
        if args.domain_limit:
            unis = unis[:args.domain_limit]

        for school, domain in unis:
            if school in done:
                print(f"  跳过 {school}（已有结果）")
                continue
            print(f"\n=== {school}  {domain}")
            found = []
            for cand in candidates(domain):
                hit = probe_homepage(cand)
                if hit:
                    print(f"    ✓ 院系站 {hit['url']}  [{hit['title'][:26]}]")
                    pages, err = scan_faculty_pages(hit["url"])
                    hit["faculty_pages"] = pages
                    hit["people"] = sum(p["people"] for p in pages)
                    hit["with_research"] = sum(p["with_research"] for p in pages)
                    if pages:
                        print(f"        师资栏目 {len(pages)} 个，"
                              f"抽出 {hit['people']} 人（有方向 {hit['with_research']}）")
                    else:
                        print(f"        没找到可用师资栏目 {err or ''}")
                    found.append(hit)
            results.append({"school": school, "domain": domain, "sites": found})
            json.dump(results, open(out_json, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)

    # ---- 报告 ----
    md = [f"# 985 高校院系站点勘察  {datetime.now():%Y-%m-%d %H:%M}", ""]
    tot_sites = sum(len(r["sites"]) for r in results)
    ok_sites = sum(1 for r in results for s in r["sites"] if s.get("people"))
    md.append(f"已勘察 **{len(results)}** 所学校 ｜ 验证到院系站 **{tot_sites}** 个 "
              f"｜ 其中能抽出导师的 **{ok_sites}** 个")
    md.append("")
    md.append("> **怎么读这张表**：`院系站` 是抓得通、且页面标题确实是物理/材料/力学的；")
    md.append("> `抽出` 是用现有三条解析路径真跑一遍的结果。**0 人不代表站点没数据**，")
    md.append("> 只代表现有解析器还不认它的结构 —— 这正是下一步要补的地方。")
    md.append("")
    md.append("| 学校 | 院系站 | 方向 | 抽出人数 | 有方向 | 用哪个栏目 |")
    md.append("|---|---|---|---|---|---|")
    for r in results:
        if not r["sites"]:
            md.append(f"| {r['school']} | — | — | — | — | **没找到院系站** |")
            continue
        for s in r["sites"]:
            used = "、".join(p["label"][:10] for p in s.get("faculty_pages") or []) or "—"
            md.append(f"| {r['school']} | {s['title'][:22]} | {s['kind']} | "
                      f"{s.get('people', 0)} | {s.get('with_research', 0)} | {used} |")
    with open(os.path.join(OUT, "recon-985.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"\n报告 -> {os.path.join(OUT, 'recon-985.md')}")
    print(f"数据 -> {out_json}")


if __name__ == "__main__":
    main()

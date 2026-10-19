# -*- coding: utf-8 -*-
"""从 **Tsites 平台**的导师查询接口拉"名录级"导师数据。

用法（在 collector 目录下）：
    python tools/tsites_faculty.py --probe https://supervisor.fudan.edu.cn/
    python tools/tsites_faculty.py --site https://supervisor.fudan.edu.cn/ --out fudan

产出（**独立文件，不并进 `faculty.jsonl`**）：
    data/out/faculty-roster-<out>.jsonl

---

## 为什么单独一个文件、不并进主数据

这个接口给的是**名录级**数据：姓名 / 邮箱 / **学科** / 导师类型，**没有研究方向**
（实测 `researchdirection` 填充率 **0/200**）。而"专业契合"匹配要的是研究方向。

并进 `faculty.jsonl` 会把那一层 87% 的方向覆盖率**稀释成一个假的数字** ——
宁少不假。所以这里输出到独立文件，并且每条都带 `_level: "roster"`。

## 接口怎么来的（可复现）

复旦 `supervisor.fudan.edu.cn` 是 Tsites 建的。读它引用的
`/system/resource/tsites/dsfcadvancesearch.js`，里面有：

    GET /system/resource/tsites/dsfcadvancesearch.jsp
        ?enrolleunit= &enrollid= &yjxk= &pageindex= &pagesize=
        &teacherName= &searchDirection= &dslx=
        &viewmode=8 &viewid=… &siteOwner=… &viewUniqueId=…

后三个 id 在页面的 `var tsites_load_data_options = {...}` 里，本工具自己从页面抠，
**不写死**（写死就只能跑复旦一所）。

## 已知边界

- **杠杆有限**：扫过手上 15 所学校的研招网/研究生院首页，只有复旦用 Tsites。
- `faculty.<host>/<slug>/` 那些个人主页**返回 617 B 的错误页**，
  所以研究方向也补不到（去掉 `/zh_CN/index.htm`、加 Referer 都试过）。
- 详见 `docs/FACULTY-ROUTES.md`。

归属：A。
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common          # noqa: E402

OUT = common.OUT_DIR
API_JS = "/system/resource/tsites/dsfcadvancesearch.js"
API_JSP = "/system/resource/tsites/dsfcadvancesearch.jsp"


def load_options(home):
    """从首页里抠出 viewid / siteOwner / viewUniqueId —— **不写死**。"""
    raw, _ = common.fetch(home, refresh=False, sleep=(0.5, 1.1), retries=1, timeout=25)
    if not raw:
        return None, "首页取不到"
    html = common.decode(raw)
    m = re.search(r"tsites_load_data_options\s*=\s*(\{.*?\})\s*;", html, re.S)
    if not m:
        return None, "页面里没有 tsites_load_data_options（可能不是 Tsites 站点）"
    try:
        opts = json.loads(m.group(1))
    except Exception as e:                                     # noqa: BLE001
        return None, f"解析 options 失败：{e}"
    need = ("viewId", "siteOwner", "viewUniqueId")
    if not all(k in opts for k in need):
        return None, f"options 缺字段：{[k for k in need if k not in opts]}"
    return opts, None


def query(home, opts, pageindex=1, pagesize=100):
    p = {"enrolleunit": "", "enrollid": "", "yjxk": "",
         "pageindex": str(pageindex), "pagesize": str(pagesize),
         "teacherName": "", "searchDirection": "", "dslx": "",
         "viewmode": str(opts.get("viewMode", 8)),
         "viewid": str(opts["viewId"]),
         "siteOwner": str(opts["siteOwner"]),
         "viewUniqueId": str(opts["viewUniqueId"]),
         "ispreview": "false", "ellipsis": "...", "alignright": "false"}
    u = home.rstrip("/") + API_JSP + "?" + urllib.parse.urlencode(p)
    raw, _ = common.fetch(u, refresh=False, sleep=(0.5, 1.1), retries=1, timeout=30)
    if not raw:
        return None
    try:
        return json.loads(common.decode(raw))
    except Exception:                                          # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", default=None, help="只探测这个站是不是 Tsites，并打首页样本")
    ap.add_argument("--site", default=None, help="拉取这个站的导师名录")
    ap.add_argument("--out", default="tsites", help="输出文件后缀")
    ap.add_argument("--pagesize", type=int, default=200)
    ap.add_argument("--max-pages", type=int, default=40, help="上限保护")
    args = ap.parse_args()

    target = args.probe or args.site
    if not target:
        print("给一个 --probe 或 --site"); return

    opts, err = load_options(target)
    if err:
        print(f"  ✗ {target}\n    {err}")
        return
    print(f"  ✓ {target} 是 Tsites 站点")
    print(f"    viewId={opts['viewId']} siteOwner={opts['siteOwner']} "
          f"viewUniqueId={opts['viewUniqueId']}")

    d = query(target, opts, 1, args.pagesize)
    if not d:
        print("  ✗ 接口没返回"); return
    rows = d.get("teacherData") or []
    print(f"    总数 {d.get('totalnum')}，共 {d.get('totalpage')} 页，"
          f"本页 {len(rows)} 条")

    if args.probe:
        def fill(k):
            return sum(1 for r in rows if (r.get(k) or "").strip())
        print("    **字段填充率（这决定它能不能用于匹配）**")
        for k in ("name", "dslx", "email", "researchdirection", "url", "enrolldiscipline"):
            print(f"      {k:20s} {fill(k):>4}/{len(rows)}")
        if fill("researchdirection") == 0:
            print("    ⚠️ 研究方向全空 → **这是名录级数据，不能用于「专业契合」匹配**")
        return

    # ---- 拉全量 ----
    allrows, page = [], 1
    while page <= args.max_pages:
        d = query(target, opts, page, args.pagesize)
        if not d:
            break
        batch = d.get("teacherData") or []
        if not batch:
            break
        allrows.extend(batch)
        if page >= (d.get("totalpage") or 1):
            break
        page += 1

    # 去重：同一个人可能挂多个学科
    uniq, dup = {}, 0
    for r in allrows:
        key = (r.get("name") or "", r.get("email") or "", r.get("enrolldiscipline") or "")
        if key in uniq:
            dup += 1
            continue
        uniq[key] = r

    out = os.path.join(OUT, f"faculty-roster-{args.out}.jsonl")
    now = datetime.now().isoformat(timespec="seconds")
    with open(out, "w", encoding="utf-8") as f:
        for (name, email, disc), r in uniq.items():
            f.write(json.dumps({
                "_level": "roster",          # ← **明确标级：名录级，无研究方向**
                "_source": target,
                "_crawled_at": now,
                "name": name,
                "title": r.get("dslx"),
                "email": email or None,
                "discipline": disc,
                "homepage": r.get("url"),
                # 接口有 researchdirection 字段但实测全空；保留原值，不做任何填充
                "research_areas": [x for x in (r.get("researchdirection") or "").split()
                                   if x],
            }, ensure_ascii=False) + "\n")
    withdir = sum(1 for r in uniq.values() if (r.get("researchdirection") or "").strip())
    print(f"\n  {len(uniq)} 条 -> {out}")
    print(f"    （原始 {len(allrows)} 条，按 姓名+邮箱+学科 去重掉 {dup} 条）")
    print(f"    其中带研究方向的：**{withdir}** 条")
    print("    提醒：这是名录级数据，不要并进 faculty.jsonl 去当匹配用。")


if __name__ == "__main__":
    main()

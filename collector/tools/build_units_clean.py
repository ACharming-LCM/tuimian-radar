# -*- coding: utf-8 -*-
"""生成 `data/out/units-clean.json`：能支撑"按学院订阅"的单位清单（A26）。

    cd collector && python tools/build_units_clean.py --write

## 为什么需要它

C 要做"复旦大学 + 经济学院"这种订阅，但数据里只有 school / unit 两个字段，
而 unit 是自由文本。这份清单把三类来源并成一张表，让前端**不用猜**：

- `notices.jsonl`：有公告的单位（notice_count / latest_publish）
- `faculty.jsonl`：有导师的单位（faculty_count）
- `sites.json`：配置里存在、但这轮 0 产出的单位

每条给出 `level`（校级 / 院系级 / 师资），前端据此决定"这所学校能不能按学院订阅"。

## 实测结论（2026-10-19）

**unit 字段本身没有脏名字**：34 个 (school, unit) 组合，没有空值、没有菜单串。
脏的是 `board`（栏目名），例如「学位与导师 学位授权 学位管理 导师队伍 答辩公告」——
那是导航文字被当成了栏目名，**不属于 unit**。所以这份清单的"收敛"是 0 条，
真正的问题是**院系级覆盖太薄**：29 所学校里只有 9 所有院系级单位。
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

OUT_DIR = common.OUT_DIR

# 校级单位的别名 —— 同一个东西在不同学校叫法不同，订阅页不该出现三个"研招网"。
SCHOOL_LEVEL_ALIASES = {
    "研究生招生网": "研究生招生网",
    "研究生招生信息网": "研究生招生网",
    "研招网": "研究生招生网",
    "招生信息网": "研究生招生网",
    "研究生院": "研究生院",
}

# "院系级"的判定：以这些词结尾。顺序无所谓，命中即院系级。
COLLEGE_SUFFIXES = ("学院", "学系", "系", "研究院", "研究所", "中心", "学部", "实验室")

# 脏名字判据：**只用来发现"菜单被当成了单位名"**，不是风格检查。
DIRTY_PATTERNS = [
    (re.compile(r"\s{2,}"), "含连续空格（多半是导航文字）"),
    (re.compile(r"^\s|\s$"), "首尾有空格"),
    (re.compile(r"[丨|]"), "含分隔符（菜单串）"),
    (re.compile(r"(更多|首页|点击|查看)"), "含菜单词"),
    (re.compile(r"^\s*$"), "空名字"),
    (re.compile(r".{20,}"), "名字过长（≥20 字，多半是句子）"),
]


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                rows.append(json.loads(ln))
    return rows


def classify_level(unit):
    """校级 / 院系级 / 师资。"""
    if unit in SCHOOL_LEVEL_ALIASES:
        return "校级"
    if "教师主页" in unit or "师资" in unit:
        return "师资"
    if unit.endswith(COLLEGE_SUFFIXES):
        return "院系级"
    return "其他"


def dirty_reasons(unit):
    reasons = []
    for pat, why in DIRTY_PATTERNS:
        if pat.search(unit or ""):
            reasons.append(why)
    return reasons


def canonicalize(unit):
    u = re.sub(r"\s+", " ", (unit or "").strip())
    return SCHOOL_LEVEL_ALIASES.get(u, u)


def build(notices, faculty, sites):
    """纯函数：三份输入 -> units-clean 结构。"""
    agg = defaultdict(lambda: {"notice_count": 0, "latest_publish": "",
                               "faculty_count": 0})

    for r in notices:
        key = (r.get("school") or "", r.get("unit") or "")
        a = agg[key]
        a["notice_count"] += 1
        d = r.get("publish_date") or ""
        if d > a["latest_publish"]:
            a["latest_publish"] = d
    for r in faculty:
        key = (r.get("school") or "", r.get("unit") or "")
        agg[key]["faculty_count"] += 1
    for s in sites:
        agg[(s.get("school") or "", s.get("unit") or "")]  # 占位：0 产出也要出现

    units = []
    changed = []
    for (school, raw), a in agg.items():
        canon = canonicalize(raw)
        reasons = dirty_reasons(raw)
        if canon != raw:
            changed.append({"school": school, "before": raw, "after": canon})
        units.append({
            "school": school,
            "unit": canon,
            "raw_unit": raw,
            "level": classify_level(canon),
            "notice_count": a["notice_count"],
            "latest_publish": a["latest_publish"] or None,
            "faculty_count": a["faculty_count"],
            "dirty": bool(reasons),
            "dirty_reasons": reasons,
            # 前端做订阅键用：稳定的 (学校, 单位) 组合
            "key": f"{school}|{canon}",
            # 只列"看得见"的：有公告或有导师，才值得出现在订阅页
            "subscribable": a["notice_count"] > 0 or a["faculty_count"] > 0,
        })

    order = {"校级": 0, "院系级": 1, "师资": 2, "其他": 3}
    units.sort(key=lambda u: (u["school"], order.get(u["level"], 9),
                              -u["notice_count"], u["unit"]))

    # ---- 附录：真正脏的是 board（栏目名），不是 unit ----
    # 实测 83 个栏目名里 12 个是"导航文字被当成栏目名"（「更多>> 研究生院公告」），
    # 覆盖 444 条记录。这里只**给出建议映射**，不动数据 —— board 会进 relevance 和 facets，
    # 改它要连带重算派生字段，值得单独一单。
    board_counts = defaultdict(int)
    for r in notices:
        board_counts[r.get("board") or ""] += 1
    dirty_boards = []
    for b, n in board_counts.items():
        reasons = dirty_reasons(b) if b else ["空栏目名"]
        if not reasons:
            continue
        clean = re.sub(r"\s*更多\s*[>＞+]+\s*", "", b)
        clean = re.sub(r"\s+", " ", clean).strip()
        # 去掉"更多>>"之后还剩一长串导航词的，regex 修不了 —— 如实标成低置信，
        # 别让前端拿一个照样是菜单的名字去展示。
        auto = bool(clean) and len(clean) <= 12 and clean.count(" ") <= 2
        dirty_boards.append({"board": b, "records": n,
                             "suggested": clean or "（空）",
                             "auto_fixable": auto,
                             "reasons": reasons})
    dirty_boards.sort(key=lambda x: -x["records"])

    by_school = defaultdict(lambda: {"校级": 0, "院系级": 0, "师资": 0, "其他": 0})
    for u in units:
        by_school[u["school"]][u["level"]] += 1
    schools = [{"school": s, "counts": c,
                "has_college": c["院系级"] > 0,
                "unit_count": sum(c.values())}
               for s, c in sorted(by_school.items())]

    return {
        "generated_at": None,      # 由 main 填
        "total_units": len(units),
        "total_schools": len(schools),
        "schools_with_college": sum(1 for s in schools if s["has_college"]),
        "dirty_units": sum(1 for u in units if u["dirty"]),
        "dirty_boards": dirty_boards,
        "changed": changed,
        "_note": ("unit 是自由文本，这份表是**唯一口径**：前端做\"学校 + 学院\"订阅时读它，"
                  "不要自己从 notices 里 group by。level=校级 的单位是研究生院/研招网，"
                  "院系级 才是真正的学院；学校没有院系级单位时，只能提供校级订阅。"
                  "dirty=true 的 raw_unit 已被 canonicalize 收敛（本轮实测 0 条）。"),
        "units": units,
        "schools": schools,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notices", default=os.path.join(OUT_DIR, "notices.jsonl"))
    ap.add_argument("--faculty", default=os.path.join(OUT_DIR, "faculty.jsonl"))
    ap.add_argument("--sites", default=os.path.join(COLLECTOR, "sites.json"))
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "units-clean.json"))
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    notices = load_jsonl(a.notices)
    faculty = load_jsonl(a.faculty)
    with open(a.sites, encoding="utf-8") as f:
        sites = json.load(f)["schools"]

    built = build(notices, faculty, sites)
    from datetime import datetime
    built["generated_at"] = datetime.now().isoformat(timespec="seconds")

    print(f"单位 {built['total_units']} 个 / 学校 {built['total_schools']} 所 / "
          f"有院系级单位的 {built['schools_with_college']} 所")
    print(f"  脏名字 {built['dirty_units']} 个；收敛 {len(built['changed'])} 条")
    for u in built["units"]:
        if u["dirty"]:
            print(f"    ⚠️ {u['school']} | {u['raw_unit']} -> {u['unit']} | {u['dirty_reasons']}")
    if not a.write:
        print("\n（预演，未写盘。加 --write 落盘）")
        return 0
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(built, f, ensure_ascii=False, indent=1)
    os.replace(tmp, a.out)
    print(f"\n✅ 已写 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

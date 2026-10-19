# -*- coding: utf-8 -*-
"""数据量突变门禁（B21）：**数据被写残时必须红**。

## 为什么要有这个文件

2026-10-19 凌晨，`data/out/notices.jsonl` 被写残成 **69 条**（原 2751），
持续了约 **3 小时**，而门禁 **一条都没报**。

不是漏写了断言，是**所有断言都在验形状**：69 条合法记录和 2751 条合法记录
在键集合、分页、三态文案上没有任何区别。**形状对 ≠ 内容对** ——
`test_data_health.py` 的 docstring 里已经写过这句话，但它验的是
"每条记录好不好"，没有一条在问"**记录还在不在**"。

## 判据只有一条，但方向很重要

**当前公告条数比基线少超过 20% → 红**，并报出少了多少条、哪几所学校少了。

三个刻意的决定：

1. **比"变化"，不比绝对值。** 2751 不是一个永远正确的数（数据会涨）；
   写死它，哪天涨到 5000 门禁就自己红了。基线是**提交进版本库的**
   （`contracts/data_volume_baseline.json`），改它要在 journal 里说明理由。
2. **只抓"突然少一大截"。** 每天少 1%、十天少 10% 这种缓慢流失它抓不到 ——
   那种要靠每日快照做趋势，**这里不假装能做**。20% 这个阈值的意思是
   "一次事故级的缩水"。
3. **缺基线 = 红，不是绿。** 没有比较对象时，这条判据会变成"永远通过"，
   比没有更糟（和 `test_data_health._baseline` 同一条规矩）。

## 两个方向都不误报

- **数据变多**：正常（A 每天在抓），不红；
- **数据小幅变少**（合并重复、清理脏记录）：20% 以内不红 —— 那是正常运维。

## 重出基线

    D:\\python.3.10.11\\python.exe server/tests/test_data_volume.py --write-baseline

**注意**：如果当前条数比旧基线少两成以上，`--write-baseline` 会拒绝写入，
要显式加 `--force` —— 防止"数据刚被写残，顺手把残值记成新基线"，
那等于事故自愈、门禁永远绿。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import config  # noqa: E402

BASELINE = os.environ.get(
    "TUIMIAN_VOLUME_BASELINE",
    os.path.join(ROOT, "contracts", "data_volume_baseline.json"))
UNKNOWN_SCHOOL = "（未知学校）"


# ---------------------------------------------------------------------------
# 测量
# ---------------------------------------------------------------------------

def _load_jsonl(path):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                # 坏行不算"公告" —— 半截 JSON 正是"正在被写"的形状。
                # 不在这里崩：坏行的问题由 `test_data_health` 和加载器的
                # `load_state` 负责说；这里只数"数得出来的条数"。
                continue
    return recs


def measure():
    """当前数据量：总条数 + 每所学校条数。**基线文件和门禁都用它**，避免两套口径。"""
    recs = _load_jsonl(config.NOTICES_PATH)
    schools = Counter((r.get("school") or "").strip() or UNKNOWN_SCHOOL for r in recs)
    return {
        "notices": len(recs),
        "schools": dict(sorted(schools.items())),
        "source": os.path.relpath(config.NOTICES_PATH, ROOT),
    }


# ---------------------------------------------------------------------------
# 比较
# ---------------------------------------------------------------------------

def compare(current, base, drop_limit=None):
    """返回 `None`（正常）或一份"数据可能被写残"的报告（dict）。

    `drop_limit` 默认取 `config.DATA_VOLUME_DROP_LIMIT`（0.20）。
    **边界按"超过"算**：正好少 20% 不红，20.01% 红。
    """
    limit = config.DATA_VOLUME_DROP_LIMIT if drop_limit is None else drop_limit
    b = int(base.get("notices") or 0)
    c = int(current.get("notices") or 0)
    if b <= 0:
        raise AssertionError(
            "数据量基线里的 notices 是 0 —— 这份基线本身不可用（0 条做分母永远是 0%）。\n"
            "  重出：python server/tests/test_data_volume.py --write-baseline")

    lost = b - c
    if lost <= 0:
        return None                      # 变多 / 不变：正常
    ratio = lost / b
    if ratio <= limit + 1e-12:           # 边界算"没超过"
        return None

    base_schools = base.get("schools") or {}
    cur_schools = current.get("schools") or {}
    school_lost = []
    for s, n in base_schools.items():
        n = int(n)
        now = int(cur_schools.get(s, 0))
        if now < n:
            school_lost.append({"school": s, "before": n, "after": now, "lost": n - now})
    school_lost.sort(key=lambda x: (-x["lost"], x["school"]))

    return {
        "baseline": b,
        "current": c,
        "lost": lost,
        "drop_ratio": round(ratio, 4),
        "limit": limit,
        "schools": school_lost,
        "new_schools": sorted(set(cur_schools) - set(base_schools)),
    }


def message(rep, base=None):
    """给人看的红信息。**必须同时给三个数**：少了多少、比例、哪几所学校。"""
    lines = [
        "数据可能被写残：公告 **%d → %d** 条（少了 **%d** 条，-%.1f%%），"
        "超过 %.0f%% 的阈值。"
        % (rep["baseline"], rep["current"], rep["lost"],
           rep["drop_ratio"] * 100, rep["limit"] * 100),
    ]
    if base:
        lines.append("基线：%s（%s 记录，%d 条；当时 %d 所学校）。"
                     % (os.path.relpath(BASELINE, ROOT),
                        base.get("recorded_at") or "时间未知",
                        rep["baseline"], len(base.get("schools") or {})))
    if rep["schools"]:
        lines.append("掉得最多的学校（共 %d 所变少）：" % len(rep["schools"]))
        for x in rep["schools"][:5]:
            lines.append("  %s  %d → %d（-%d 条）"
                         % (x["school"], x["before"], x["after"], x["lost"]))
        if len(rep["schools"]) > 5:
            lines.append("  …另有 %d 所" % (len(rep["schools"]) - 5))
    else:
        lines.append("**按学校看不出来**：基线里没有这所学校（或学校名被改了）——")
        lines.append("  这说明要么数据整批换了，要么基线的学校口径变了。")
    if rep.get("new_schools"):
        lines.append("新出现的学校：%s" % "、".join(rep["new_schools"][:5]))
    lines.append("")
    lines.append("常见原因：抓取/清洗**非原子写**（先清空再逐条写，中途挂了）；")
    lines.append("或者某一轮抓取把栏目选丢了。**先看 data/out/ 的 mtime 和日志，别看接口** ——")
    lines.append("  接口形状是好的，坏的是数量。")
    lines.append("如果这是**有意的**重抓/清理，更新基线并在 journal 里说明理由：")
    lines.append("  D:\\python.3.10.11\\python.exe server/tests/test_data_volume.py --write-baseline")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 基线
# ---------------------------------------------------------------------------

def _baseline():
    if not os.path.exists(BASELINE):
        raise AssertionError(
            "缺少数据量基线 %s。\n"
            "  没有它，这条判据会变成\"永远通过\"—— 那比没有更糟。\n"
            "  建立：python server/tests/test_data_volume.py --write-baseline"
            % os.path.relpath(BASELINE, ROOT))
    with open(BASELINE, encoding="utf-8") as f:
        return json.load(f)


def write_baseline(force=False):
    cur = measure()
    old = None
    if os.path.exists(BASELINE):
        with open(BASELINE, encoding="utf-8") as f:
            old = json.load(f)
    if old and not force:
        rep = compare(cur, old)
        if rep is not None:
            raise SystemExit(
                "拒绝把 %d 条写成新基线 —— 比旧基线 %d 条少了 %.1f%%，"
                "超过 %.0f%% 的阈值。\n"
                "  数据可能正因为写残而变少，这时更新基线等于把事故记成「正常」。\n"
                "  确认这次缩水是有意的，再加 --force。\n\n%s"
                % (cur["notices"], rep["baseline"], rep["drop_ratio"] * 100,
                   rep["limit"] * 100, message(rep, old)))
    data = {
        "notices": cur["notices"],
        "schools": cur["schools"],
        "recorded_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "source": cur["source"],
        "_note": ("公告条数的基线（B21 数据量突变门禁的比较对象）。"
                  "只在**有意的**重抓/清理之后重出；改它要在 journal 里说明理由。"
                  "重出：python server/tests/test_data_volume.py --write-baseline"),
    }
    with open(BASELINE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("已重出 %s：%d 条 / %d 所学校"
          % (os.path.relpath(BASELINE, ROOT), data["notices"], len(data["schools"])))


# ---------------------------------------------------------------------------
# 门禁用例（由 run.py 用 importlib 加载并 extend 进 CASES）
# ---------------------------------------------------------------------------

def _case(fn):
    CASES.append(fn)
    return fn


CASES = []


@_case
def data_公告总量不许突然少两成():
    """**B21 的主判据。** 真实数据 vs 提交进版本库的基线。

    防的事故（真实发生过）：`notices.jsonl` 被写残成 69 条（原 2751），
    持续 3 小时，门禁全绿 —— 因为 69 条合法记录在**形状**上和 2751 条没区别。
    """
    cur = measure()
    base = _baseline()
    rep = compare(cur, base)
    assert rep is None, message(rep, base)


@_case
def data_量突变判据必须能点名到学校():
    """**反向验证：造一份"只剩 69 条"的假数据，这条判据必须红，而且要说出是哪几所。**

    如果只报"少了 2682 条"却不说是哪几所学校，排查时还是得自己去找 ——
    "点名"是这条判据的一半价值（另一半是它会红）。
    """
    base = {
        "notices": 2751,
        "schools": {"北京大学": 1200, "清华大学": 900, "浙江大学": 500,
                    "天津大学": 151},
        "recorded_at": "2026-10-19T00:00:00",
    }
    cur = {"notices": 69, "schools": {"北京大学": 69}}
    rep = compare(cur, base)
    assert rep is not None, "只剩 69 条（-97.5%）居然没红 —— 这条判据失效了"
    assert rep["lost"] == 2682 and rep["baseline"] == 2751 and rep["current"] == 69, rep
    assert rep["drop_ratio"] > 0.97, rep
    names = [x["school"] for x in rep["schools"]]
    # 三所整校消失的都必须点名（按掉得多少排序，北大 1131 最大）
    assert names[:3] == ["北京大学", "清华大学", "浙江大学"], names
    assert rep["schools"][0]["lost"] == 1131, rep["schools"][0]

    text = message(rep, base)
    assert "少了 **2682** 条" in text, text
    assert "北京大学" in text and "清华大学" in text, text
    assert ("非原子写" in text) or ("data/out" in text), (
        "红信息里没有指向数据层（写残/日志）的方向，只会让人去查接口", text)


@_case
def data_量正常波动不许误报():
    """**两个方向都不误报** —— 误报会把这条判据变成噪音，然后被关掉。

    变多（A 每天在抓）、小幅变少（合并重复/清脏记录）都不该红；
    **正好少 20% 算"没超过"**，20.1% 才算红（边界写清楚，别靠感觉）。
    """
    base = {"notices": 1000, "schools": {"甲大学": 600, "乙大学": 400}}
    ok_cases = [
        {"notices": 1100, "schools": {"甲大学": 700, "乙大学": 400}},   # 变多
        {"notices": 1000, "schools": {"甲大学": 600, "乙大学": 400}},   # 不变
        {"notices": 850, "schools": {"甲大学": 500, "乙大学": 350}},    # -15%
        {"notices": 800, "schools": {"甲大学": 500, "乙大学": 300}},    # -20%（边界）
    ]
    for cur in ok_cases:
        assert compare(cur, base) is None, ("正常波动被误报", cur)

    bad = {"notices": 799, "schools": {"甲大学": 500, "乙大学": 299}}   # -20.1%
    rep = compare(bad, base)
    assert rep is not None, "少 20.1% 没红 —— 阈值边界写反了"
    assert rep["lost"] == 201, rep


@_case
def data_量基线自身必须自洽():
    """基线文件是手写/脚本写的 JSON，会坏。**自洽性先验一遍**：

    - 总条数必须等于各校条数之和（不等的基线，减出来的"哪所学校少了"是假的）；
    - 必须有 `recorded_at`（没有日期就不知道它代表哪一天的数据）。

    这一条防的是"基线被人改坏，然后门禁拿着坏基线去判数据"。
    """
    base = _baseline()
    total = int(base.get("notices") or 0)
    schools = base.get("schools") or {}
    assert schools, "基线里没有按学校的条数 —— 少了也只能报总数，说不出是哪几所"
    assert sum(int(v) for v in schools.values()) == total, (
        "基线自相矛盾：总条数 %d ≠ 各校之和 %d"
        % (total, sum(int(v) for v in schools.values())))
    assert base.get("recorded_at"), "基线缺 recorded_at —— 不知道它代表哪天的数据"


# ---------------------------------------------------------------------------
# 单独跑（排查时用）
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="数据量突变门禁 / 重出基线")
    ap.add_argument("--write-baseline", action="store_true", help="把当前条数写成新基线")
    ap.add_argument("--force", action="store_true",
                    help="缩水超过两成也照样写（确认是有意清理时才用）")
    args = ap.parse_args()

    if args.write_baseline:
        write_baseline(force=args.force)
        return 0

    failed = 0
    for fn in CASES:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print("\nFAIL  %s\n%s" % (fn.__name__, e))
        else:
            print("ok    %s" % fn.__name__)
    print("\n%d 通过 / %d 失败 / 共 %d 条" % (len(CASES) - failed, failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

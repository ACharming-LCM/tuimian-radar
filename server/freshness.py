# -*- coding: utf-8 -*-
"""数据新鲜度。契约见 `contracts/api.md` 第 12 节。

产品最核心的承诺是"每天更新"。在这个模块之前，**这句话不可验证** ——
没有任何接口能回答"数据是什么时候的"。评委问一句就得翻文件系统。

数据源 `data/out/freshness.json` 由 A 产出，字段是 A 先定、B 照写。
**数据层只报事实，不做"新鲜不新鲜"的判断** —— 阈值在这里。

## 两个必须自己算、不能信文件的字段

**1. `freshness_hours` 不能用。** 它是数据层**生成时刻**算的快照
（实测 37 所学校全是 `0.0`，因为 `last_ok` 都等于 `generated_at`）。
文件放着不动它就是 0，隔 20 小时还说是 0 —— 拿它判断会得出"永远刚更新过"。
所以 `age_hours` **一律按 `last_ok` 和当前时间现算**。

**2. `last_ok` 的语义是"最后一次尝试"，不是"最后一次成功"。**
A 的实现里它被**无条件**写成"本轮时间"，失败的学校也写。
所以一个连续失败的学校在 `last_ok` 上看起来和新的一模一样。
**这正是"抓取失败不可见"那个洞的来源**，靠 `status` 的三态划分来补（见下）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

# ---------------------------------------------------------------------------
# stale 阈值。**写死一个数，理由如下**（契约第 12 节）
#
#   · 下限由节拍定：管道一天一轮，24 小时以内的数据**必须**算新鲜 ——
#     否则每次正常运行都会有一段时间被标成"过期"，那个标记就没人信了。
#   · 上限由容错定：24 小时节拍 + **6 小时宽限**。少跑一轮不该立刻报警；
#     而真的漏了一整天一定会被标出来。
#
# 为什么不分档（"这个学校应该多久更新一次"）：我们没有那个先验，
# 编一个出来就是假装知道。一个数 + 说清理由，比一套看起来聪明的分档诚实。
#
# **这是工程约定，不是事实** —— 所以它出现在响应里（`stale_after_hours`），
# 让读接口的人看得见判断依据。
# ---------------------------------------------------------------------------
STALE_AFTER_HOURS = 30.0

# `status` 的优先级（越靠前越坏）。`status` 只是排序/上色用的最坏值，
# 三个信号（has_failure / empty / freshness）都独立给全。
STATUS_ORDER = ("failed", "empty", "stale", "ok")


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def _age_hours(dt, now):
    """时间差（小时），四舍五入两位。`now`/`dt` 都是 naive local time。

    **不许出现负数。** 容器时区是 UTC、数据时间戳是 +08 时，`now - dt`
    会是负的（实测 -7.68 小时），页面会写「-7.7 小时前」。时间戳在未来
    只可能是时钟/时区不一致；对外说「0 小时」比说一个负数诚实。
    """
    if dt is None:
        return None
    return round(max(0.0, (now - dt).total_seconds() / 3600.0), 2)


def humanize_age(hours):
    """现成的中文相对时间。按本项目规矩"前端渲染要用的字段必须在同一个响应里给全" ——
    让 C 自己算会有三个页面三种写法。"""
    if hours is None:
        return None
    if hours < 1 / 60:
        return "刚刚"
    if hours < 1:
        return f"{int(round(hours * 60))} 分钟前"
    if hours < 24:
        return f"{hours:.1f} 小时前"
    return f"{hours / 24:.1f} 天前"


class FreshnessStore:
    def __init__(self, path, data, load_error=None, stale_after_hours=STALE_AFTER_HOURS):
        self.path = path
        self.data = data
        self.load_error = load_error
        self.stale_after_hours = stale_after_hours

    @classmethod
    def load(cls, path, stale_after_hours=STALE_AFTER_HOURS):
        if not path or not os.path.exists(path):
            return cls(path, None, load_error="还没有新鲜度数据（每日流程没跑过）",
                       stale_after_hours=stale_after_hours)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls(path, json.load(f), stale_after_hours=stale_after_hours)
        except (json.JSONDecodeError, OSError) as e:
            return cls(path, None, load_error=f"新鲜度数据读取失败：{e}",
                       stale_after_hours=stale_after_hours)

    # ---------- 单校 ----------

    def school_view(self, s, now):
        last_ok = _parse_dt(s.get("last_ok"))
        age = _age_hours(last_ok, now)
        # 没有 last_ok 就没有年龄 —— 不能当成 0（那是"刚更新过"的意思）
        fresh = "unknown" if age is None else (
            "fresh" if age <= self.stale_after_hours else "stale")

        failed = list(s.get("failed") or [])
        has_failure = bool(failed)
        boards = s.get("boards")
        boards_empty = s.get("boards_empty")
        records = s.get("records")
        # **`empty` 这一态是"抓取失败不可见"的解药。**
        # 实测浙大研招网：boards=2 / boards_empty=2 / records=0 / failed=[]
        # —— 两个栏目全空、一条没抓到、**没有任何错误码**，
        # 在只看 failed 的旧口径下它是完美的"成功"。
        empty = bool(not has_failure and isinstance(boards, int) and boards > 0
                     and boards_empty == boards and records == 0)

        if has_failure:
            status = "failed"
        elif empty:
            status = "empty"
        elif fresh == "stale":
            status = "stale"
        elif age is None:
            status = "unknown"
        else:
            status = "ok"

        return {
            "school": s.get("school"), "unit": s.get("unit"),
            "homepage": s.get("homepage"),
            "last_ok": s.get("last_ok"),
            "age_hours": age,
            # 每所学校给 fresh/stale —— 契约要求
            "freshness": fresh,
            "records": records,
            "boards": boards, "boards_empty": boards_empty,
            "failed": failed, "has_failure": has_failure,
            "empty": empty,
            "status": status,
        }

    # ---------- 整体 ----------

    def response(self, now=None):
        now = now or datetime.now()
        base = {
            "generated_at": None, "now": now.isoformat(timespec="seconds"),
            "generated_age_hours": None, "generated_age_text": None,
            "oldest_hours": None,
            "stale_after_hours": self.stale_after_hours,
            "worst": "unknown",
            "totals": {"records": 0, "schools": 0,
                       "failed_schools": 0, "stale_schools": 0, "empty_schools": 0},
            "schools": [],
            "source": os.path.abspath(self.path) if self.path else None,
            "_note": ("stale_after_hours 是接口层的工程约定（一天一轮 + 6 小时宽限），"
                      "不是事实；age_hours 按 last_ok 现算，不用文件里的 freshness_hours"
                      "（那是生成时刻的快照，文件不动它永远是 0）。"),
        }

        # ---- 从来没跑过 / 读不到 ----
        # **不崩、不 500、不假装 ok。** "没有数据"和"数据是新鲜的"是两个结论。
        if self.data is None:
            base["_error"] = self.load_error
            base["message"] = "还没有数据 —— 跑一次每日流程就会出现"
            return base

        gen = _parse_dt(self.data.get("generated_at"))
        base["generated_at"] = self.data.get("generated_at")
        base["generated_age_hours"] = _age_hours(gen, now)
        base["generated_age_text"] = humanize_age(base["generated_age_hours"])

        raw = [s for s in (self.data.get("schools") or []) if isinstance(s, dict)]
        views = [self.school_view(s, now) for s in raw]
        base["schools"] = views

        ages = [v["age_hours"] for v in views if v["age_hours"] is not None]
        base["oldest_hours"] = max(ages) if ages else None

        # totals：`records` 用数据层的值（它按 school+unit 归属算过，
        # 这是 A 修过的一个坑 —— 只按 school 算会把同一条记录算进每个站点）
        t = self.data.get("totals") or {}
        base["totals"] = {
            "records": t.get("records", sum(v["records"] or 0 for v in views)),
            "schools": len(views),
            "failed_schools": sum(1 for v in views if v["has_failure"]),
            "stale_schools": sum(1 for v in views if v["freshness"] == "stale"),
            "empty_schools": sum(1 for v in views if v["empty"]),
        }

        # 整体 worst = 所有学校 status 里最坏的
        seen = {v["status"] for v in views} - {"unknown"}
        base["worst"] = next((s for s in STATUS_ORDER if s in seen), "unknown")
        return base

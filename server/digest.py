# -*- coding: utf-8 -*-
"""提醒摘要（`GET /api/digest`）。契约见 `contracts/api.md` 第 11 节。

## 一句话

`/api/changes` 说"**这一轮**变了什么"；这里说"**和我有关的**变了什么"。
中间那段就是产品的付费理由。

## 三条硬规则

**1. 只发 `added` / `changed`。**
`reparsed` / `reidentified` / `removed` 一条都不出现 —— 它们的意思是
"**我们的处理变了**"，不是"外面的世界变了"（分类见 `changes.WORLD_CHANGED_BUCKETS`）。
把 `reparsed` 发进提醒 = 告诉用户"浙江大学物理学院有更新"，点进去公告一个字没变。
**提醒只要开始说谎，就没有人再看它**，而"哪条被悄悄改了"正是我们收钱的功能。

**2. 同一公告在区间内变了多次，只提醒一条，取最近那次。**
提醒回答"这条你需要看一眼"，不是流水账。同一条改了三次就提醒三次，
用户第三次就开始忽略提醒 —— 那比漏提醒更糟。

**3. 空结果必须能分清是哪种空。**
"你还没关注"和"关注了但这些天真的没事"是两个完全不同的事实；
"完全没有记录"更不同于"没有变化"（后者是**说谎**，我们不知道有没有变化）。

## 数据源

读**差异报告**，不读公告库 —— 提醒是关于"变化"的：
- `<out>/changes.json`（最近一轮，日期取 `generated_at` 的日期部分）
- `<out>/history/<日期>.json`（每天的差异报告，日期取文件名）

两边**同一个日期时以 `changes.json` 为准**（它更新）。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

from server import watch as watch_mod
from server.changes import WORLD_CHANGED_BUCKETS

# `since` 不给时的默认回看窗口（天）
DEFAULT_WINDOW_DAYS = 7


def _date_of(report, fallback=None):
    """从报告里取日期。取不到就用文件名给的 fallback。"""
    gen = (report or {}).get("generated_at") or ""
    if len(gen) >= 10 and gen[4] == "-" and gen[7] == "-":
        return gen[:10]
    return fallback


def _parse_date(s):
    try:
        return date.fromisoformat(str(s).strip())
    except (ValueError, TypeError, AttributeError):
        return None


class DigestStore:
    """加载差异报告，按关注规则生成提醒。

    `reports` 是 `{日期: 报告dict}`，**不读公告库**。
    """

    def __init__(self, reports, out_path=None, history_dir=None, load_error=None):
        self.reports = reports or {}
        self.out_path = out_path
        self.history_dir = history_dir
        self.load_error = load_error

    # ---------- 加载 ----------

    @classmethod
    def load(cls, changes_path, history_dir=None):
        reports = {}
        errors = []

        # ① 每天的差异报告。日期取文件名 —— 那是 run_daily 归档时定的，比文件内容更权威
        hist = history_dir or (os.path.join(os.path.dirname(changes_path), "history")
                              if changes_path else None)
        if hist and os.path.isdir(hist):
            for name in sorted(os.listdir(hist)):
                if not name.endswith(".json") or name == "index.json":
                    continue
                stem = name[:-5]
                if _parse_date(stem) is None:
                    continue
                try:
                    with open(os.path.join(hist, name), encoding="utf-8") as f:
                        reports[stem] = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    errors.append(f"{name}: {e}")

        # ② 最近一轮的 changes.json。**同一个日期时它赢**（更新）
        if changes_path and os.path.exists(changes_path):
            try:
                with open(changes_path, encoding="utf-8") as f:
                    cur = json.load(f)
                d = _date_of(cur)
                if d:
                    reports[d] = cur
            except (json.JSONDecodeError, OSError) as e:
                errors.append(f"changes.json: {e}")

        return cls(reports, out_path=changes_path, history_dir=hist,
                   load_error="；".join(errors) or None)

    # ---------- 查询用的小工具 ----------

    def dates(self):
        return sorted(self.reports.keys())

    @property
    def earliest(self):
        ds = self.dates()
        return ds[0] if ds else None

    @property
    def latest(self):
        ds = self.dates()
        return ds[-1] if ds else None

    # ---------- 主体 ----------

    def build(self, user_id, watches, since=None, until=None, today=None):
        """生成 digest。所有边界都在这里决定，调用方不用再判。

        参数 `since` / `until` 是字符串或 `None`（`None` 表示用默认窗口）。
        返回值直接就是契约里的响应形状。
        """
        today = today or date.today()
        all_dates = self.dates()
        watches = list(watches or [])

        base = {
            "user_id": user_id,
            "since": since,
            "since_effective": None,
            "until": None,
            "since_clamped": False,
            "since_in_future": False,
            "earliest_available": self.earliest,
            "latest_available": self.latest,
            "watches": [watch_mod.public_view(w) for w in watches],
            "total": 0,
            "items": [],
            "days_covered": [],
            "empty_reason": None,
            "message": "",
            # **结构化的"要注意的事"**，和 message 分开。
            #
            # 为什么不把"日期被夹了"揉进 message：message 是**主要结论**，
            # 会被"命中 N 条"/"没有变更"覆盖 —— 我第一版就是写进 message，
            # 结果被覆盖掉，用户完全看不到自己被夹了（而契约要求必须显式告知）。
            # 而且一条消息里塞两件事，前端没法只挑其中一件显示。
            #
            # 也**不要**让测试去 message 里搜关键词：我那条测试当初靠"命中消息里
            # 恰好也含 2026-10-16"通过，实际上夹取提示早就丢了 ——
            # 和之前给套磁信加 `cited_from` 是同一个教训：自然语言不是数据。
            "notes": [],
            "_note": "提醒只包含 added / changed —— 那是「外面的世界变了」。"
                     "reparsed / reidentified / removed 是我们自己的处理变了，"
                     "不会出现在提醒里（见 api.md 第 11 节）。",
        }

        def note(text):
            base["notes"].append(text)

        # ---- 边界 D：一条记录都没有 ----
        # **必须先判这个。** 没有记录时说"没有变化"是**说谎** ——
        # 我们不知道有没有变化。而且这时候告诉用户"去加关注"是误导：
        # 他加了还是空的，会以为是自己配错了。
        if not all_dates:
            base.update({
                "empty_reason": "no_records_at_all",
                "message": "还没有任何变更记录（每日归档未建立）—— 关注加上了也暂时不会有内容",
            })
            if self.load_error:
                base["_error"] = self.load_error
            return base
        # ---- 定区间 ----
        first, last = all_dates[0], all_dates[-1]
        since_d = _parse_date(since)
        until_d = _parse_date(until)

        # `since` 不给：默认回看 DEFAULT_WINDOW_DAYS 天（到最新记录为止，不是到"今天"——
        # 归档可能落后于今天，用今天当锚点会让默认窗口偶然滑出所有记录）
        if since_d is None:
            last_d = _parse_date(last)
            since_d = last_d - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
            base["since"] = since  # 原样回显（可能是 None）

        if until_d is None:
            until_d = _parse_date(last)

        # ---- 边界 C：since 早于有记录的时间 ----
        first_d = _parse_date(first)
        if since_d < first_d:
            base["since_clamped"] = True
            base["since_effective"] = first
            # 放进 `notes` 而不是 `message`：message 后面会被主要结论覆盖
            note(f"我们只有 {first} 起的记录，已按那天开始算"
                 f"（你请求的是 {since}）")
        else:
            base["since_effective"] = since_d.isoformat()

        # ---- 边界 C2：since 在未来 ----
        if since_d > today:
            base["since_in_future"] = True
            note(f"开始日期 {since_d.isoformat()} 在未来，所以不会有内容")

        base["until"] = until_d.isoformat()

        covered = [d for d in all_dates
                   if since_d <= (_parse_date(d) or since_d) <= until_d]
        base["days_covered"] = covered

        # ---- 边界 A：没有关注 ----
        if not watches:
            base["empty_reason"] = "no_watches"
            base["message"] = "你还没有关注任何学校 / 院系 / 关键词 —— 加一条之后这里就会有内容"
            return base

        # ---- 收集命中 ----
        # 日期**从新到旧**遍历，每个 notice_id 第一次命中就是"最近那次"（硬规则 2）
        items_by_id = {}
        for d in sorted(covered, reverse=True):
            report = self.reports.get(d) or {}
            for bucket in WORLD_CHANGED_BUCKETS:
                for entry in (report.get(bucket) or []):
                    if not isinstance(entry, dict):
                        continue
                    rec = entry.get("record") if bucket == "changed" else entry
                    if not isinstance(rec, dict) or not rec.get("id"):
                        continue
                    nid = rec["id"]
                    if nid in items_by_id:
                        continue          # 已经有更新的那次了
                    hits = watch_mod.matched_by(watches, rec)
                    if not hits:
                        continue
                    items_by_id[nid] = {
                        "notice_id": nid,
                        "title": rec.get("title"),
                        "school": rec.get("school"),
                        "unit": rec.get("unit"),
                        "url": rec.get("url"),
                        "publish_date": rec.get("publish_date"),
                        "deadline": rec.get("deadline"),
                        "change": bucket,
                        "changed_on": d,
                        # `fields` 只在 changed 时有意义（形状同 §10 的 changed[].fields）
                        "fields": (entry.get("fields") if bucket == "changed" else None),
                        "matched_by": hits,
                        "why": "；".join(h["reason"] for h in hits),
                    }

        # 输出顺序：变化日期新的在前；同一天按学校、标题（稳定，不随 dict 顺序）
        items = sorted(items_by_id.values(),
                       key=lambda x: (x["changed_on"], x["school"] or "", x["title"] or ""),
                       reverse=True)
        base["items"] = items
        base["total"] = len(items)

        if items:
            base["message"] = (f"关注 {len(watches)} 条规则，"
                               f"{base['since_effective']} 以来命中 {len(items)} 条")
        else:
            # ---- 边界 B：关注了但区间内没变化 ----
            base["empty_reason"] = "no_changes_in_range"
            base["message"] = (f"关注了 {len(watches)} 条规则，"
                               f"但 {base['since_effective']} 以来没有新增或变更")
        return base


def fetched_at():
    """`generated_at` 用。单独一个函数便于测试替换。"""
    return datetime.now().isoformat(timespec="seconds")

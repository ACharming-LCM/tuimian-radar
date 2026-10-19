# -*- coding: utf-8 -*-
"""更新历史（`GET /api/changes/history`）。

**这是"月度订阅"唯一能拿出来的凭据。**

演示时被问"你怎么证明每天在跑"，答案不该是"我们的脚本能跑"（那是承诺），
而该是"这是过去 N 天的记录"（那是事实）。这个接口就是把那份事实给出去。

数据源：`data/out/history/index.json`（每天一条）+ `data/out/history/<日期>.json`
（当天完整的变更报告，形状和 `changes.json` 一样）。

## 设计取舍

- **`days` 从新到旧排**。用户要看的是"最近发生了什么事"，
  按老到新排会让他先翻到最早那天。
- **`days_with_changes` 单独给一份日期列表**。
  索引里大多数天可能是"0 增 0 改"，用户想看"哪几天真有动静"时不用自己扫一遍。
- **没有 `_note` 字段**。索引文件自己带 `_note`，那是给读文件的人看的说明，
  不是接口响应字段（A 已经澄清过这条：fixture 的标注性 `_` 字段和真实响应字段是两回事）。
  出处说明放在 `source` 里。
"""
from __future__ import annotations

import json
import os

from server.changes import WORLD_CHANGED_BUCKETS

# 有内容的桶。`reparsed`（我们改了抽取代码）也算"这天有动静" ——
# 对用户的意义不同（不是学校改通知），但"日报这天不是空的"这一点相同。
_CHANGE_KEYS = ("added", "changed", "reparsed", "removed", "reidentified")


class HistoryStore:
    def __init__(self, index_path, index, load_error=None):
        self.index_path = index_path
        self.dir = os.path.dirname(index_path) if index_path else ""
        self.index = index
        self.load_error = load_error

    @classmethod
    def load(cls, index_path):
        if not index_path or not os.path.exists(index_path):
            return cls(index_path, None, load_error="还没有更新历史（归档未建立）")
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                return cls(index_path, json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            return cls(index_path, None, load_error=f"更新历史读取失败：{e}")

    def _day_detail(self, day):
        """读某一天的完整报告。缺文件返回 None —— 不猜、不补空对象。"""
        date = day.get("date")
        if not date or not self.dir:
            return None
        path = os.path.join(self.dir, f"{date}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def response(self, detail=False):
        if self.index is None:
            payload = {"total": 0, "days": [], "days_with_changes": [],
                       "days_with_user_changes": [],
                       "generated_at": None,
                       "source": os.path.abspath(self.index_path) if self.index_path else None,
                       "_error": self.load_error}
            return payload

        raw_days = self.index.get("days") or []
        # 从新到旧。日期是 ISO 字符串，直接比大小就是时间顺序。
        days = sorted([d for d in raw_days if isinstance(d, dict) and d.get("date")],
                      key=lambda d: d["date"], reverse=True)

        with_changes = [d["date"] for d in days
                        if any((d.get(k) or 0) for k in _CHANGE_KEYS)]

        # **和 `/api/digest` 同口径的那一份。**
        #
        # `days_with_changes` 回答的是"**我们的管道每天在跑吗**"（运维视角，
        # 所以五个桶全算，含 reparsed）。但用户问"过去 7 天变了什么"时，
        # 他问的是"**外面的世界**动了什么" —— 只有 added / changed。
        #
        # 两个口径混用会互相打脸：history 说"3 天有变化"、digest 说"你的提醒是空的"，
        # 用户会认为有一个是坏的。所以这一份单独给出来，
        # 让"过去 N 天有变化的天数"和"digest 覆盖了哪些天"能对上。
        #
        # 注意 `index.json` 的 days[] 目前**没有 `reparsed` 列**（A 的 run_daily 还没写），
        # 这不影响这里 —— 只读 added / changed，这两列索引里有。
        with_user_changes = [d["date"] for d in days
                             if any((d.get(k) or 0) for k in WORLD_CHANGED_BUCKETS)]

        payload = {
            "total": len(days),
            "days": days,
            "days_with_changes": with_changes,
            "days_with_user_changes": with_user_changes,
            # 索引里最早/最晚各是哪天 —— "我们跑了多久"一眼能看出来
            "first_date": days[-1]["date"] if days else None,
            "last_date": days[0]["date"] if days else None,
            "source": os.path.abspath(self.index_path) if self.index_path else None,
        }
        if detail:
            # 可选：把每一天的完整变更报告也带上（默认不带，索引已经够回答
            # "跑了多久、每天多少条"；完整报告里 added/removed 是精简记录，会大很多）
            payload["detail"] = {d["date"]: self._day_detail(d) for d in days}
        return payload

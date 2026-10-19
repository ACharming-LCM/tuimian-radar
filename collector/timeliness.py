# -*- coding: utf-8 -*-
"""时效性：补发布日、算 `is_fresh` / `age_days`、归一 `admission_year`。

## 三个字段各自解决什么

- `publish_date` 缺失 → 这条公告在列表里**排不了序**，首屏也就永远见不到它。
  实测 1119 条里有 52 条没有发布日，其中 43 条的日期**就在我们自己的缓存里**，
  只是详情页写的标签是「更新时间」，而 `detail.py` 当时只认「更新日期」。
- `is_fresh` / `age_days` → 用户第一屏要的是"今天/本周该看的"，
  这需要把"新"变成字段，而不是让每个下游各自拿 `publish_date` 和 today 相减。
- `admission_year` → `target_year` 原来把"2027 年招生"和"2026 年的日常事务"混在一起
  （2026 只是公告的年份）。`admission_year` 只在 **topic=apply** 时给值：
  它回答的是"这条公告在招哪一届"，而不是"这条公告写于哪一年"。

## 为什么不拿 `crawled_at` 当发布日

`crawled_at` 是**我们第一次抓到它**的时刻。用它冒充发布日，会把 2013 年的论坛公告
标成"今天"，直接违反"每条都新"这条底线。补不到就是补不到，宁可空着并写清原因。

归属：A（数据管道）。
"""
import datetime
import os
import re

import common
import detail

FRESH_DAYS = 30        # ≤30 天算"新"
STALE_DAYS = 180       # >180 天算"旧"（协调者口径）

YEAR_RE = re.compile(r"20(2\d)")


def recover_publish_date(rec):
    """给**缺发布日**的记录找回日期。返回 `(date|None, source)`。

    来源按可信度排序：带标签的详情页元信息 > URL 里的日期 > 正文开头的标签日期。
    **不猜**：三个来源都拿不到就返回 `(None, "unrecoverable")`。
    """
    if (rec.get("publish_date") or "").strip():
        return rec["publish_date"], "existing"

    url = rec.get("url") or ""
    cache = common.cache_path(url)
    if os.path.exists(cache):
        try:
            with open(cache, "rb") as f:
                d = detail.parse_detail(f.read(), url)
        except Exception:                                  # noqa: BLE001
            d = None
        if d and d.get("publish_date"):
            return d["publish_date"], "detail_cache"

    m = re.search(r"/(20\d{2})/(\d{2})(\d{2})/", url)
    if m:
        d = common.iso_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
        if d:
            return d, "url"

    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})",
                  (rec.get("body_text") or "")[:200])
    if m:
        y, mo, dd = m.groups()
        if 1 <= int(mo) <= 12 and 1 <= int(dd) <= 31:
            return f"{y}-{int(mo):02d}-{int(dd):02d}", "body_head"

    return None, "unrecoverable"


def time_fields(rec, today=None):
    """返回 `{is_fresh, age_days, admission_year}`。

    `is_fresh`：`true` = 发布 ≤30 天；`false` = 30~180 天；`"stale"` = >180 天。
    没有发布日时为 `null` —— **不是 false**：不知道有多新，不能说成"不新"。
    """
    today = today or datetime.date.today()
    pub = (rec.get("publish_date") or "").strip()
    age = None
    if pub:
        try:
            age = (today - datetime.date(*map(int, pub[:10].split("-")))).days
        except (ValueError, TypeError):
            age = None
    if age is None:
        is_fresh = None
    elif age <= FRESH_DAYS:
        is_fresh = True
    elif age > STALE_DAYS:
        is_fresh = "stale"
    else:
        is_fresh = False

    # admission_year：只在"报考相关"时给值，且优先取标题里的年份
    # （「2027年硕士研究生招生简章」→ 2027），标题没有才退到 target_year。
    admission_year = None
    if rec.get("topic") == "apply":
        m = YEAR_RE.search(rec.get("title") or "")
        if m:
            admission_year = int("20" + m.group(1))
        elif rec.get("target_year"):
            admission_year = rec["target_year"]

    return {"is_fresh": is_fresh, "age_days": age, "admission_year": admission_year}


def tier_of(rec, today=None):
    """三档时效分层（A24）：`live` / `recent` / `archive`。

    - `live`：发布 ≤30 天 **且** 截止日未过（或没有截止日）
    - `recent`：发布 ≤180 天
    - `archive`：更早

    为什么加这一层而不是回退 400 天过滤：过滤是**删数据**，删了就再也拿不回来；
    分层是**打标签**，前端默认只显示 live + recent，archive 留在"资料库"里可查。
    78.5% 的历史公告不是噪音，是"这条线往年怎么招"的证据。

    没有发布日的记录不算 live（不知道多新就不能说它新），落 recent 也不合适 ——
    直接归 archive，并在 `tier_reason` 里写清是"缺发布日"，别让它冒充新公告。
    """
    today = today or datetime.date.today()
    pub = (rec.get("publish_date") or "").strip()
    age = rec.get("age_days")
    if age is None and pub:
        try:
            age = (today - datetime.date(*map(int, pub[:10].split("-")))).days
        except (ValueError, TypeError):
            age = None
    if age is None:
        return "archive"
    if age <= FRESH_DAYS:
        dl = (rec.get("deadline") or "").strip()
        if not dl:
            return "live"
        try:
            if datetime.date(*map(int, dl[:10].split("-"))) >= today:
                return "live"
        except (ValueError, TypeError):
            return "live"      # 截止日解析不了时按"未知"处理，不因此降档
        return "recent"        # 新发布但已过截止：还在"近期"里，别进资料库
    if age <= STALE_DAYS:
        return "recent"
    return "archive"

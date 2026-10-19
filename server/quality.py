# -*- coding: utf-8 -*-
"""质量与时效字段：`record_quality` / `topic` / `is_fresh` / `age_days`。

这四个字段由数据层产出（`collector/quality.py`、`collector/topic.py`、
`collector/timeliness.py`），**接口层只读，不自己再判一遍** ——
两套判据迟早会打架，这个项目已经栽过四次同样的跟头。

## `is_fresh` / `age_days` 为什么**透传**，不在这里重算

我一开始写的是"按 `publish_date` 现算"，理由是 `HANDOFF-B` §5.3 记过
`freshness_hours` 那个快照坑。**改掉了**，因为数据层的设计意图正相反 ——
`collector/timeliness.py` 的 docstring 原话：

> `is_fresh` / `age_days` → 用户第一屏要的是"今天/本周该看的"，
> 这需要把"新"变成字段，**而不是让每个下游各自拿 `publish_date` 和 today 相减**。

两个理由让透传是对的：

1. **两套时钟会打架。** 接口层现算、数据层也算，两边差一天时没人分得清谁对。
   而这个项目已经栽过四次"同一件事两处实现"（`relevance` 排序、`warnings` 措辞、
   `group_kind`、`LIST_FIELDS`）。
2. **列表 fixture 是逐字段严格比对的。** 现算的话 `age_days` 每天变一次，
   固化的 fixture 从第二天起就与接口不等 —— 门禁会天天红，而红的理由和代码无关。
   （`match.response.json` 就是因为这个才在 `META.json` 里声明了 `_time_sensitive`。）

**代价要说清**：这两个值是**上一次管道运行那一刻**的事实。
管道停了它们就冻住（`/api/freshness` 和 `source.data_age_hours` 会告诉你数据多旧）。
要请求时刻的真值，那是数据层的事，不是接口层偷偷换一套算法。

## 三态纪律

| 字段 | 缺值时 | **不许**变成 |
|---|---|---|
| `record_quality` | `"unknown"` | `"ok"`（把"没标过"说成"检查过、没问题"） |
| `topic` | `null` | `"other"`（`other` 是一个判断结果，"没分类"不是） |
| `is_fresh` | `null` | `false`（不知道有多新，不能说成"不新"） |
| `age_days` | `null` | `0`（同上，0 的意思是"今天发的"） |
"""
from __future__ import annotations

import datetime

# 与 `collector/timeliness.py` 的两个阈值保持一致。**改这里就要改那里**。
# 只在"记录里缺这两个字段"（老数据）时用作兜底计算，判据与数据层同源。
FRESH_DAYS = 30
STALE_DAYS = 180

# `record_quality` 的取值（数据层 `collector/quality.py`）。
QUALITY_OK = "ok"
QUALITY_THIN = "thin"
QUALITY_JUNK = "junk"
# **服务端独有**，数据层永远不会发它：只给"老数据没记这一项"用。
# 不给它的话，老记录必须在三个值里挑一个，而挑任何一个都是编造。
QUALITY_UNKNOWN = "unknown"

# `topic` 的取值（数据层 `collector/topic.py`）。
TOPIC_APPLY = "apply"
TOPIC_NON_APPLY = "non_apply"
TOPIC_OTHER = "other"


def _parse_day(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def time_fields(rec, today=None):
    """`(is_fresh, age_days)`，按 `publish_date` **现算**。

    语义与数据层逐条一致：

    - `is_fresh = True`    发布 ≤ 30 天
    - `is_fresh = False`   30 ~ 180 天
    - `is_fresh = "stale"` > 180 天
    - `is_fresh = None`    没有发布日 / 解析不出来 —— **不是 False**

    第三种值 `"stale"` 是数据层自己定的词（不是布尔），照抄不改：
    把它压成 `False` 就丢掉了"180 天以上"这一档，而 C 的"折叠半年以上"要用它。
    """
    today = today or datetime.date.today()
    pub = _parse_day(rec.get("publish_date"))
    if pub is None:
        return None, None
    age = (today - pub).days
    if age < 0:
        # 发布日在未来 = 数据错。**不截断成 0**（0 是"今天发的"），
        # 也不当成新鲜：当成"不知道"，age_days 如实给负数。
        return None, None
    if age <= FRESH_DAYS:
        return True, age
    if age <= STALE_DAYS:
        return False, age
    return "stale", age


def of(rec, today=None):
    """一条记录的质量/时效四件套。**任何记录都能调用**（老数据不会抛错）。

    `record_quality` / `topic` / `is_fresh` / `age_days` **有就原样透传**；
    只有记录里根本没有这两个时效字段时，才按数据层同一口径兜底计算
    （`today` 只在这个兜底分支里用到）。
    """
    if "is_fresh" in rec or "age_days" in rec:
        is_fresh = rec.get("is_fresh")
        age_days = rec.get("age_days")
    else:
        is_fresh, age_days = time_fields(rec, today)
    return {
        "record_quality": rec.get("record_quality") or QUALITY_UNKNOWN,
        "topic": rec.get("topic"),
        "is_fresh": is_fresh,
        "age_days": age_days,
    }


def is_junk(rec):
    """该不该从列表/推送里**默认**排除。

    只认数据层明确标的 `junk`。缺字段 = 没标过 = **不排除**：
    拿不准宁可多给一条并说明，也不要替用户决定它不存在
    （`HANDOFF-B` 0.2 第 1 条）。
    """
    return (rec.get("record_quality") or "") == QUALITY_JUNK


def junk_reason(rec):
    """给人看的一句"为什么这条是 junk"。数据层给 `quality_hits`，没有就如实说。"""
    hits = rec.get("quality_hits") or []
    label = {
        "title_is_site_name": "标题是站点名/栏目名",
        "body_lt_100": "正文不到 100 字",
        "body_meta_only": "正文只有发布时间/点击数这类元信息",
    }
    parts = [label.get(h, h) for h in hits]
    if parts:
        return "；".join(parts)
    return "数据层标为 junk，但没给出判据"

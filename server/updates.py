# -*- coding: utf-8 -*-
"""事件流的两半：**"今天/本周新增"** 与 **"资料库"**（契约 §20）。

## 为什么要分成两个接口

`events.jsonl` 有 1131 条，其中一大半是补抓时翻出来的历史文章（发布日半年以上）。
把它们和"今天新增"混在一个响应里，那个响应就不能叫「今日更新」——
用户看到 1131 条会说"这哪是今天"，而我们把一个**存档**叫成了**动态**。

（C 的前端已经先做过一版"时效四档"：今天/本周/本月/资料库。这一节把同一件事
搬到接口层，让"哪条算新"只有一个判据 —— 前端那份是**展示分组**，
这里这份是**数据分区**，两者用同一套 30 天口径。）

## 分区的定义（**这两条一起定义，缺一条就会重叠或漏**）

```
updates = 发布日在 `days` 天内（默认 7） 且 未过期
library = 其余全部
```

- **并集 = 事件全集**，**交集 = 0** —— 因为 `library` 是 `updates` 的补集，
  不是"另外查一遍"。两处各写一套筛选条件，迟早会漏一条（漏的那条两个接口都不给，
  而没有人会发现）。门禁里有一条专门验这个恒等式。
- **"未过期"按三态算**：只有 `deadline.state == "found"` 且 `days_left < 0` 才算过期。
  `unknown` / `absent` **不算过期** —— 我们不知道它截没截止，把它踢进资料库
  等于替用户决定"这条没用了"（§14 那条不变量的第四次落点）。
- **`is_historical` 不是"在 library 里"**：一条 20 天前的公告落在 library 里
  （因为它不在本周窗口内），但它**不是历史**。所以每一半都带 `is_historical`，
  按 **30 天 + 未过期** 判，而不是按"它出现在哪个接口里"判。
  否则文档会自相矛盾：同一个词在两个接口里指两件事。
"""
from __future__ import annotations

from datetime import date

from server import config, credibility, quality

# `updates` 的默认窗口（天）。契约 §20.2：默认近 7 天，可放大到 30。
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = quality.FRESH_DAYS          # 30 —— 和"新"的判据同一个数


def days_left_of(deadline, today):
    """距截止天数。解析不了 → `None`（**不是 0**：0 是"今天截止"）。"""
    if not deadline:
        return None
    try:
        return (date.fromisoformat(str(deadline).strip()[:10]) - today).days
    except ValueError:
        return None


def is_expired(event, today):
    """**只有明确读到截止日、且真的过了**才算过期。

    `unknown` / `absent` 一律不算 —— 把"我们没读到"当成"已经过期"
    是这套三态要消灭的那个混淆，在资料库这里的代价是"一条还能报名的公告被归档"。
    """
    if credibility.state_of(event, "deadline") != credibility.FOUND:
        return False
    dl = days_left_of(event.get("deadline"), today)
    return dl is not None and dl < 0


def age_days_of(event, today):
    """发布距今天数。没有发布日 → `None`（**不许当成 0**）。"""
    pub = (event.get("publish_date") or "").strip()
    if not pub:
        return None
    try:
        return (today - date.fromisoformat(pub[:10])).days
    except ValueError:
        return None


def is_historical(event, today):
    """这条算"历史参考"吗？判据 = **30 天以上，或已截止**。

    **不看它出现在哪个接口里。** 见模块 docstring 最后一段。
    """
    if is_expired(event, today):
        return True
    age = age_days_of(event, today)
    if age is None:
        # 没有发布日：**不能说它是历史**（也没有依据说它是新的）。
        return False
    return age > quality.FRESH_DAYS


def reason_of(event, today):
    """给用户看的一句话："为什么它在资料库/为什么它还在这儿"。"""
    if is_expired(event, today):
        dl = days_left_of(event.get("deadline"), today)
        return "已截止 %d 天（仍保留，学校可能延期或补发附件）" % (-dl)
    age = age_days_of(event, today)
    if age is None:
        return "发布日期未读到，无法判断新旧"
    if age > quality.FRESH_DAYS:
        return "%d 天前发布（历史参考）" % age
    return "%d 天前发布，不在本周窗口内" % age


def split(events, today, window_days=DEFAULT_WINDOW_DAYS):
    """把事件分成 `(updates, library)`。**这是唯一的入口**，别在别处再分一次。"""
    window_days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    updates, library = [], []
    for e in events:
        age = age_days_of(e, today)
        fresh_enough = age is not None and 0 <= age <= window_days
        # **脏记录不进"今日新增"**（和列表/推送同一条口径，契约 §19.2）。
        # 它不影响恒等式：library 是 complement，脏记录落进 library 并被标出来，
        # 两个接口的并集仍然是全集。
        if fresh_enough and not is_expired(e, today) and not quality.is_junk(e):
            updates.append(e)
        else:
            library.append(e)
    return updates, library


def item_of(event, today):
    """两个接口共用的条目形状。**同一个形状**，前端才能共用一份渲染。"""
    block = credibility.deadline_display(event)
    block["days_left"] = (days_left_of(event.get("deadline"), today)
                          if block.get("state") == credibility.FOUND else None)
    q = quality.of(event, today)
    return {
        "event_id": event.get("event_id"),
        "notice_id": event.get("notice_id"),
        "type": event.get("type"),
        "title": event.get("title"),
        "school": event.get("school"),
        "unit": event.get("unit"),
        "url": event.get("url"),
        "publish_date": event.get("publish_date"),
        "detected_on": event.get("detected_on"),
        "deadline": block,
        "record_quality": q["record_quality"],
        "topic": q["topic"],
        "is_fresh": q["is_fresh"],
        "age_days": age_days_of(event, today),
        # **两个接口都给这两个键**，含义**完全相同**：
        # `is_historical` 按"30 天 + 未过期"判，不按"出现在哪个接口"判。
        "is_historical": is_historical(event, today),
        "history_reason": reason_of(event, today),
    }

# -*- coding: utf-8 -*-
"""时效字段回归：`is_fresh` / `age_days` / `admission_year`，以及发布日补回。

    cd collector && python tests/test_timeliness.py

要点：`null`（没有发布日）**不是 `false`**。把"不知道"说成"不新"，
和把 `unknown` 说成 `absent` 是同一种错。
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import timeliness      # noqa: E402

FAILED = []
TODAY = datetime.date(2026, 10, 18)


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        期望 {want!r}  实际 {got!r}")
        FAILED.append(name)


def tf(pub, topic="apply", title="2027年硕士研究生招生简章", target_year=2027):
    return timeliness.time_fields({"publish_date": pub, "topic": topic,
                                   "title": title, "target_year": target_year}, TODAY)


print("== is_fresh 三态 ==")
check("今天发布 → true", tf("2026-10-18")["is_fresh"], True)
check("29 天前 → true", tf("2026-09-19")["is_fresh"], True)
check("30 天前 → true（边界内）", tf("2026-09-18")["is_fresh"], True)
check("31 天前 → false", tf("2026-09-17")["is_fresh"], False)
check("180 天前 → false（边界内）", tf("2026-04-21")["is_fresh"], False)
check("181 天前 → stale", tf("2026-04-20")["is_fresh"], "stale")
check("**没有发布日 → null（不是 false）**", tf("")["is_fresh"], None)

print("\n== age_days ==")
check("age_days 正确", tf("2026-10-08")["age_days"], 10)
check("没有发布日 → null", tf("")["age_days"], None)

print("\n== admission_year 归一 ==")
check("2027 招生简章 → 2027",
      tf("2026-09-01", title="清华大学2027年硕士研究生招生简章")["admission_year"], 2027)
check("标题年份优先于 target_year",
      tf("2026-09-01", title="2028年博士招生专业目录", target_year=2027)["admission_year"], 2028)
check("**日常事务不给招生年份**",
      tf("2026-09-01", topic="non_apply", title="2026年秋季学期研究生选课通知")["admission_year"],
      None)
check("标题无年份时退到 target_year",
      tf("2026-09-01", title="硕士研究生招生专业目录", target_year=2027)["admission_year"], 2027)

print("\n== 发布日只在缺失时补，绝不覆盖已有值 ==")
got, src = timeliness.recover_publish_date({"publish_date": "2013-09-27", "url": ""})
check("已有日期原样返回", (got, src), ("2013-09-27", "existing"))
got, src = timeliness.recover_publish_date({"publish_date": "", "url": "https://x.edu/2026/0413/c1/page.htm"})
check("URL 里的日期能补回", (got, src), ("2026-04-13", "url"))
got, src = timeliness.recover_publish_date({"publish_date": "", "url": "https://x.edu/none",
                                            "body_text": "发布时间：2026年5月1日 正文…"})
check("正文开头的标签日期能补回", (got, src), ("2026-05-01", "body_head"))

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

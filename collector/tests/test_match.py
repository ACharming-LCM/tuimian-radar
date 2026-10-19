# -*- coding: utf-8 -*-
"""匹配打分回归测试。不依赖 pytest：

    cd collector && python tests/test_match.py

为什么要它：打分的输入是**抽取出来的字段**，而抽取本来就不完整、偶尔还抽错。
如果没有测试锁住"字段缺失该怎么算""抽错了该怎么算"，这两个错误类型会混在一起，
最后表现为"推荐结果莫名其妙" —— 那种问题查起来最费时间。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import match_preview as M          # noqa: E402

FAILED = []
TODAY = date(2026, 10, 16)


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        期望 {want!r}  实际 {got!r}")
        FAILED.append(name)


def rec(**kw):
    base = {
        "id": "t1", "title": "某公告", "school": "浙江大学", "unit": "物理学院",
        "publish_date": "2026-09-10", "deadline": None, "apply_mode": "推免",
        "target_year": 2027, "degree_types": ["硕士"], "majors": ["物理学"],
        "materials": [], "url": "http://x/", "attachments": [],
        "extract": {"method": "rule", "version": "v2", "confidence": 1.0},
    }
    base.update(kw)
    return base


PROF = {
    "majors": ["物理学"], "wanted_modes": ["推免", "夏令营"], "target_levels": ["硕士", "直博"],
    "preferred_schools": ["浙江大学"], "preferred_units": ["物理学院"],
    "target_entry_year": 2027, "exclude_expired": True,
    "english": {"cet6": 580},
}

print("== 硬过滤 ==")
items, st = M.match([rec(deadline="2026-09-01")], PROF, TODAY)      # 已过期
check("过期记录被丢弃", (st["dropped_expired"], st["scored"]), (1, 0))
items, st = M.match([rec(apply_mode="统考")], PROF, TODAY)          # 方式不符
check("招生方式不符被丢弃", (st["dropped_mode"], st["scored"]), (1, 0))
items, st = M.match([rec(apply_mode=None)], PROF, TODAY)            # 抽不到方式
check("apply_mode 为 null 不丢（67% 抽不到）", st["scored"], 1)
items, st = M.match([rec(deadline="2026-09-01")],
                    {**PROF, "exclude_expired": False}, TODAY)
check("exclude_expired=false 时保留并标已过期", items[0]["bucket"], "已过期")

print("\n== 字段缺失给 0.4，不给 0（否则抽取缺陷会伪装成匹配结论）==")
r = M.score_notice(rec(majors=[], degree_types=[], apply_mode=None), PROF, TODAY)
check("三个字段全缺失", (r[4]["major"], r[4]["level"], r[4]["mode"]), (0.4, 0.4, 0.4))
check("缺失不等于不相关（分数不为 0）", r[0] > 0, True)

print("\n== 抽取与标题矛盾时，不拿它当排序裁判（真实案例：东南大学硕士章程被判成博士）==")
items, _ = M.match([rec(title="东南大学2027年硕士研究生招生章程", degree_types=["博士"])],
                   PROF, TODAY)
check("矛盾时层次降为缺失(0.4)而不是 0", items[0]["parts"]["level"], 0.4)
check("并且给出明确警告", any("层次判断可能有误" in w for w in items[0]["warnings"]), True)
items, _ = M.match([rec(title="东南大学2027年博士研究生招生简章", degree_types=["硕士"])],
                   {**PROF, "target_levels": ["博士", "直博"]}, TODAY)
check("反向矛盾同样处理", items[0]["parts"]["level"], 0.4)
items, _ = M.match([rec(title="某某招生通知", degree_types=["博士"])], PROF, TODAY)
check("真不匹配(标题无矛盾)才给 0", items[0]["parts"]["level"], 0.0)

print("\n== 材料缺口提醒：只提硬门槛、放软语气 ==")
items, _ = M.match([rec(materials=["托福", "雅思"])], PROF, TODAY)
check("托福提醒出现", any("托福" in w for w in items[0]["warnings"]), True)
items, _ = M.match([rec(materials=["托福"])],
                   {**PROF, "english": {"cet6": 580, "toefl": 100}}, TODAY)
check("已有托福则不提醒", any("托福" in w for w in items[0]["warnings"]), False)
items, _ = M.match([rec(materials=["四级"])], PROF, TODAY)
check("六级在身不提醒四级（已从词表移除）",
      any("四级" in w for w in items[0]["warnings"]), False)
check("语气放软，不再说'你的画像里没有'",
      all("你的画像里没有" not in w for w in items[0]["warnings"]), True)

print("\n== bucket 分档 ==")
for dl, want in [("2026-10-20", "立即行动"), ("2026-11-01", "重点关注"),
                 ("2026-12-30", "可规划"), (None, "需人工确认")]:
    check(f"deadline={dl}", M.bucket_of(dl, TODAY)[0], want)

print("\n== 置信度降权：低置信不许排到高置信前面 ==")
hi = M.score_notice(rec(extract={"method": "rule", "version": "v2", "confidence": 1.0}),
                    PROF, TODAY)[0]
lo = M.score_notice(rec(extract={"method": "rule", "version": "v2", "confidence": 0.0}),
                    PROF, TODAY)[0]
check("低置信被降权", lo < hi, True)
check("最多降到 70%", round(lo / hi, 2), 0.70)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

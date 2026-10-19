# -*- coding: utf-8 -*-
"""主题分类回归：`apply` / `non_apply` / `other`。

    cd collector && python tests/test_topic.py

每个用例都对着一次真实错判写，别再删：
「四、六级」写法带顿号、「推免预报名」里含"毕业生"、讲坛公告、团队介绍、机构名标题。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import topic      # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        期望 {want!r}  实际 {got!r}")
        FAILED.append(name)


def t(title, body="正文" * 200):
    return topic.classify({"title": title, "body_text": body})[0]


print("== 报考相关 ==")
check("招生简章", t("北京大学2027年硕士研究生招生简章"), "apply")
check("推免", t("南京大学2027年接收推荐免试研究生工作办法"), "apply")
check("报考点公告", t("2027年硕士研究生招生考试报考点公告"), "apply")
check("夏令营", t("力学学院2026年全国优秀大学生夏令营报名通知"), "apply")
check("**「推免预报名」里含“毕业生”，不许被判成毕业事务**",
      t("华中师范大学2027年接收优秀应届本科毕业生免试攻读研究生预报名通知"), "apply")
check("学制与培养费用", t("南京大学2027年硕士研究生学制与培养费用"), "apply")

print("\n== 在读 / 录取之后 ==")
check("**四、六级（带顿号）**", t("关于做好2026年下半年全国大学英语四、六级考试报名工作的通知"), "non_apply")
check("拟录取名单公示", t("新媒体研究院2026年博士招生拟录取公示名单"), "non_apply")
check("答辩名单", t("南开大学2026-2027学年第一学期申请答辩学术博士平台评审名单"), "non_apply")
check("竞赛", t("关于举办郑州大学第八届研究生人工智能创新大赛的通知"), "non_apply")
check("选课", t("关于2026-2027学年第一学期研究生选课工作的通知"), "non_apply")
check("新生体检", t("关于2026级研究生新生体检安排的通知"), "non_apply")

print("\n== 其他 ==")
check("讲坛公告", t("关于张少伟教授做客我校研究生“名师名家讲坛”的公告"), "other")
check("团队介绍", t("经管 | 金融科技与监管创新团队"), "other")
check("招聘", t("物理学院现代光学研究所招聘1名劳动合同制职工"), "other")
check("机构名标题", t("北京大学数学学院"), "other")
check("新闻", t("福建理工大学办学130周年高质量发展大会召开"), "other")

print("\n== 可解释性 ==")
_, hits = topic.classify({"title": "北京大学2027年硕士研究生招生简章", "body_text": ""})
check("apply 必须带命中词", bool(hits), True)
check("命中词是标题里的那个", "招生" in hits, True)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

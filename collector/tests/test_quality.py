# -*- coding: utf-8 -*-
"""质量分级回归：`junk` / `thin` / `ok` 的边界，以及**不许误伤正文**。

    cd collector && python tests/test_quality.py

为什么值得单独测：这个判据决定"哪些记录不进首屏"。判松了脏记录漏进首屏，
判紧了真公告被藏起来 —— 两种都比不判更糟。特别是 `body_meta_only`：
第一版用"有元信息且没有句号"就判 junk，实测把 100 条记录打成 junk（真正的两倍），
其中还有「化学学院有悠久的历史…」这种正经正文。所以边界必须有断言。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quality      # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        期望 {want!r}  实际 {got!r}")
        FAILED.append(name)


def q(title, body, school="某大学", unit="研究生院"):
    return quality.classify({"title": title, "school": school, "unit": unit,
                             "body_text": body})[0]


print("== 站点名 / 栏目名 ==")
check("标题就是站点名 → junk", q("天津大学研究生招生网", "正文" * 200), "junk")
check("标题等于 unit → junk", q("研究生院", "正文" * 200, unit="研究生院"), "junk")
check("正常标题不受影响", q("天津大学2027年硕士研究生招生简章", "正文" * 200), "ok")

print("\n== 按正文长度分档 ==")
check("99 字 → junk", q("某通知", "字" * 99), "junk")
check("100 字 → thin（边界）", q("某通知", "字" * 100), "thin")
check("299 字 → thin", q("某通知", "字" * 299), "thin")
check("300 字 → ok（边界）", q("某通知", "字" * 300), "ok")

print("\n== 只有元信息的正文 ==")
check("发布时间+浏览次数 → junk", q("某通知", "发布时间：2026-09-29浏览次数：13"), "junk")
check("加分享尾巴 → junk", q("某通知", "发布时间：2026-10-15\n点击数：18300\n分享至："), "junk")
check("**长正文里出现「来源」不算 junk**（第一版在这里栽过）",
      q("某通知", "化学学院有悠久的历史，可追溯至1929年。" + "正文" * 150), "ok")
check("**有句子的短正文不是 meta-only**",
      q("某通知", "各位考生：请于10月20日前提交材料。" + "字" * 120), "thin")

print("\n== 判据可追溯 ==")
_, hits = quality.classify({"title": "天津大学研究生招生网", "school": "天津大学",
                            "unit": "研究生院", "body_text": "发布时间：2026-09-29"})
check("junk 一定带判据", bool(hits), True)
check("站点名判据在", "title_is_site_name" in hits, True)
check("过短判据在", "body_lt_100" in hits, True)

print("\n== 标题回退（A6）==")
TJU_BODY = ("字体大小：\n"
            "天津大学关于开展2027年硕博连读研究生选拔的通知\n"
            "来源：  发布时间：2026-10-12\n"
            "为选拔优秀硕士研究生攻读博士学位，现面向本校学籍在读硕士生开展选拔工作。" * 6)
t, src = quality.title_fallback({"title": "天津大学研究生招生网", "body_text": TJU_BODY})
check("从正文首行捞回真标题", t, "天津大学关于开展2027年硕博连读研究生选拔的通知")
check("来源标记为 fallback", src, "fallback")
check("正文太短不回退（<300 字）",
      quality.title_fallback({"title": "天津大学研究生招生网", "body_text": "字体大小：\n某通知"})[1],
      "none")
check("正文里找不到像标题的行 → none",
      quality.title_fallback({"title": "x", "body_text": "一、报考条件\n二、报名方式\n" * 40})[1],
      "none")

print("\n== issues/011：机构名标题不再一刀切成 junk ==")
NAV_BODY = "首页\n系史\n历史阶段\n招生动态\n博士招生\n硕士招生\n专业介绍\n联系我们\n"
check("导航列表 → junk",
      quality.classify({"title": "北京大学社会学系", "school": "北京大学", "unit": "研究生院",
                        "body_text": NAV_BODY})[0], "junk")
check("正文有真标题 → 不判 junk（等 retitle 换掉标题）",
      quality.classify({"title": "北京大学马克思主义学院", "school": "北京大学",
                        "unit": "研究生院",
                        "body_text": "北京大学马克思主义学院2026年硕士招生拟录取公示名单自即日起公示7日。" * 20})[0],
      "ok")
check("**能捞回标题**",
      quality.title_from_body({"title": "北京大学马克思主义学院",
                               "url": "https://x.invalid/none",
                               "body_text": "北京大学马克思主义学院2026年硕士招生拟录取公示名单自即日起公示7日。"}),
      "北京大学马克思主义学院2026年硕士招生拟录取公示")
check("字段名不当标题（「（1）网上报名时间：…」）",
      quality.title_from_body({"title": "北京大学数学学院", "url": "https://x.invalid/none",
                               "body_text": "（1）网上报名时间：2025年10月20日9:00至11月19日17:00。"}),
      None)

print("\n== issues/011 附带：编号导航页 ==")
NUM_BODY = ("001信息与通信工程学院\n002电子科学与工程学院\n003材料与能源学院\n"
            "004机械与电气工程学院\n005光电科学与工程学院\n006自动化工程学院\n")
check("001信息与通信工程学院 + 正文全是编号院系 → junk",
      quality.classify({"title": "001信息与通信工程学院", "school": "电子科技大学",
                        "unit": "研究生院", "body_text": NUM_BODY})[0], "junk")
check("普通编号开头但正文是公告 → 不误伤",
      quality.classify({"title": "001信息与通信工程学院关于2027年硕士招生的通知",
                        "school": "电子科技大学", "unit": "研究生院",
                        "body_text": "正文" * 300})[0], "ok")

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

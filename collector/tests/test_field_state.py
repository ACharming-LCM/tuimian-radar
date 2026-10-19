# -*- coding: utf-8 -*-
"""字段可信度三态（found / absent / unknown）的契约测试。不依赖 pytest：

    cd collector && python tests/test_field_state.py

为什么需要它：三态存在的**唯一理由**是区分两句话 ——
    「这公告里就没有截止日」 vs 「我们没读到截止日」
对用户这两句是反的。所以这个文件的重点**不是**"值抽得对不对"，
而是**"我们有没有把'不知道'说成'没有'"**。那是本项目一直在防的那类错误，
而且它看起来像正常运行。

数据侧的证据链（真数据实测）：
    selector 167 / generic 23 / none 6
    其中 selector 里 9 条命中的是元信息框 —— 正文是 0 字或「2026-10-08浏览次数：10」
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fields        # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        期望 {want!r}  实际 {got!r}")
        FAILED.append(name)


def check_true(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n        {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


def st(how, body, **kw):
    """按 crawl.py 的方式算三态：值留空，只看正文质量对结论的影响。"""
    return fields.field_state(
        {"deadline": None, "majors": []}, body=body,
        body_len=len(body), body_how=how, attachments=[], **kw)


print("== body_state：正文是怎么取到的 ==")
# `file` 是同义的 ok —— 整条公告本身就是一个 PDF/DOCX，正文从那份文件里抽出来
check("file（公告本身就是文件）→ ok", fields.body_state("file", 0), "ok")
check("selector + 长正文 → ok", fields.body_state("selector", 1000), "ok")
check("selector + 刚好到门槛 → ok",
      fields.body_state("selector", fields.ABSENT_MIN_BODY_CHARS), "ok")
check("selector + 差一个字 → missing",
      fields.body_state("selector", fields.ABSENT_MIN_BODY_CHARS - 1), "missing")
check("generic（通用兜底）→ generic", fields.body_state("generic", 5000), "generic")
check("none（没取到）→ missing", fields.body_state("none", 0), "missing")
check("未知来源按最保守处理 → missing", fields.body_state("???", 5000), "missing")

print("\n== 元信息框：长度够也不构成证据 ==")
# 实测 4 条复旦的正文就是这一句，17 字，会被长度门槛挡住。
META_SHORT = "2026-10-08浏览次数：10"
# 而这件**单件**元信息框长 116 字：也被 150 的门槛挡住了。
# 那还测它干什么？——测的是**判据本身**有没有生效（而不是碰巧被长度挡住），
# 所以下面专门造一个"够长、但依然是元信息框"的样本。
META_ONE = ("发布时间：2026-10-08 浏览次数：10 发布者：研究生院 审核：范修林、薛晶晶 "
            "终审：余学功 来源：研究生招生网 编辑：某某 责编：某某 "
            "更新时间：2026-10-09 访问量：31275 字体：大 中 小 打印 关闭 分享")
check("短元信息串 → missing", st("selector", META_SHORT)["body"], "missing")
check_true("单件元信息框的长度确实没到门槛（实测形态就是这样）",
           116 < fields.ABSENT_MIN_BODY_CHARS, "116 字 < 150")

# **够长的元信息框**：这里才真正在测内容判据。
# 必须落在 `ABSENT_MIN_BODY_CHARS < 长度 <= META_ONLY_MAX_CHARS` 这个窗口里 ——
# 这是个很窄的窗口，所以直接断言它（下面那条），免得以后有人改了门槛、
# 这条用例悄悄变成在测长度而不是在测内容判据。
META_LONG = (META_ONE + " 联系电话：021-88888888 电子邮箱：yzb@fudan.edu.cn "
             "办公地址：上海市杨浦区邯郸路220号 邮政编码：200433")
check_true("样本落在「够长、但没超元信息上限」的窗口里（否则测的不是内容判据）",
           fields.ABSENT_MIN_BODY_CHARS < len(META_LONG) <= fields.META_ONLY_MAX_CHARS,
           f"{len(META_LONG)} 字，窗口 ({fields.ABSENT_MIN_BODY_CHARS}, {fields.META_ONLY_MAX_CHARS}]")
check("**够长的元信息框照样是 missing**（全靠内容判据挡下）",
      st("selector", META_LONG)["body"], "missing")
check_true("它没有句读，这正是判据的依据",
           not fields.SENTENCE_PUNCT_RE.search(META_LONG), META_LONG[:40])

# 真实故障的那 9 条正文（0～44 字）。全部没有句读 —— 这是判据的依据。
for bad in ("", "2026-10-08浏览次数：10",
            "发布者：史杨审核：范修林、薛晶晶终审：余学功发布时间：2025-03-28浏览次数：13",
            "发布时间：2026-08-22浏览次数：31275"):
    check(f"实测问题正文仍然是 missing：{bad[:24]!r}", st("selector", bad)["body"], "missing")

# **反向**：有内容的短公告不能被误判成元信息框 —— 误伤这个方向更严重
# （它会把一条真公告的结论从 absent 退成 unknown，甚至更糟）。
# 试过用"剥掉元信息后残留字数"判，实测元信息框残留 34 字、真实短正文残留 54 字，
# **没有干净的分界线**，所以改用句读判。下面这些必须保住。
for good in ("各位考生： 欢迎报考我校2027年硕士研究生。现将我校2027年硕士研究生招生专业目录公布如下，供广大考生查阅。",
             "注：本资料仅供参考，最终招生办法以教育部2025年研究生招生相关政策和学校2025年博士和硕士研究生招生简章为准。",
             "一、报名时间：2026年10月5日至10月25日。二、报名网址：研招网。",
             "发布时间：2026-08-22\n各位考生：现公布复试名单。"):
    check_true(f"有内容的短正文不被误判：{good[:22]!r}",
               not fields._body_is_meta_only(good), good)

# 长正文里零星出现「审核：」「来源：」是正常的，判据**不覆盖**长正文。
LONG_WITH_META = ("现公布我校2027年硕士研究生招生复试录取工作办法，请各位考生仔细阅读。" * 6
                  + "审核：研究生院 来源：招生办")
check_true("长正文里的元信息字段不影响判断（判据只挡短正文）",
           len(LONG_WITH_META) > fields.META_ONLY_MAX_CHARS
           and not fields._body_is_meta_only(LONG_WITH_META), f"{len(LONG_WITH_META)} 字")

print("\n== 核心不变量：不能把「不知道」说成「没有」 ==")
# 这是整个特性的意义所在。下面每一条都是"我们没读到"，绝不许输出 absent。
for how, body, why in (
        ("generic", "统一身份认证平台 请登录后访问 用户名 密码 登录", "兜底抓来的是登录页"),
        ("none", "", "根本没取到正文"),
        ("selector", "2026-10-08浏览次数：10", "选择器命中的是元信息框"),
        ("selector", "短", "正文太短，不足以当证据"),
        ("selector", META_LONG, "只剩元信息字段"),
):
    r = st(how, body)
    check(f"[{why}] deadline=unknown", r["deadline"], "unknown")
    check(f"[{why}] majors=unknown", r["majors"], "unknown")
    check(f"[{why}] attachments=unknown", r["attachments"], "unknown")
    check_true(f"[{why}] 没有任何字段被说成 absent",
               "absent" not in r.values(), f"{r}")

print("\n== 扫描版附件：页面看着正常，但正文我们真的没读到 ==")
GOOD = "各位考生：现将我校2027年硕士研究生招生简章公布如下，请仔细阅读。" * 8
r = fields.field_state({"deadline": None, "majors": []}, body=GOOD,
                       body_len=len(GOOD), body_how="selector", attachments=[],
                       pdf_stats={"scanned": 1})
check("有扫描版附件时→ deadline 退回 unknown", r["deadline"], "unknown")
check("有扫描版附件时→ body 仍是 ok（正文本身没问题）", r["body"], "ok")
r2 = fields.field_state({"deadline": None, "majors": []}, body=GOOD,
                        body_len=len(GOOD), body_how="selector", attachments=[],
                        pdf_stats={"scanned": 0})
check("没有扫描版附件时→ 才敢说 absent", r2["deadline"], "absent")

print("\n== 正文可信时，absent 是敢说的话 ==")
r = st("selector", GOOD)
check("正文够长够真 → deadline=absent", r["deadline"], "absent")
check("正文够长够真 → majors=absent", r["majors"], "absent")
check("正文够长够真 → attachments=absent", r["attachments"], "absent")
check("正文够长够真 → body=ok", r["body"], "ok")

print("\n== found 优先于一切：有值就是 found ==")
# 抽到值的时候，正文质量如何都不影响"这条有这个字段"
for how in ("selector", "generic", "none"):
    r = fields.field_state({"deadline": "2026-04-28", "majors": ["物理学"]}, body="x" * 500,
                           body_len=500, body_how=how, attachments=[{"url": "u"}])
    check(f"[{how}] 有值 → deadline=found", r["deadline"], "found")
    check(f"[{how}] 有值 → majors=found", r["majors"], "found")
    check(f"[{how}] 有值 → attachments=found", r["attachments"], "found")

print("\n== 不传 body_how / body_len 时要从 body 现算（两条路必须一致）==")
# crawl 有 detail 的来源信息，离线回放只有存下来的正文。
# 两条路算出来必须一致 —— **同一个策略写在两个地方迟早分叉**，本项目栽过 4 次。
built = fields.field_state({"deadline": None, "majors": []}, body=GOOD)
explicit = fields.field_state({"deadline": None, "majors": []}, body=GOOD,
                              body_len=len(GOOD), body_how="selector")
check("省略参数 == 显式传 selector", built, explicit)
check("省略参数时不会误判成 generic", built["body"], "ok")

print("\n== 形状：字段齐全、取值都在枚举里 ==")
r = st("selector", GOOD)
check("六个键一个不少", sorted(r), ["attachments", "body", "contact", "deadline", "majors", "materials"])
check_true("三态取值合法",
           all(r[k] in ("found", "absent", "unknown")
               for k in ("deadline", "majors", "attachments")), f"{r}")
check_true("body 取值合法", r["body"] in ("ok", "generic", "missing"), r["body"])

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

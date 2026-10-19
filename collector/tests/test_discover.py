# -*- coding: utf-8 -*-
"""栏目判定（classify_board）的单元测试。

    cd collector && python tests/test_discover.py

**为什么补这个文件**：`classify_board` 是整个「栏目发现」的核心 ——
它决定了我们去抓哪些栏目，抓错了就是**整块噪音**或者**整块漏抓**。
但在此之前它只有一份评估脚本（`tools/eval_discovery.py`），**没有单元测试**。
评估脚本测的是"在一批真实站点上整体表现如何"，改一个词之后要跑几分钟才能知道；
而单元测试盯的是"具体某个栏目名的判定有没有变"，一秒出结果。

用例全部来自真实抓到的栏目名（见 `data/out/board-verdicts.md` 的人工核对记录）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import discover      # noqa: E402

FAILED = []
N = 0


def case(own, full, expect, why):
    global N
    N += 1
    got, reason = discover.classify_board(own, full)
    if got != expect:
        FAILED.append((own, full, expect, got, reason, why))
        print(f"  FAIL  {own!r}\n        期望 {'保留' if expect else '丢弃'}，"
              f"实际 {'保留' if got else '丢弃'}（{reason}）\n        理由：{why}")
    else:
        print(f"  PASS  {'保留' if expect else '丢弃'}  {own[:44]}")


print("== 强制否决（只看链接自身文字）==")
case("国际招生", "国际招生", False, "「国际招生」里有'招生'，但全是留学生 —— 必须压过正向词")
case("导师资格、招生、津贴拨付、评优相关", None, False,
     "这个'招生'指导师的招生资格，不是考生的招生")
case("在职攻读硕士学位", None, False, "在职不适用应届推免生")

print("\n== 正向词压过弱否决 ==")
case("本科生教学 消息公告 培养方案 本校推免 实践教学 文件汇编 表格下载", None, True,
     "栏目名里有'推免' → 正向词压过'教学/表格/下载'这些弱否决词。实测这个栏目里的"
     "「选拔本科生赴西北大学」「卓越培养项目报名」都是真招生信息")
case("专业学位招生", None, True, "弱否决词刻意用组合词（'学位管理'而非'学位'），就是为了别误杀这个")
case("招生培养", None, True, "'培养'是弱否决词，但'招生'压过它")

print("\n== 科研动态类（本次新增的负向词）==")
case("研究进展 研究机构 学术活动 公共平台 通知", None, False,
     "整块是论文报道。实测这里抓进 3 条 Science/Nature Materials/PRL，"
     "而且因为按日期排，它们顶到了公告列表第一位")
case("研究生教育发展&成果展示", None, False, "校级课题优秀成果展示，不是招生")
case("学术报告", None, False, "学术讲座排期")

print("\n== 泛栏目名（这一层管不了，要如实知道）==")
case("通知公告", None, True,
     "**故意保留**：这个栏目名看不出内容，实测里面既有'国庆带值班表'"
     "也有可能有招生通知。栏目名看不出来的事情，不该在这一层猜 —— "
     "那种情况要靠记录级排序解决")

print("\n== 保守保留 ==")
case("硕士研究生", None, True, "光秃秃的'硕士研究生'，无正无负 → 保守保留")
case("招生信息", None, True, "正向词")
case("通知事项", None, True, "泛栏目名，保守保留")

print("\n== URL 形状判据：栏目 vs 文章（**这条差点悄悄毁掉整个维普系**）==")
# 背景：为了支持路径式栏目（上交/清华/川大那种没有扩展名的），我放宽了 LIST_HINT，
# 同时加了一道 DETAIL_LIKE 反向闸（"文章不是栏目"）。
# **第一版我把 `/\d{5,}/` 当成"文章 id"** ——
# 可维普 CMS 的**栏目** URL 就是 5 位栏目号：`/50916/list.htm`。
# 于是浙大材料那一页 136 个栏目全被这道闸杀掉，该站记录从 6 条掉到 0 条。
# 别的学校没炸，只是因为它们的栏目名是字母（`/ysjs/list.htm`）——
# **一个判据只在部分站点上出错时，最容易瞒过去。**
# 所以这里把两类 URL 都钉住。
def url_case(u, want_article, why):
    global N
    N += 1
    got = bool(discover.DETAIL_LIKE.search(u.split("?")[0]))
    tag = "PASS" if got == want_article else "FAIL"
    print(f"    {tag}  {'文章' if got else '栏目'}  {why}")
    if got != want_article:
        FAILED.append(why)


url_case("https://mse.zju.edu.cn/50916/list.htm", False, "维普栏目：5 位栏目号不能被当文章")
url_case("https://mse.zju.edu.cn/51226/list.htm", False, "维普栏目：同上")
url_case("http://physics.zju.edu.cn/qstp_73988/list.htm", False, "维普栏目：字母名")
url_case("https://mse.zju.edu.cn/2023/0414/c51226a2742945/page.htm", True,
         "维普文章：/YYYY/MMDD/ + c…a…")
url_case("https://x.edu.cn/info/1040/6762.htm", True, "info 路径是文章")
url_case("https://x.edu.cn/article/12345", True, "article 路径是文章")
url_case("https://x.edu.cn/zsjz/bsszs", False, "路径式栏目（现代站点）")
url_case("https://yzb.sjtu.edu.cn/zsjz/bsszs", False, "路径式栏目：上交")
url_case("https://gradschool.ustc.edu.cn/column/9", False, "column/9 是栏目不是文章")

print(f"\n{'=' * 56}")
if FAILED:
    print(f"❌ {len(FAILED)} / {N} 失败")
    sys.exit(1)
print(f"✅ {N} 项全过")

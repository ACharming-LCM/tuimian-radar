# -*- coding: utf-8 -*-
"""导师名单解析回归测试。不依赖 pytest：

    cd collector && python tests/test_faculty.py

为什么要它：导师层是"专业契合的实验室"这个卖点唯一的支撑数据。
它的解析比公告更脆 —— 公告至少有排版，师资页是**图文卡片**：
姓名在 h3、职称在没标签的 <p>、字段值在 <span> 里。
任何一个位置变了，结果就是"抓到 0 人"或者"抓到的方向是乱码"，
而这两种失败在页面上都看不出来（页面明明有人）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common        # noqa: E402
import faculty       # noqa: E402

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


# 结构照抄实测到的浙大物理学院师资卡片（li.list-item > a > div.info > h3 + p）
FIXTURE = """<html><head><title>凝聚态物理研究所</title></head><body>
<ul>
  <li class="list-item">
    <a href="https://person.zju.edu.cn/zhangsan">
      <div class="pic"><img src="/x.jpg" alt=""></div>
      <div class="info">
        <h3>张三</h3>
        <p>教授 | 博士生导师</p>
        <p class="over">E-mail&nbsp; :&nbsp; <span>teacher05@zju.edu.cn</span></p>
        <p class="over">研究方向&nbsp; :&nbsp; <span>量子光学 · 冷原子物理</span></p>
        <p class="over">个人主页&nbsp; :&nbsp; <span class="home">https://person.zju.edu.cn/zhangsan</span></p>
      </div>
    </a>
  </li>
  <li class="list-item">
    <a href="https://person.zju.edu.cn/lisi">
      <div class="info">
        <h3>李四</h3>
        <p>研究员</p>
        <p class="over">研究领域：超导材料、强关联电子系统；拓扑物态</p>
        <p class="over">联系电话：0571-88888888</p>
      </div>
    </a>
  </li>
  <li class="list-item">
    <a href="/notice.htm"><div class="info"><h3>关于招聘博士后的通知</h3></div></a>
  </li>
  <li class="list-item">
    <a href="/x.htm"><div class="info"><h3>研究方向</h3></div></a>
  </li>
</ul></body></html>"""

print("== 基本结构（照抄真页面的卡片结构）==")
recs = faculty.parse_faculty_page(FIXTURE.encode("utf-8"), "http://x/list.htm",
                                  "浙江大学", "zju", "物理学院")
check("只解析出 2 位真人（标题型/非姓名型被跳过）", len(recs), 2)
r = recs[0]
check("姓名", r["name"], "张三")
check("职称", r["title"], "教授")
check("博导标记", r["is_phd_supervisor"], True)
check("邮箱", r["email"], "teacher05@zju.edu.cn")
check("研究方向已切分", r["research_areas"], ["量子光学", "冷原子物理"])
check("个人主页", r["homepage"], "https://person.zju.edu.cn/zhangsan")
check("group 来自页面 title", r["group"], "凝聚态物理研究所")
check("school/unit", (r["school"], r["unit"]), ("浙江大学", "物理学院"))

print("\n== 字段标签的变体（不同学校写法不同，不能只认一种）==")
r2 = recs[1]
check("「研究领域」也认", r2["research_areas"],
      ["超导材料", "强关联电子系统", "拓扑物态"])
check("顿号/逗号/分号都能切", len(r2["research_areas"]), 3)
check("电话", r2["phone"], "0571-88888888")
check("不是博导时不硬编码 True", r2["is_phd_supervisor"], None)
check("没有个人主页时为 None", r2["homepage"], "https://person.zju.edu.cn/lisi")

print("\n== 不能把非姓名条目当成导师（宁缺毋滥）==")
names = [r["name"] for r in recs]
check_true("「关于招聘博士后的通知」被排除", "关于招聘博士后的通知" not in names)
check_true("「研究方向」这种字段名被排除", "研究方向" not in names)

print("\n== 坏输入不能炸掉整批 ==")
check("空字节返回空列表", faculty.parse_faculty_page(b"", "http://x/", "A", "a", "U"), [])
check("非 HTML 返回空列表",
      faculty.parse_faculty_page(b"\x00\x01\x02binary", "http://x/", "A", "a", "U"), [])
check("没有 h3 的页面返回空列表",
      faculty.parse_faculty_page(b"<html><body><ul><li><a href='/x'>a</a></li></ul></body></html>",
                                 "http://x/", "A", "a", "U"), [])

print("\n== 真实缓存页（离线，走 data/raw）==")
real_url = "http://physics.zju.edu.cn/ysjs/list.htm"
if os.path.exists(common.cache_path(real_url)):
    raw, _ = common.fetch(real_url)
    real = faculty.parse_faculty_page(raw, real_url, "浙江大学", "zju", "物理学院")
    check_true("至少解析出 5 位导师", len(real) >= 5, f"实际 {len(real)}")
    with_res = [r for r in real if r["research_areas"]]
    check_true("研究方向覆盖率 > 80%", len(with_res) / max(len(real), 1) > 0.8,
               f"{len(with_res)}/{len(real)}")
    check_true("所有记录都有姓名", all(r["name"] for r in real))
    check_true("id 唯一", len({r["id"] for r in real}) == len(real))
    print(f"        样例：{real[0]['name']} / {real[0]['title']} / "
          f"{real[0]['research_areas']}")
else:
    print("  跳过（无缓存，先跑一次 crawl_faculty.py）")

print("== 路径 B：带表头的表格（复旦物理那种，页面上 <li> 数量是 0）==")
# 回归用例。原解析器只认 `li > h3`（浙大 person.zju.edu.cn 模板），
# 换一所学校就整页 0 人 —— 复旦物理师资页是一张带表头的 <table>，
# 实测换路径前 0 人、换后 23 人且研究方向 / 邮箱 100%。
# 注意两件事，都是从真页面抄来的：
#   · 姓名格前面有一列**首字母索引**（A/B/…），表头那行也有这一列，所以按列号对得上
#   · 两字名被排版成「陈 唯」（中间垫一个空格），裸 NAME_RE 匹配不上
TABLE_FIXTURE = """<html><head><title>师资队伍</title></head><body>
<table class="wp_editor_art_paste_table"><tbody>
<tr><td></td><td>姓名</td><td>职称</td><td>研究方向</td><td>邮箱</td><td>办公地点</td></tr>
<tr><td>A</td><td>安正华</td><td>教授</td><td>纳米热成像与非厄米物理</td><td>teacher06@fudan.edu.cn</td><td>S208</td></tr>
<tr><td>B</td><td>陈 唯</td><td>副教授</td><td>凝聚态理论</td><td>teacher07@fudan.edu.cn</td><td>S301</td></tr>
<tr><td>C</td><td>师资队伍</td><td>导航词</td><td>不该进</td><td>x@x.cn</td><td>-</td></tr>
</table></body></html>"""

trecs = faculty.parse_faculty_page(TABLE_FIXTURE.encode("utf-8"),
                                   "https://phys.fudan.edu.cn/7476/list.htm",
                                   "复旦大学", "fudan", "物理学系")
check("抽出 2 位真人（导航词被 NOT_A_NAME 挡掉）", len(trecs), 2)
check("姓名（首字母索引已剥掉）", trecs[0]["name"], "安正华")
check("两字名带空格也能认（陈 唯）", trecs[1]["name"], "陈 唯")
check("职称按列映射", trecs[0]["title"], "教授")
check("研究方向按列映射", trecs[0]["research_areas"], ["纳米热成像与非厄米物理"])
check("邮箱按列映射", trecs[0]["email"], "teacher06@fudan.edu.cn")
check("group 来自页面 title", trecs[0]["group"], "师资队伍")

print("\n== 职称词串到姓名格时不能当成导师 ==")
# 实测这个 bug：`助理教授` 在 TITLE_WORDS 里却不在 NOT_A_NAME 里，
# 于是表格里职称格串到姓名格的那一行被当成一位叫「助理教授」的导师。
# 复旦物理因此解析出 23 人、去重后只剩 22 —— **靠对不上账才发现**。
# 修法不是往黑名单里再加一个词，而是让黑名单自动并入 TITLE_WORDS（见 faculty.py）。
TITLE_AS_NAME = """<html><head><title>师资队伍</title></head><body><table>
<tr><td>姓名</td><td>职称</td></tr>
<tr><td>张三</td><td>教授</td></tr>
<tr><td>助理教授</td><td>助理教授</td></tr>
</table></body></html>"""
trecs2 = faculty.parse_faculty_page(TITLE_AS_NAME.encode("utf-8"), "https://x/", "复旦", "f", "u")
check("职称词不会被当成姓名", [r["name"] for r in trecs2], ["张三"])

print("\n== 两条路径的优先级：A 有结果就不走 B（保基线）==")
# 浙大的卡片页里也塞一张表，A 出了人就不该被 B 覆盖
MIX = FIXTURE.replace("</body>", "<table><tr><td>姓名</td><td>职称</td></tr>"
                                "<tr><td>安正华</td><td>教授</td></tr></table></body>")
mrecs = faculty.parse_faculty_page(MIX.encode("utf-8"), "https://x/", "浙江大学",
                                  "zju", "物理学院")
check("路径 A 胜出（没有被表格路径换掉）", [r["name"] for r in mrecs], ["张三", "李四"])

print("\n== 路径 C：姓名和职称粘连、没有分隔符（浙大材料那种）==")
# 回归用例。浙大材料的列表页把姓名和职称直接拼在一起：
#     朱笑东高级实验师  卢洋藩高级实验师  吴春春副研究员  张欣特聘研究员
# `NAME_RE` 要求整段正好 2-4 个汉字，所以**一个都认不出来**，
# 而我一开始把这判成"JS 渲染"——判错了，名字就在静态 HTML 里。
# 两个必须验证的点：
#   · 「吴春春副研究员」不能切成「吴春春副」+「研究员」（名字要懒惰匹配）
#   · 「张欣特聘研究员」不能降级成「研究员」（职称取最长的命中词）
CONCAT_FIXTURE = """<html><head><title>副高职教师</title></head><body>
<div><a href="https://person.zju.edu.cn/zhuxd">朱笑东高级实验师</a></div>
<div><a href="/2023/0414/c51226a2742945/page.htm">吴春春副研究员</a></div>
<div><a href="https://person.zju.edu.cn/0619753">张欣特聘研究员</a></div>
<div>学院概况</div>
</body></html>"""
crecs = faculty.parse_faculty_page(CONCAT_FIXTURE.encode("utf-8"), "https://mse.zju.edu.cn/51226/list.htm",
                                  "浙江大学", "zju", "材料科学与工程学院")
check("抽出 3 人（「学院概况」这种导航词不算）", len(crecs), 3)
check("姓名不贪婪：吴春春 而不是 吴春春副", [r["name"] for r in crecs],
      ["朱笑东", "吴春春", "张欣"])
check("职称取最长命中：「副研究员」不降级成「研究员」",
      [r["title"] for r in crecs], ["高级实验师", "副研究员", "特聘研究员"])
check("顺带取到个人主页（二级抓取要用）", crecs[0]["homepage"],
      "https://person.zju.edu.cn/zhuxd")
check("相对链接被补全", crecs[1]["homepage"],
      "https://mse.zju.edu.cn/2023/0414/c51226a2742945/page.htm")

print("\n== 二级抓取：从个人主页补研究方向 ==")
# 个人页上 li>h3 和 table 数量**都是 0**，A/B/C 三条路径一个都摸不到，
# 所以这一层只能单独认「研究方向：…」那段正文。
# 坑：那段文字后面常常直接撞上内联 JS，不切掉会混进一大段代码。
PERSON_FIXTURE = """<html><head><title>朱笑东-浙江大学个人主页</title></head><body>
<div>研究方向：半导体材料的表界面、杂质缺陷、测试技术 var pageUid = '0014900'; var apiRelation = '/api/x';</div>
<div>邮箱：teacher08@zju.edu.cn</div>
</body></html>"""
areas, email, pt = faculty.person_research(PERSON_FIXTURE.encode("utf-8"))
check("研究方向切分正确", areas, ["半导体材料的表界面", "杂质缺陷", "测试技术"])
check_true("内联 JS 被切掉了", not any("var" in a for a in areas), str(areas))
check("顺带抽到邮箱", email, "teacher08@zju.edu.cn")

print("\n== 标签在、但字典里没有时，不能整条丢掉 ==")
# 实测大连理工 faculty.dlut.edu.cn 的卡片长这样，**曾经整页 0 人**：
#     <h3>董婉</h3><p>单位：物理学院</p><p>职称：助理研究员</p>
# 姓名、结构、个人主页链接全都在，`li > h3` 也命中了，
# 但 `职称` 当时不在 LABELS 里 → 那一行被静默丢掉 → title_text 始终 None
# → 第二道闸（"卡片不可能只有姓名"）把**整页真人一起拒掉**。
LABEL_FIXTURE = """<html><head><title>教师主页</title></head><body><ul>
<li><a href="http://faculty.dlut.edu.cn/dongwan/zh_CN/index.htm">
  <div><h3>董婉</h3><p>单位：物理学院</p><p>职称：助理研究员</p></div></a></li>
<li><a href="http://faculty.dlut.edu.cn/sanglin/zh_CN/index.htm">
  <div><h3>桑琳</h3><p>单位：物理学院</p><p>职称：副研究员</p></div></a></li>
</ul></body></html>"""
lrecs = faculty.parse_faculty_page(LABEL_FIXTURE.encode("utf-8"),
                                  "http://faculty.dlut.edu.cn/x.jsp", "大连理工大学",
                                  "dlut", "教师主页")
check("整页不再 0 人", len(lrecs), 2)
check("单位/职称标签行被认出来", [r["name"] for r in lrecs], ["董婉", "桑琳"])
# 职称**不能降级**：曾经因为 _li_card_people 里留着一份"首个命中"的旧逻辑，
# 「助理研究员」显示成「研究员」。现在统一走 _make_rec（取最长命中）。
check("职称不降级：助理研究员 / 副研究员",
      [r["title"] for r in lrecs], ["助理研究员", "副研究员"])
check("个人主页仍在", lrecs[0]["homepage"],
      "http://faculty.dlut.edu.cn/dongwan/zh_CN/index.htm")

print("\n== 研究方向的三个过滤器（都是被真实脏数据逼出来的）==")
# ① 句子碎片：`研究方向为污水…等。近年来主持国家自然科学基金项目5项`
#    不截断的话，项目句子会混进方向，直接污染"专业契合"匹配
check("去掉开头的「为」", faculty._clean_areas("为污水厌氧生物处理技术"), ["污水厌氧生物处理技术"])
# ② 网址
check("丢掉含网址的片段", faculty._clean_areas("金属材料、请移步个人英文主页 http://x.cn"),
      ["金属材料"])
# ③ 导航词密集：`论文 科研项目 教学活动 授课信息 学习资料` 是**空格分隔**的，
#    SPLIT_RE 不切空格 → 整串成了一个 22 字的"方向"，逐个词比黑名单逮不到
check("导航词密集的整串丢掉",
      faculty._clean_areas("论文 科研项目 教学活动 授课信息 学习资料"), [])
check("正常方向不受影响", faculty._clean_areas("量子光学、冷原子物理"),
      ["量子光学", "冷原子物理"])

print("\n== 个人页：研究方向在行文里时要截到句子边界 ==")
PROSE_FIXTURE = """<html><head><title>乔森</title></head><body>
<div>乔森，教授。研究方向为污水厌氧生物处理技术、生物脱氮除磷技术等。近年来主持国家自然科学基金项目5项。</div>
</body></html>"""
pa, pe, pt = faculty.person_research(PROSE_FIXTURE.encode("utf-8"))
check_true("项目句子没混进方向",
           pa and not any("国家自然科学基金" in a for a in pa), str(pa))
check_true("方向本身留住了", pa and any("污水" in a for a in pa), str(pa))

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

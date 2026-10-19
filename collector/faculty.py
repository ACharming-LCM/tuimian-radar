# -*- coding: utf-8 -*-
"""导师名单解析：从院系"师资队伍/师资力量"栏目页抽出导师记录。

**为什么这一层值得单独做**：产品最核心的承诺是"各校和用户**专业契合**的实验室招生简章"。
但校级研究生招生网只发《招生章程》《拟录取名单》——它告诉你"怎么报名"，
**不告诉你"这个方向要不要我"**。方向和导师这一层只能从院系师资页拿。

实测浙大物理学院：**关键字段全在列表页上**，不用进个人主页：
    <li class="list-item">
      <h3>唐孝威</h3>
      <p>研究员 | 博士生导师</p>
      <p class="over">E-mail : <span>teacher01@zju.edu.cn</span></p>
      <p class="over">研究方向 : <span>分子动力学模拟 · 脑功能成像 · 软物质</span></p>
      <p class="over">个人主页 : <span class="home">https://person.zju.edu.cn/tangxiaowei</span></p>
    </li>
好处是**请求数少一个数量级**，也不受个人主页那套 JS 渲染影响
（实测 person.zju.edu.cn/liuchen 只返回 577 字节，主体是异步加载的）。

归属：A。入口签名（B 不要改）：
    parse_faculty_page(raw, page_url, school, school_slug, unit) -> [faculty, ...]
"""
import hashlib
import re
from datetime import datetime

import common

# 栏目名里混着两种东西，必须分开 —— C 做导师页时被这个绊住了：
# 浙大物理的师资导航同时挂了「中国科学院院士」「特聘教授」（这是**身份**）
# 和「凝聚态物理研究所」「浙江近代物理中心」（这是**机构**）。
# schema 里 `group` 定义成"栏目/研究所"，两种混在一个字段里，界面上分不出组。
#
# 处理：`group` 保持原样（忠实于来源栏目名，不篡改），另加 `group_kind` 标明性质。
# **顺序要紧**：「中国科学院院士」里同时含「学院」，先判身份才不会被机构抢走。
GROUP_KINDS = (
    ("身份", ("院士", "特聘", "教授", "学者", "人才", "计划", "杰青", "优青",
              "讲师", "研究员", "博导", "导师")),
    ("机构", ("研究所", "中心", "实验室", "学院", "学系", "系", "团队", "课题组")),
)


def group_kind(name):
    for kind, words in GROUP_KINDS:
        if any(w in (name or "") for w in words):
            return kind
    return "其他"


def group_kind_test():
    """自检：这几条是实测里真实出现的栏目名。"""
    cases = {"中国科学院院士": "身份", "特聘教授": "身份",
             "凝聚态物理研究所": "机构", "浙江近代物理中心": "机构",
             "聚变理论与模拟中心": "机构", "师资队伍": "其他"}
    return {k: (group_kind(k), v) for k, v in cases.items()}


# 字段标签 -> 我们的字段名。中英文都收，因为不同学校写法不同。
LABELS = {
    "e-mail": "email", "email": "email", "邮箱": "email", "电子邮箱": "email",
    "研究方向": "research", "研究领域": "research", "主要研究方向": "research",
    "个人主页": "homepage", "主页": "homepage", "网址": "homepage",
    "电话": "phone", "联系电话": "phone",
    # 职称/单位写成标签行，是很常见的一种写法（大连理工 Tsites 教师主页就是）。
    # **不加这两条会导致整页 0 人**，见下面 _li_card_people 里那段注释。
    "职称": "title", "职务": "title", "专业技术职务": "title",
    "单位": "unit_label", "院系": "unit_label", "所属单位": "unit_label",
    "部门": "unit_label", "所在单位": "unit_label",
}
# 研究方向的切分符。浙大用"·"，别人家用顿号/逗号/分号。
SPLIT_RE = re.compile(r"[·、,，;；/|]\s*")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:0\d{2,3}[-\s]?\d{7,8}|1[3-9]\d{9})(?!\d)")
# 姓名：2-4 个汉字。放宽到含少数民族姓名的中点。
NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}(?:[·・][\u4e00-\u9fa5]{1,6})?$")
# 两字名在表格里常被排版成「陈 唯」「丁 鲲」（中间垫一个空格对齐）。
# 实测复旦物理有 5 个这样的人名，NAME_RE 匹配不上就直接整页丢人。
NAME_FLEX_RE = re.compile(
    r"^(?:[\u4e00-\u9fa5]{2,4}|[\u4e00-\u9fa5][\s\u3000][\u4e00-\u9fa5])"
    r"(?:[·・][\u4e00-\u9fa5]{1,6})?$")

# 师资表头 → 我们的字段。**这是通用性最高的一条路径**：
# 很多高校的师资页就是一张带表头的表格（复旦物理就是：姓名|职称|研究方向|邮箱|办公地点）。
# 认表头比猜 DOM 结构可靠得多，而且一次就给全了方向和邮箱。
COL_LABELS = {
    "姓名": "name", "名字": "name", "教师姓名": "name", "导师姓名": "name",
    "职称": "title", "专业技术职务": "title", "职务": "title", "导师类别": "title",
    "研究方向": "research", "研究领域": "research", "主要研究方向": "research",
    "主要研究领域": "research", "方向": "research", "研究内容": "research",
    "邮箱": "email", "电子邮箱": "email", "e-mail": "email", "email": "email",
    "邮件": "email",
    "电话": "phone", "联系电话": "phone", "办公电话": "phone",
    "办公地点": "office", "办公室": "office", "办公地址": "office",
    "个人主页": "homepage", "主页": "homepage",
}

# 坑（已踩）：光靠"2-4 个汉字"判断姓名是不够的 —— 「研究方向」「电子邮箱」这种
# 字段名恰好也是 4 个汉字，会被当成导师抓进来。测试里那条
# <h3>研究方向</h3> 就是这么混进去的。所以必须再加一张黑名单。
NOT_A_NAME = {
    "研究方向", "研究领域", "个人主页", "电子邮箱", "邮箱地址", "联系电话", "联系方式",
    "办公电话", "职称职务", "性别年龄", "学历学位", "个人简介", "更多信息", "查看详情",
    "教授", "研究员", "副教授", "讲师", "院士", "博士生导师", "硕士生导师", "课题组",
    # 导航词。评估报告里点过这个坑：只按"2-4 个汉字"判断姓名，
    # 会把「师资队伍」「学院概况」这类菜单项算成导师（实测中科大那个页面因此报 48 人，
    # 而里面一个老师都没有）。这里用**精确匹配**，不会误伤真名。
    # 注意黑名单天生列不全 —— 所以真正靠得住的是结构判据（见 parse_faculty_page 的两道闸）。
    "师资队伍", "师资力量", "教师队伍", "教师名录", "教师简介", "人才队伍",
    "人才培养", "学院简介", "学院概况", "学院机构", "院系介绍", "院系概况",
    "现任领导", "党政班子", "党委组成", "团委组成", "院办人员", "机构设置",
    "两院院士", "教学名师", "学科建设", "学科方向", "科学研究", "科研动态",
    "学术活动", "学术报告", "研究机构", "国际合作", "招生就业", "招生信息",
    "学生工作", "党建工作", "党建活动", "支部风采", "班级活动", "学研两会",
    "联系我们", "下载专区", "教工之家", "系友专栏", "招贤纳士", "为您服务",
    "永远怀念", "消息公告", "通知公告", "实验室", "研究方向", "教师风采",
}
TITLE_WORDS = ("教授", "研究员", "副教授", "副研究员", "讲师", "助理教授",
               "院士", "高级工程师", "工程师", "博士后",
               # 以下几条是实测缺的：`TITLE_WORDS` 里没有「特聘研究员」，
               # 于是「张欣特聘研究员」被归一成「研究员」——**职位被降级**。
               # 归一化取的是最长的命中词，所以长词必须在表里才不会被短词盖住。
               "特聘研究员", "特聘副研究员", "助理研究员", "高级实验师", "实验师",
               "高级工程师", "教授级高级工程师", "副研究馆员", "研究馆员")
# **职称词自动并入黑名单。**
# 坑（已踩）：`助理教授` 在 TITLE_WORDS 里却不在 NOT_A_NAME 里，于是表格里
# 职称格串到姓名格的那一行被当成了一位叫「助理教授」的导师 ——
# 复旦物理因此多出一条假记录（解析出 23、去重后只剩 22，数字对不上才发现）。
# 不再手工维护两份名单：**能当职称的词就不可能是人名**，并一次永绝。
NOT_A_NAME |= set(TITLE_WORDS)


def _split_label(text):
    """把「研究方向 : 分子动力学模拟 · 脑功能成像」拆成 (label, value)。"""
    m = re.match(r"^\s*([^:：]{1,12})\s*[:：]\s*(.*)$", text or "")
    if not m:
        return None, (text or "").strip()
    return m.group(1).strip().lower(), m.group(2).strip()


# 研究方向里**绝不该出现**的东西。踩过的三种：
#   · 网址：`和成果介绍、请移步个人英文主页： http…`（大连理工 王波）
#   · 页面导航词：`论文 科研项目 教学活动 授课信息 学习资料`（大连理工 夏明洋）——
#     那是个人主页的**侧栏栏目名**被当成方向段落抓进来了
#   · 句子碎片：`为污水厌氧生物处理技术…`（"研究方向**为**…"里的"为"）
# 坑（已踩）：`SPLIT_RE` 里含 `/`，会把 `http://x.cn` **切成碎片** ——
# 只过滤 `http` 前缀的话，残片 `x.cn` 会漏过去当成一个"研究方向"。
# 所以守卫要能认**裸域名**，不能只认带协议的。
AREA_NOT = re.compile(
    r"http|www\.|@|请移步|点击|更多|\b[\w-]+\.(?:cn|com|net|org|edu|io|gov)\b", re.I)
AREA_NAV = {"论文", "科研项目", "教学活动", "授课信息", "学习资料", "招生信息",
            "科研成果", "获奖信息", "专利", "著作", "指导学生", "社会兼职",
            "教育经历", "工作经历", "个人简介", "研究方向", "研究领域"}
AREA_LEAD = re.compile(r"^[为和是及与、，,：:]+")
# 一段文字里**密集出现导航词**，那它就是个人主页的侧栏栏目名，不是研究方向。
# 坑（已踩）：`论文 科研项目 教学活动 授课信息 学习资料` 是**空格分隔**的，
# 而 `SPLIT_RE` 不切空格 → 整串成了一个 22 字的"方向"，逐个词比对黑名单根本逮不到。
# 所以判据改成"命中几个"，不是"整体等于哪个"。
AREA_NAV_MIN = 3


def _clean_areas(s):
    out = []
    for part in SPLIT_RE.split(s or ""):
        p = AREA_LEAD.sub("", part.strip(" .．。:：")).strip()
        if not (2 <= len(p) <= 30):
            continue
        if AREA_NOT.search(p) or p in AREA_NAV:
            continue
        if sum(1 for w in AREA_NAV if w in p) >= AREA_NAV_MIN:
            continue
        out.append(p)
    # 去重但保序
    return list(dict.fromkeys(out))


def _table_people(doc):
    """从**带表头的表格**里抽人。返回 [{列名: 值}]。

    为什么加这条路径（实测数据支撑）：原解析器只认 `li > h3`，那是浙大
    `person.zju.edu.cn` 的模板。换一所学校就整页 0 人 ——
    复旦物理的师资页是一张 `<table>`，页面上 `<li>` 数量是 **0**，
    于是 30 多号人一个都抽不出来。而那张表**带表头**：
        姓名 | 职称 | 研究方向 | 邮箱 | 办公地点
    认表头是结构无关的，而且一次就把方向和邮箱都给全了。
    """
    out = []
    for table in doc.xpath("//table"):
        rows = table.xpath(".//tr")
        if len(rows) < 2:
            continue
        # 表头行：在前三行里挑"命中列名最多、且至少命中 2 列"的那一行
        head, hmap = None, {}
        for r in rows[:3]:
            m = {}
            for i, c in enumerate(r.xpath("./td|./th")):
                t = " ".join(common.text(c).split()).strip().lower()
                key = COL_LABELS.get(t)
                if key and key not in m.values():
                    m[i] = key
            if len(m) > len(hmap):
                head, hmap = r, m
        if len(hmap) < 2:
            continue                       # 不像表头，跳过
        for r in rows:
            if r is head:
                continue
            cells = r.xpath("./td|./th")
            if not cells:
                continue
            vals = {}
            for i, c in enumerate(cells):
                if i in hmap:
                    vals[hmap[i]] = " ".join(common.text(c).split()).strip()
            # 姓名格里常有首字母索引（"A安正华"、"B车静光"），去掉
            name = re.sub(r"^[A-Za-z]{1,3}(?=[\u4e00-\u9fa5])", "", vals.get("name", "")).strip()
            if not name or not NAME_FLEX_RE.match(name) or name in NOT_A_NAME:
                continue
            vals["name"] = name
            out.append(vals)
        if out:
            break                             # 一张够用的表就够了
    return out


def _li_card_people(raw, page_url, school, school_slug, unit, group=None):
    """路径 A：`li > h3` 卡片（浙大 `person.zju.edu.cn` 模板）。

    **这一段保持原样不动** —— 它是唯一跑通的基线（浙大物理 30 人、方向 100%），
    任何"顺手改一改"都可能把它弄坏，而那正是我们唯一的可用样本。
    """
    doc = common.parse(raw)
    if doc is None:
        return []

    # 栏目名当 group：同一院系内"凝聚态物理研究所""聚变理论与模拟中心"要能区分
    if group is None:
        t = doc.xpath("//title")
        group = common.text(t[0]) if t else None

    out = []
    for li in doc.xpath("//li"):
        h3 = li.xpath(".//h3")
        if not h3:
            continue
        name = common.text(h3[0])
        if not NAME_RE.match(name) or name in NOT_A_NAME:
            continue

        fields, title_text, homepage = {}, None, None
        for p in li.xpath(".//p"):
            raw_txt = common.text(p)
            label, value = _split_label(raw_txt)
            if label is None:
                # 没有标签的那一行通常是职称行：「研究员 | 博士生导师」
                if not title_text and len(value) <= 40:
                    title_text = value
                continue
            key = LABELS.get(label)
            if key:
                # 值优先取 <span>，避免把标签本身混进去
                spans = p.xpath(".//span")
                val = common.text(spans[0]) if spans else value
                if key == "title":
                    # 职称写成标签形式（"职称：助理研究员"）时也要喂给 title_text ——
                    # 否则第二道闸（"卡片不可能只有姓名"）会以为这张卡片没信息，把真人一起拒掉
                    if not title_text:
                        title_text = val
                else:
                    fields[key] = val
                continue
            # **标签在、但字典里没有**（如"单位：物理学院"）—— 不能就这么整条丢掉。
            # 坑（已踩）：大连理工 faculty.dlut.edu.cn 的卡片是
            #     <h3>董婉</h3><p>单位：物理学院</p><p>职称：助理研究员</p>
            # 姓名、结构、个人主页链接全都有，`li > h3` 也命中了，
            # 但两行都没进 LABELS，于是 title_text 始终是 None，
            # **第二道闸把整页真人一起拒掉 —— 解析结果 0 人。**
            # 兜底：这一行的值仍然可以当职称行用。
            if not title_text and value and len(value) <= 40:
                title_text = value
            continue

        # 兜底：标签没写但正文里有邮箱
        body_txt = common.text(li)
        if not fields.get("email"):
            m = EMAIL_RE.search(body_txt)
            if m:
                fields["email"] = m.group(0)
        if not fields.get("phone"):
            m = PHONE_RE.search(body_txt)
            if m:
                fields["phone"] = m.group(0)
        # 兜底：研究方向可能写成"研究领域"或干脆没标签
        if not fields.get("research"):
            m = re.search(r"研究方向\s*[:：]?\s*(.{2,120})", body_txt)
            if m:
                fields["research"] = m.group(1)

        a = li.xpath(".//a[@href]")
        if a:
            homepage = common.norm_url(page_url, a[0].get("href"))
        if fields.get("homepage"):
            homepage = fields["homepage"] or homepage

        # 第二道闸：真实的师资卡片不可能只有姓名。
        # 注意**不能**用 href 判断 —— 每张卡片都有链接，包括杂质条目。
        # 只认结构内容：邮箱 / 研究方向 / 电话 / 职称行。
        if not (fields.get("email") or fields.get("research")
                or fields.get("phone") or title_text):
            continue

        # **统一走 _make_rec**，别再在这里抄一份记录构造。
        # 坑（已踩）：这里原来有一份自己的 title 逻辑，是"`TITLE_WORDS` 里首个命中"的旧版；
        # 我把 `_make_rec` 改成"取最长命中"（好让「副研究员」不降级成「研究员」）时，
        # **只改了副本、没改这份原件** —— 于是大连理工的「助理研究员」照样显示成「研究员」。
        # 两处算同一个字段，迟早会分叉。所以这里删掉重复实现。
        out.append(_make_rec(name, title_text, fields, homepage, group, page_url,
                             school, school_slug, unit))
    return out


def rec_hash(rec):
    """记录的内容哈希。二级抓取补了信息之后必须重算，否则变更检测看不出差异。"""
    sig = "|".join([rec.get("name") or "", rec.get("title") or "",
                    rec.get("email") or "", ",".join(rec.get("research_areas") or [])])
    return "sha1:" + hashlib.sha1(sig.encode("utf-8")).hexdigest()


def _make_rec(name, title_text, fields, homepage, group, page_url,
              school, school_slug, unit):
    """两条解析路径共用的记录构造，保证字段口径一致。"""
    title = None
    if title_text:
        # **取最长的那个职称词**，不是列表里最先命中的那个。
        # 坑（已踩，和路径 C 的正则同一个毛病）：`TITLE_WORDS` 里「研究员」排在
        # 「副研究员」前面，而 `"研究员" in "副研究员"` 为真 —— 于是
        # 「吴春春副研究员」被归一成「研究员」、「张欣特聘研究员」也被降级成「研究员」。
        # 职位被降级不只是难看：`is_phd_supervisor` 这类判断都读同一个字段。
        hits = [w for w in TITLE_WORDS if w in title_text]
        title = max(hits, key=len) if hits else title_text[:20]
    sig = f"{name}|{title_text}|{fields.get('email')}|{fields.get('research')}"
    return {
        "id": f"{school_slug}-{hashlib.sha1((unit + name).encode()).hexdigest()[:8]}-"
              f"{hashlib.sha1(name.encode()).hexdigest()[:6]}",
        "school": school,
        "school_slug": school_slug,
        "unit": unit,
        "group": group,
        "group_kind": group_kind(group),
        "name": name,
        "title": title,
        "is_phd_supervisor": ("博士生导师" in (title_text or "")) or None,
        "is_master_supervisor": ("硕士生导师" in (title_text or "")) or None,
        "email": fields.get("email"),
        "phone": fields.get("phone"),
        "research_areas": _clean_areas(fields.get("research") or ""),
        "homepage": homepage,
        "source_url": page_url,
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
        "content_hash": "sha1:" + hashlib.sha1(sig.encode("utf-8")).hexdigest(),
    }


def person_research(raw, page_url=""):
    """从**个人主页**里抽研究方向（二级抓取用）。返回 (research_areas, email, title)。

    为什么必须单独写：实测 `person.zju.edu.cn` 的个人页上
    `li>h3` 卡片数和 `table` 数**都是 0** —— A/B/C 三条路径一个都摸不到，
    但正文里明明写着「研究方向：半导体材料的表界面、杂质缺陷、测试技术」。
    所以这一层只认那段文字。

    坑（已踩）：`研究方向` 后面紧跟的正文常常直接撞上内联 JS，例如
        …测试技术 var pageUid = '0014900'; var apiRelation = '/api/relation?sign=…'
    不切掉的话，研究方向里会混进一大段代码。
    """
    doc = common.parse(raw)
    if doc is None:
        return [], None, None
    body = doc.xpath("//body")
    if not body:
        return [], None, None
    txt = " ".join(common.text(body[0]).split())

    areas = []
    # 研究方向这一段到哪里结束？**真正的终止符是"下一个字段标签"**。
    # 坑（已踩两次，两个方向）：
    #   · 浙大 person.zju.edu.cn：后面直接撞上内联 JS（`…测试技术 var pageUid = …`）
    #   · 北大化学：后面跟着 `办公室：A626 办公室电话：… 电子信箱：…`
    # 第一版我只认 `\s+var\s` 或行尾，于是北大化学**整条匹配失败** ——
    # 那一页明明写着「研究方向: 物理化学，生物物理，分子光谱」。
    # 所以改成：先贪婪取一段，再在**第一个字段标签**处切断。
    STOP = re.compile(
        r"\s*(?:办公室|办公地点|办公电话|电话|电子信箱|电子邮箱|邮箱|E-?mail|"
        r"职称|职务|学术简历|教育背景|工作经历|学习经历|代表性|讲授|招生|通信地址|"
        r"地址|课题组网站|个人主页|var\b|function\b)")
    m = re.search(r"(?:主要)?研究方向[：:\s]*(.{2,220})", txt)
    if not m:
        m = re.search(r"研究领域(?:及兴趣)?[：:\s]*(.{2,220})", txt)
    if m:
        v = STOP.split(m.group(1))[0]
        # **在句子边界截断。**
        # 坑（已踩）：有些学校的个人页是**简介段落**，研究方向嵌在行文里，例如大连理工
        #     「…研究方向为污水处理技术、生物脱氮除磷技术等。近年来主持国家自然科学基金项目5项」
        # 不在句号处截断的话，`research_areas` 里会混进「近年来主持国家自然科学基金项目5项」
        # 这种**根本不是研究方向**的散文句子 —— 它会直接污染"专业契合"的匹配。
        # 保留第一个句号之前的部分（要求至少有 6 个字，免得把"…等。"这种过短的也切了）。
        cut = re.split(r"[。！？；;]", v, maxsplit=1)
        if len(cut) > 1 and len(cut[0].strip()) >= 6:
            v = cut[0]
        areas = _clean_areas(v)[:8]        # 一个人不可能有几十个方向，多了多半是抓错

    email = None
    me = EMAIL_RE.search(txt)
    if me:
        email = me.group(0)

    title = None
    hits = [w for w in TITLE_WORDS if w in txt[:400]]
    if hits:
        title = max(hits, key=len)
    return areas, email, title


def _concat_people(doc, page_url):
    """路径 C：**姓名和职称连在一起、中间没有任何分隔符**。

    实测来源（浙大材料）：
        朱笑东高级实验师  卢洋藩高级实验师  吴春春副研究员  张欣特聘研究员 …
    `NAME_RE` 要求整段正好是 2-4 个汉字，所以这种写法**一个都认不出来**。
    我一开始把它判成"JS 渲染"——**判错了**，名字就在静态 HTML 里。

    两个容易写错的点：
      1. **名字要用懒惰匹配（最短优先）**。贪婪匹配会在
         「吴春春副研究员」上先吞掉 4 个字得到「吴春春副」+「研究员」——
         多认一个字，人名就错了。懒惰匹配先试 2 个字，失败再往后试。
      2. **职称词要长的排前面**，否则「副教授」会被「教授」抢先匹配。
    """
    # 长的排前面：特聘副研究员 > 副研究员 > 研究员
    titles = ("特聘研究员", "特聘副研究员", "高级实验师", "高级工程师", "助理研究员",
              "助理教授", "副研究员", "副教授", "研究员", "实验师", "工程师",
              "教授", "讲师", "院士", "博士后")
    pat = re.compile(r"^([\u4e00-\u9fa5]{2,4}?)(" + "|".join(titles) + r")$")

    out, seen = [], set()
    for el in doc.iter():
        if not isinstance(el.tag, str) or el.tag in ("script", "style", "title"):
            continue
        if len(el):                      # 只看叶子节点：避免把父节点的整段文字反复匹配
            continue
        t = " ".join((el.text or "").split())
        if not t or len(t) > 20:         # 一个人一条，正常不会超过 20 字
            continue
        m = pat.match(t)
        if not m:
            continue
        name = m.group(1)
        if name in NOT_A_NAME or name in seen:
            continue
        seen.add(name)
        # 顺手把这一条对应的链接取出来 —— 二级抓取要靠它去个人主页拿研究方向。
        # 链接可能就在自己身上，也可能是父节点里的 <a>（浙大材料是后者）。
        href = None
        par = el.getparent()
        if par is not None:
            if par.tag == "a" and par.get("href"):
                href = par.get("href")
            else:
                for x in par.iter("a"):
                    if x.get("href"):
                        href = x.get("href")
                        break
        out.append({"name": name, "title": m.group(2),
                    "homepage": common.norm_url(page_url, href) if href else None})
    return out


def _name_link_people(doc, page_url):
    """路径 D：**内容区里的人名链接**（很多学校的师资页就是一堆 `<a>张三</a>`）。

    判据不是"文字像人名" —— 那样会把「学院概况」「师资队伍」这类导航词算成人
    （评估报告里踩过：一个只 6 个人的页面被数出 38 个"人"）。
    真正的判据是 **href 指向哪里**：

    | href 形状 | 是什么 |
    |---|---|
    | `person.<host>/xxx` · `/info/…` · `/page.htm` · `/YYYY/MMDD/c…a…/page.htm` | **个人页 → 人** |
    | `<xxx>/list.htm` · `/list2.htm` · 以 `/` 结尾 | 栏目页 → 导航 |

    实测浙大航空航天学院师资页：人名样式的链接 191 个，其中 **127 个指向个人页、
    37 个指向 list.htm 栏目页**。**按 href 分就够了，不用猜文字。**
    """
    NAV = re.compile(r"/list\d*\.htm/?$|/\d+/$|/$")
    PERSON = re.compile(r"person\.|/info/|/page\.htm|/\d{4}/\d{2}\d{2}/c\d+a\d+")

    out, seen = [], set()
    for a in doc.xpath("//a[@href]"):
        t = " ".join(common.text(a).split()).strip()
        if not t or len(t) > 20 or t in NOT_A_NAME:
            continue
        if not NAME_FLEX_RE.match(t):
            continue
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if NAV.search(href) or not PERSON.search(href):
            continue
        url = common.norm_url(page_url, href)
        if not url or url in seen:
            continue
        seen.add(url)
        # 分组名：往上找最近的一个标题元素（「应用力学研究所」这种）
        group = None
        for anc in a.iterancestors():
            if not isinstance(anc.tag, str):
                continue
            hs = anc.xpath("./h1|./h2|./h3|./h4|./h5|./strong|./b")
            if hs:
                cand = " ".join(common.text(hs[0]).split()).strip()
                if 2 <= len(cand) <= 24:
                    group = cand
                break
        out.append({"name": t, "title": None, "homepage": url, "group": group})
    return out


def parse_faculty_page(raw, page_url, school, school_slug, unit, group=None):
    """返回导师记录列表。找不到结构就返回空列表（不要抛异常打断整批）。

    **三条路径按"保基线"的顺序试**，谁先出结果就用谁：
      A. `li > h3` 卡片 —— 浙大模板，唯一跑通的基线，先试它；
      B. **带表头的表格** —— 复旦物理那种，`<li>` 数量是 0；
      C. **姓名+职称粘连**（`朱笑东高级实验师`）—— 浙大材料那种，没有任何分隔符；
      D. **内容区里的人名链接**（`<a href="person…">张三</a>`）—— 浙大航空航天那种，
         判据是 href 指向个人页而不是 list.htm 栏目页。
    前面的有结果就不走后面的，这样加新路径不会动到已跑通的基线。
    """
    out = _li_card_people(raw, page_url, school, school_slug, unit, group)
    if out:
        return out

    doc = common.parse(raw)
    if doc is None:
        return []
    if group is None:
        t = doc.xpath("//title")
        group = common.text(t[0]) if t else None

    for v in _table_people(doc):
        name = v.get("name")
        title_text = v.get("title")
        fields = {k: v.get(k) for k in ("email", "phone", "research", "homepage")}
        out.append(_make_rec(name, title_text, fields,
                             fields.get("homepage") or v.get("homepage"),
                             group, page_url, school, school_slug, unit))
    if out:
        return out

    for v in _concat_people(doc, page_url):
        out.append(_make_rec(v["name"], v["title"], {"homepage": v.get("homepage")},
                             v.get("homepage"), group, page_url, school, school_slug, unit))
    if out:
        return out

    for v in _name_link_people(doc, page_url):
        out.append(_make_rec(v["name"], v["title"], {"homepage": v.get("homepage")},
                             v.get("homepage"), v.get("group") or group, page_url,
                             school, school_slug, unit))
    return out


def person_card_count(doc):
    """页面上像「人员」的条目数 —— 与 CMS 无关的判据。

    坑（已踩）：第一版只认 `h3/h4` 里的姓名，结果北大物理学院从"找到 2 个入口页"
    变成"什么都没找到"。原因很简单：**很多学校的人员列表是 `<a>张三</a>`，
    根本不用标题标签**。判据太窄，就把"能看到的人"判成了"没有人"。

    现在认两种信号，取并集：
      · 链接文字本身就是一个姓名（`<a>张三</a>`）—— 最通用
      · 块里有 h3/h4 且文字是姓名
    姓名判据复用 `NAME_RE` + `NOT_A_NAME`，和解析器同一套标准，
    所以"页面里有人、解析器却抽不到"这件事能被真实反映出来。
    """
    names = set()
    for a in doc.xpath("//a"):
        t = common.text(a)
        if NAME_RE.match(t) and t not in NOT_A_NAME:
            names.add(t)
    for el in doc.xpath("//li | //div | //tr"):
        heads = el.xpath("./h3 | ./h4 | ./*/h3 | ./*/h4")
        if not heads:
            continue
        t = common.text(heads[0])
        if NAME_RE.match(t) and t not in NOT_A_NAME:
            names.add(t)
    return len(names)


def discover_faculty_pages(homepage, max_pages=30, verify=True, min_people=5):
    """从院系首页找出真正的师资列表页。

    实测浙大物理学院的师资是分研究所挂在同一个下拉菜单里的
    （凝聚态物理研究所、浙江近代物理中心、聚变理论与模拟中心…），
    所以不能只找一个"师资队伍"链接，要把这一类全部收进来。

    verify=True 时会**逐个打开确认页面上真的有人**（person_card_count >= min_people）。
    多花几次请求，但换来的是"不再把目录页当师资页"——
    实测不验的话 6 个院系里 4 个找不到真页面。
    """
    doc = common.parse(common.fetch(homepage)[0])
    if doc is None:
        return []
    kw = re.compile(r"师资|教师|导师|队伍|人才|教授|研究人员|研究组|课题组|"
                    r"研究所|中心|实验室|系$|faculty|people")
    found = {}
    for a in doc.xpath("//a"):
        href = (a.get("href") or "").strip()
        label = common.text(a)
        if not href or not label or len(label) > 24:
            continue
        url = common.norm_url(homepage, href)
        if not url or not url.split("?")[0].endswith((".htm", ".html", ".jsp", ".psp")):
            continue
        if kw.search(label):
            found[url] = label

    out = []
    for url, label in list(found.items())[:max_pages]:
        if not verify:
            out.append({"url": url, "label": label})
            continue
        raw, _ = common.fetch(url)
        d = common.parse(raw)
        if d is None:
            continue
        n = person_card_count(d)
        if n >= min_people:
            out.append({"url": url, "label": label, "people": n})
    return out

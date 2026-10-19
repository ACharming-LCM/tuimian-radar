# -*- coding: utf-8 -*-
"""栏目发现：给一个院系首页，自动找出"招生信息 / 通知公告"这类列表页。

这一步是这个项目能不能规模化的关键：全国高校有几十个院系站点，
如果每个都要人工写选择器，30 所学校就要写 30 遍。
实测国内高校大量使用同一套建站 CMS（栏目 url 形如 /zsxx_69706/list.htm，
列表容器 id 形如 wp_news_w10），因此可以自动发现。

归属：A（数据管道）。
"""
import re

import common

# 栏目名关键词 -> 权重。权重高的栏目排在前面先抓。
BOARD_KW = {
    "推免": 10, "免试": 10, "研究生": 8, "招生": 8, "硕士": 6, "博士": 6,
    "夏令营": 7, "报考": 5, "培养": 3, "学位": 3,
    "通知": 4, "公告": 4, "公示": 4, "新闻": 1,
}

# ---------------------------------------------------------------------------
# 正/负向判定。依据：16 个站点 51 个栏目的人工核对结果（data/out/board-verdicts.md），
# 自动发现的精度只有约 1/3，错因高度集中在下面几类。
# 与其让 crawl 去抓一堆「学位申请系统」「导师评优」「在职攻读硕士学位」，
# 不如在发现阶段就丢掉。
# ---------------------------------------------------------------------------

# 强制否决：出现即丢弃，**压过任何正向词**。
# 必须能压过正向词，因为「国际招生」里有"招生"但它全是留学生。
# 导师/评优/津贴 也放在这里：真实案例「导师资格、招生、津贴拨付、评优相关」
# 里有"招生"，但那个招生是指**导师的招生资格**，不是考生的招生。弱否决压不过正向词，
# 所以这几类必须升级成强制否决。
BOARD_VETO = ("国际", "留学生", "港澳台", "在职", "同等学力", "名誉", "代培",
              "非全日制", "导师", "评优", "津贴")

# 弱否决：仅当栏目名里**没有**正向词时才丢弃。
# 刻意用组合词（"学位管理"而不是"学位"），否则会误杀"专业学位招生"这类正常栏目。
# 注意"培养""教学"用裸词是安全的 —— 因为只要栏目名里有"招生"这类正向词就不会被否决，
# 所以"招生培养"能活下来，而"研究生教学""人才培养"会被丢掉。
BOARD_NEG = ("学籍", "注册", "请假", "休学", "复学", "导师", "评优", "津贴",
             "学位管理", "学位申请", "学位授予", "学位工作", "答辩", "盲审",
             "培养", "教学", "课程", "选课", "开题", "中期",
             "竞赛", "大赛", "公派", "留学", "简介", "联系方式", "中心简介",
             "表格", "下载", "党建", "校友", "奖助", "助学", "贷款", "教材",
             # 党务/工会/群团/离退休类：整块栏目都是内部新闻，对保研生是纯噪音。
             # 坑（已踩）：原来只有「党建」，而清华物理系的栏目叫「**党团**学工」——
             # 差一个字没匹配上，于是那个栏目被抓进来 3 条
             # （「分工会举办AI赋能办公培训」「青年教职工政治引领座谈会」「党支部荣获…」），
             # **而且它那几页不带日期**，连带把 publish_date 弄成了 None、
             # 同时打红 schema 和排序两条门禁。
             # 教训：否决词要覆盖**同一个意思的常见写法**，不然就变成"差一个字就漏"。
             "党团", "团委", "工会", "统战", "纪检", "离退休", "妇委", "学生工作",
             # 科研动态类：整块栏目都是论文报道/学术活动，对保研生是纯噪音。
             # 实测来源：浙大物理的「研究进展 研究机构 学术活动 公共平台 通知」抓进 3 条
             # （Science / Nature Materials / PRL 的论文报道），
             # 而公告列表默认按日期排，**这三条科研新闻直接顶到了列表第一位** ——
             # 评委打开列表页看到的第一条是物理系的论文报道，不是招生信息。
             #
             # 注意这里**只加栏目名里能看出来的科研词**。`通知公告`这类泛栏目名
             # 里面混着"带值班表"，但栏目名本身看不出来，这一层管不了 ——
             # 那种情况的正确解法是记录级排序，不是这里猜。
             # 另外正向词优先于本表，所以「招生信息」类栏目里的科研条目不会被误伤。
             "研究进展", "学术活动", "学术报告", "学术讲座", "科研成果",
             "成果展示", "科研动态", "研究机构", "公共平台")

# 强正向词：出现即保留（除非命中 BOARD_VETO）。
BOARD_POS = ("招生", "推免", "免试", "夏令营", "预推免", "报考",
             "专业目录", "招生简章", "招生信息", "招生动态")


def classify_board(own_label, full_label=None):
    """返回 (是否保留, 原因)。

    **为什么要分开传两个标签**（踩过的坑）：
    这些高校导航菜单的层级是 `<li><a>硕士研究生</a><ul>入学、注册、请假、休（复）…</ul></li>`，
    真正说明用途的文字在**父级菜单**里，链接自身文字只有光秃秃的「硕士研究生」。
    但强制否决词又不能用父级文字 —— 父级往往把**兄弟条目**的文字也包含进来，
    拿它匹配「国际」「港澳台」会误杀掉正常栏目。

    所以：
      · 强制否决（国际/港澳台/在职/导师…）只看**链接自身文字** —— 高置信、不误杀；
      · 弱否决和正向词看**完整菜单文字** —— 证据越多越好。
    """
    full = full_label or own_label
    for w in BOARD_VETO:
        if w in own_label:
            return False, f"否决词「{w}」"
    if any(w in full for w in BOARD_POS):
        return True, "命中正向词"
    for w in BOARD_NEG:
        if w in full:
            return False, f"弱否决词「{w}」"
    return True, "无正无负，保守保留"

LIST_HINT = re.compile(
    # ① 维普 / 南湖之舟系（浙大物理、复旦、南大…）：/zsxx_69706/list.htm
    r"list\.(htm|html|psp|jsp)(\?|$)"
    # ② 西湖 / 正方系（西安交大…）：xynr.jsp?urltype=tree.TreeTempUrl&wbtreeid=1027
    r"|xynr\.jsp"
    # ③ 栏目目录形态：/zsxx/index.htm、/yjs/list.jsp
    r"|/[\w-]{2,24}/(index|list|news|tzgg|zsxx|zsjy|yjs)\.(htm|html|jsp|psp)$"
    # ④ **路径式栏目（现代站点，没有扩展名）**：/zsjz/bsszs、/column/69
    #
    # 坑（已踩，而且瞒了很久）：前三条是**维普/正方 CMS 的 URL 约定**。
    # 上交（`yzb.sjtu.edu.cn/zsjz/bsszs`）、清华、川大这些站的栏目是路径式的，
    # **一条都不匹配 → 候选在算分之前就被全筛掉**。
    # 后果很隐蔽：`with_rejected=True` 里拒绝数是 **0**，报告看起来像"这些站点没有栏目"，
    # 而实际上首页上招生类链接有 149 个（川大一家 27 个）。
    # **一个在"生成候选"阶段就失败的过滤器，不会在"拒绝原因"里留下任何痕迹。**
    r"|/[\w-]{2,24}/[\w-]{2,32}$"
    r"|/column/\d+"
    # ⑤ **同站两段路径 + 词形文件名**：`/zsgz/sszs/zsjz.htm`（武大）、`/yjszs.htm`
    #
    # 坑（已踩）：规则 ③ 只认 `index|list|news|tzgg|zsxx|zsjy|yjs` **这几个固定词**，
    # 于是 `zsjz.htm`（招生简章）、`a2027n.htm` 这类**真实栏目页**全被漏掉。
    # 实测武汉大学研究生院首页 8 个招生类链接、形状合格的 **0 个** ——
    # 报告上写着"首页找不到候选栏目"，看着像那个站没栏目。
    # 判据放宽成"词形文件名"而不是"白名单里的词"，
    # 纯数字文件名（多为文章 id）交给下面的 DETAIL_LIKE 兜。
    r"|/[\w-]{2,24}/[\w-]{2,32}\.(htm|html|jsp|psp)$"
    # ⑥ **根目录下的词形文件**：`/ss_news.html`（重庆大学）、`/bs_news.html`
    #
    # 坑（已踩，2026-10-17 逐站排查时抓到的）：规则 ⑤ 要求**两段**路径，
    # 而重庆大学研招网的栏目就叫 `https://yz.cqu.edu.cn/ss_news.html` ——
    # 只有一段。整站 8 个栏目**一个都进不了候选**，产出 0 条，
    # 而在 `freshness.json` 里它显示成 `boards=8 empty=8 failed=[]`，
    # 看起来像"栏目抓到了但没内容"。**又一个"生成候选阶段就失败、拒绝原因里没痕迹"。**
    #
    # ⚠️ 这里第一版我又踩了一次：写成 `^/[\w-]{2,32}\.html$`。
    # 但 `LIST_HINT` 是拿**完整 URL** 去 search 的（`https://yz.cqu.edu.cn/ss_news.html`），
    # `^/` 永远匹配不到字符串开头 —— 于是这条规则看起来写好了、实际一条都没生效。
    # **锚点必须对"喂进来的东西"成立**，不是对"我以为喂进来的东西"。
    r"|/[\w-]{2,32}\.(htm|html|shtml|jsp|psp|php)$"
    # ⑦ **尾斜杠路径（无扩展名）**：`http://ga.sjtu.edu.cn/zsgl/xlygl/`（上交夏令营）
    #
    # 规则 ④ 认 `/seg/seg` 但不认结尾带 `/` 的。现代站点（尤其自建报名系统）常用这种。
    r"|/[\w-]{2,24}/[\w-]{2,32}/$"
    r"|/[\w-]{2,24}/[\w-]{2,32}/[\w-]{2,32}/?$",
    re.I,
)

# 文章详情页**不是栏目**。放宽 URL 形状之后必须配一道反向闸，
# 否则「2027年硕士研究生招生简章」这种**文章**会因为标题命中招生词而被当成栏目，
# 每个"栏目"只产出 1 条，列表页就被文章淹了。
#
# 坑（已踩，而且差点悄悄毁掉整个维普系）：
# 我第一版写了 `/\\d{5,}/`（"长数字就是文章 id"）——
# **可维普 CMS 的栏目 URL 就是 5 位栏目号**：`/50916/list.htm`、`/51226/list.htm`。
# 于是在浙大材料那一页上，136 个栏目**全被这道闸杀掉**，该站记录从 6 条掉到 0 条。
# 别的学校没炸只是因为它们的栏目名是字母（`/ysjs/list.htm`、`/qstp_73988/list.htm`）——
# **一个判据只在部分站点上出错时，最容易瞒过去。**
# 现在的判据改成认**文章 URL 的真实形状**，不认"数字有多长"：
#   · 维普文章：`/2023/0414/c51226a2742945/page.htm`（`/YYYY/MMDD/` + `c…a…`）
#   · 通用：`/info/` `/article/` `/content/` `/detail`
DETAIL_LIKE = re.compile(
    r"/info/|/article/|/content/|/detail|/\d{4}/\d{2}\d{2}/|c\d{5,}a\d{4,}|"
    r"/t20\d{6}_|\.(pdf|docx?|xlsx?|zip|rar)$|"
    # 2026-10-17 补两种实测的文章形状。**必须和上面放宽 LIST_HINT 同时做** ——
    # 放宽了栏目形状却不补文章形状，首页新闻标题会全被当成栏目候选，
    # 每个"栏目"只出 1 条，列表反而被文章淹掉（这是这块注释开头警告的事）。
    r"/post/\d+|/news/\d{4}-\d{2}/\d+|/archives/\d+",
    re.I,
)

# 登录页 / 版权行 / 办事系统 —— 判成栏目会白占额度（见 discover_boards 里的注释）
BOARD_JUNK = re.compile(r"登录|登陆|版权所有|Copyright|忘记密码|©|qblogin|/Login", re.I)


def score_board(label):
    return sum(w for k, w in BOARD_KW.items() if k in label)


def discover_boards(homepage, max_boards=6, with_rejected=False, apply_filter=True):
    """返回 [{label, url, score}]，按分数降序。只认形如 .../list.htm 的栏目页。

    with_rejected=True 时返回 (kept, rejected)，rejected 里每条带 `reason`，
    用于评估工具算精度、以及在报告里解释"为什么丢了这个栏目"。
    apply_filter=False 用于对照实验，看过滤器到底挣了多少。
    """
    doc = common.parse(common.fetch(homepage)[0])
    if doc is None:
        return ([], []) if with_rejected else []

    found = {}
    for a in doc.xpath("//a"):
        href = (a.get("href") or "").strip()
        # 私用区图标（图标字体的码位）必须在这里就删掉：北大研招网的菜单文字是
        # `\ue603 硕士招生 \ue61e 博士招生 …`，不清理的话它会被当成栏目名落盘，
        # 还会在 GBK 环境下把整轮 `print()` 打崩。
        own = common.clean_text(common.text(a))
        if not href or not own or len(own) > 24:
            continue
        url = common.norm_url(homepage, href)
        if not url or not LIST_HINT.search(url.split("?")[0]):
            continue
        if DETAIL_LIKE.search(url.split("?")[0]):
            continue

        # 父级菜单文字通常更完整（用途说明在父级，链接自身只有"硕士研究生"这种光秃秃的名字）。
        # 取更长的那个当展示标签 —— **展示用长标签，判定时两个都传**（见 classify_board 注释）。
        parent = common.clean_text(common.text(a.getparent()))
        full = parent if (parent and len(parent) <= 60 and len(parent) > len(own)) else own

        # 坑（已踩，被 eval_discovery.py 抓出来的回归）：算分要用**拼接后的文字**，
        # 不能只取其中一个。`score_board` 是**累加**关键词权重的，
        # "父级 自身" 拼起来可能刚好过 3 分门槛，而单独任一个都过不了 ——
        # 我一度改成 max(各自得分)，召回就从 100% 悄悄掉到 81%，
        # 而过滤器本身并没有误杀任何正确栏目。这种损失没有基准评估根本看不见。
        s = score_board(f"{full} {own}")
        if s < 3:
            continue
        prev = found.get(url)
        if prev is None or s > prev["score"]:
            found[url] = {"label": full, "own": own, "url": url, "score": s}

    kept, rejected = [], []
    for b in sorted(found.values(), key=lambda x: -x["score"]):
        # 登录页 / 版权行 / 办事系统页面**不是栏目** —— 它们出 0 条，却会白占栏目额度。
        # 放宽 URL 形状之后这批会涌进来：实测上海交大 6 个槽位**全是这种**
        # （`grd.ccnu.edu.cn/yjsxt/qblogin`、`yz.scu.edu.cn/Account/Login`、
        #   `版权所有：中山大学研究生院 © 2010-2026`），结果真正的内容页反而被挤掉。
        if BOARD_JUNK.search(b["label"]) or BOARD_JUNK.search(b["url"]):
            rejected.append({**b, "reason": "登录页/版权行/办事系统，不是栏目"})
            continue
        ok, reason = classify_board(b["own"], b["label"])
        b["reason"] = reason
        if ok or not apply_filter:
            kept.append(b)
        else:
            rejected.append(b)
    # 注意 rejected 要按分数排序后再截断，否则报告里看不到最该丢的那几个
    if with_rejected:
        return kept[:max_boards], rejected[:max_boards * 2]
    return kept[:max_boards]

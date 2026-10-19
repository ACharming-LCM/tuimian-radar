# -*- coding: utf-8 -*-
"""记录质量分级：`junk` / `thin` / `ok` —— **只标记，不删除**。

## 为什么是"标记"而不是"删掉"

1119 条里有 115 条是脏的：44 条标题就是站点名（「天津大学研究生招生网」「北京大学研究生招生网」），
71 条正文不到 100 字。直接删有两个问题：

1. **删了就没法复查。** 用户报"你们少了一条"时，我们连"那条当时长什么样"都拿不出来。
2. **判据会错。** 今天判成 junk 的，明天可能因为改了选择器变成好记录；反过来也一样。
   物理删除等于把一个**会变的判断**固化成不可逆的事实。

所以只在落盘前打一个 `record_quality` 字段，让下游自己决定收不收：
首屏/推送只取 `ok`，检索页可以把 `thin` 也放出来（并标注"正文较短"），
`junk` 留档可查。**这与 `field_state` 的三态是同一个思路：把"我们不确定"表达出来，
而不是用一个 null 假装确定。**

## 三档口径

- `junk`：标题是站点名/栏目名；或正文 < 100 字；或正文只有"发布时间/点击数/来源"这类元信息
- `thin`：100 ~ 300 字 —— 通常是"见附件"型公告，正文短但未必没用
- `ok`：≥ 300 字

阈值 100 / 300 的来源：实测 selector 命中的正文长度分位数是
p5=17 / p10=193 / p25=359 / p50=928。"17 字那批"就是元信息框（见 fields.ABSENT_MIN_BODY_CHARS
的注释），所以 100 卡在它和正常短正文之间；300 是"一段完整通知"的常见下限。

归属：A（数据管道）。下游（B/C）**只读这个字段，不要自己再判一遍**，
否则两套判据迟早会打架 —— 这个项目已经栽过 4 次同样的跟头。
"""
import os
import re

import common

# 站点名 / 栏目名：整条标题就是一个网站名。
# 真实样本：「天津大学研究生招生网」「北京大学研究生招生网」「上海大学研究生院」
SITE_NAME_RE = re.compile(
    r"^[\u4e00-\u9fff（）()·\s]{2,24}"
    r"(?:研究生招生网|研究生招生信息网|研招网|研究生院|招生信息网|招生网)$")

# 只讲"这条记录怎么来的"的元信息，没有任何公告内容。
META_ONLY_RE = re.compile(
    r"发布(?:时间|日期|者)|更新(?:时间|日期)|发表(?:时间|日期)|刊登(?:时间|日期)|"
    r"浏览(?:次数|量)|点击(?:次数|量)|阅读(?:次数|量)|来源|审核|编辑|责编|终审|"
    r"作者|访问量|字体|打印|关闭|分享|上一篇|下一篇|返回列表|"
    r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}|[：:\s]")

JUNK_MIN_CHARS = 100      # 低于它：几乎一定是元信息框或空壳
OK_MIN_CHARS = 300        # 达到它：一段完整的通知
META_RESIDUE_MAX = 50     # 剥掉元信息后剩下的汉字少于这个数 → 这一页只有元信息


def _is_site_name(title):
    t = (title or "").strip()
    return bool(t) and bool(SITE_NAME_RE.match(t))


def _is_prose(body):
    """正文像人写的：有句号，且不是三五行。"""
    b = (body or "").strip()
    return len(b) >= 100 and "。" in b


# ---------------------------------------------------------------------------
# 表格式目录页（A21）：标题是站点名、正文是一张「序号/学院/专业/学制/计划数」的表。
#
# 实测（上海大学 gmis.shu.edu.cn 8 条 + 北大招生目录 4 条）：这类页面正文 300~27000 字，
# 于是被门禁算成"正文完好却被判 junk"，看起来像标题抽取坏了。其实**根本不是公告** ——
# 整页没有一个句号，前几行就是表头。
#
# 判据三条同时成立（缺一条就会被真公告冤枉）：
#   1. 标题是站点名/学校名/单位名；
#   2. 正文前 500 字里**没有任何句末标点**（表格不是句子）；
#   3. 正文里 ≥8 个「…学院/研究院/系/中心」或 ≥5 个 6 位专业代码，且出现表头词。
# ---------------------------------------------------------------------------
TABLE_HEAD_RE = re.compile(r"(招生目录|专业目录|招生计划|分数线|专业名称|学院名称|序号)")
CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")


def is_table_directory(rec):
    """这条是不是"表格式目录页"（专业目录 / 招生计划 / 分数线表）。返回 `(bool, 命中数)`。

    只看**前 200 字**：表格页的结构特征全在页首（院系名或专业代码密集、没有一个句号）；
    也正因为只看页首，北大那两页正文 179/116 个句号（页尾附了招生说明）不会让判据失效。
    实测分布（A21，16 条 junk 长正文）是两极：
        北大 4 条目录：names200 = 24~25，句号 0
        上大 8 条表格：codes200 = 5~6，句号 0
        天津真公告：names200 = 0，句号 6      ← 必须救回，不能被这条判进去
        北大数学学院真公告：names200 = 2，句号 4
    """
    title = (rec.get("title") or "").strip()
    if not (_is_site_name(title) or title in ((rec.get("school") or ""),
                                              (rec.get("unit") or ""))):
        return False, 0
    head = (rec.get("body_text") or "")[:200]
    if not head or "。" in head:
        return False, 0
    names = len(INDEX_NAME_RE.findall(head))
    codes = len(CODE_RE.findall(head))
    if names >= 8 or codes >= 5:
        return True, max(names, codes)
    return False, max(names, codes)


def _is_meta_only(body):
    """短正文里除了元信息字段什么都没有。

    判据用的是**残留字数**：把元信息标签、日期、数字、标点全剥掉，剩下的汉字少于 50 个，
    就认定这一页只有"发布时间/浏览次数/来源"。实测分布是**两极**，中间几乎是空的：

        residue = 0    49 条（「发布时间：2026-09-29浏览次数：13」）
        20~80          14 条（「…分享至：」这类尾巴）
        ≥ 100         143 条（真正的正文）

    所以 50 这个门槛不是拍的，是那片空档。只在 n < 400 时启用 ——
    长正文里出现「来源：」是正常的（页脚），不能因此判 junk。
    """
    b = (body or "").strip()
    if not b:
        return True
    if len(b) >= 400:
        return False
    if not META_ONLY_RE.search(b):
        return False
    rest = META_ONLY_RE.sub("", b)
    rest = re.sub(r"[\s\d\W_]+", "", rest)
    return len(rest) < META_RESIDUE_MAX


def classify(rec):
    """返回 `(quality, hits)`。`hits` 是命中的判据名，用来解释"为什么判成这个档"。"""
    title = rec.get("title") or ""
    body = rec.get("body_text") or ""
    n = len(body.strip())
    hits = []

    if _is_site_name(title) or title.strip() in ((rec.get("school") or ""),
                                                 (rec.get("unit") or "")):
        # 先排除"表格式目录页"（A21）：它不是公告，标题是站点名只是第二症状。
        # 判据见 is_table_directory()。这一类必须打成 body_is_index 前缀 ——
        # 门禁的"junk 里正文完好"靠这个前缀把索引页排除掉。
        table, n_names = is_table_directory(rec)
        if table:
            hits.append(f"body_is_index(table:{n_names})")
        else:
            hits.append("title_is_site_name")
    if n and n < JUNK_MIN_CHARS:
        hits.append("body_lt_100")
    if _is_meta_only(body):
        hits.append("body_meta_only")
    is_idx, n_names = is_index_page(rec)
    if is_idx:
        hits.append(f"body_is_index({n_names})")
    elif ORG_ONLY_RE.match(title.strip()):
        # 标题只有机构名。**不能一刀切成 junk**（B 的 issues/011：第一版这样干，
        # 把 7 条真公告也埋了）。先看正文能不能补出标题：
        #   · 能补 → 不判 junk，交给 retitle 步骤换上（标题比机构名有用得多）
        #   · 是导航列表/编号列表 → junk（这类页面本来就不是公告）
        #   · 补不出 → junk，但原因写清是 title_is_org_only
        if _is_nav_list(body):
            hits.append("body_nav_list")
        elif not title_from_body(rec):
            hits.append("title_is_org_only")
    # 编号导航页：标题是「001信息与通信工程学院」，正文是 001/002/003… 的院系列表。
    # 和"招生目录索引页"同形状，只是正文里没有"招生目录"四个字（B 的 issues/011 附带）。
    if _is_numbered_nav(rec):
        hits.append("body_numbered_nav")

    if hits:
        return "junk", hits
    if n < OK_MIN_CHARS:
        return "thin", ["body_100_to_300"]
    return "ok", []


# ---------------------------------------------------------------------------
# 标题回退（A6）：标题被抽成站点名，但正文其实完好
#
# 实测天津大学 39 条：标题是「天津大学研究生招生网」，正文 7650 字、内容完整，
# 真正的标题就写在正文第 2 行（第 1 行是「字体大小：」，第 3 行是「来源： 发布时间：…」）。
# 这类记录被判 junk 是按口径（标题画不出卡片），但**藏着真公告**，对首屏是纯损失。
#
# 判据收紧在"正文 ≥300 字"：正文短的那些，标题错了就真的没法用了。
# ---------------------------------------------------------------------------

# 页面装饰行：不是标题
TITLE_NOISE_RE = re.compile(
    r"^(?:字体大小|字号|打印|关闭|分享|扫一扫|返回|上一篇|下一篇|当前位置|首页|"
    r"来源|发布(?:时间|日期)|更新(?:时间|日期)|浏览(?:次数|量)|点击(?:次数|量)|"
    r"阅读(?:次数|量)|审核|编辑|责编|作者|访问量)\s*[：:]?")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
FALLBACK_MIN_CHARS = 300
TITLE_MIN, TITLE_MAX = 8, 80


def _headline_from_prose(rec):
    """标题是站点名、正文是正常文章时，从正文前几行找那条"像标题"的行。

    和上面那个严格版分开，是因为**标题里可以有逗号**：天津大学 2026-06-16 那条的真标题是
    「天津大学研究生录取通知书电子版开放下载啦，小伙伴们快去领取！」—— 严格版见到「，」
    就否掉，于是这条 1511 字的真公告永远救不回来（A21 实测）。

    放宽的同时补了三条约束，防止把正文第一句当标题：
      · 必须以 ！/？ 结尾（标题语气），或公告词落在最后 15 个字里；
      · 不含表格分隔（≥2 个 `/`、`|`）—— 那是「专业名称/代码」行；
      · 10~60 字，且不是机构名。
    """
    body = rec.get("body_text") or ""
    if not _is_prose(body):
        return None, "none"
    for raw in body.splitlines()[:15]:
        line = raw.strip()
        if not (10 <= len(line) <= 60):
            continue
        if TITLE_NOISE_RE.match(line) or _is_site_name(line):
            continue
        if ORG_ONLY_RE.match(line):
            continue
        if re.match(r"^[一二三四五六七八九十0-9]+[、.．]", line):
            continue
        if re.match(r"^[（(0-9\-—•·]", line):
            continue
        if line.count("/") >= 2 or "|" in line:
            continue
        if not (line.endswith(("！", "？")) or NOTICE_WORD_RE.search(line[-15:])):
            continue
        return line.rstrip("！？"), "fallback"
    return None, "none"


def title_fallback(rec):
    """给"标题是站点名但正文完好"的记录找回标题。返回 `(title|None, source)`。"""
    body = rec.get("body_text") or ""
    if len(body.strip()) < FALLBACK_MIN_CHARS:
        return None, "none"
    if _is_nav_list(body):
        return None, "none"
    title = (rec.get("title") or "").strip()
    if _is_site_name(title) or title in ((rec.get("school") or ""),
                                         (rec.get("unit") or "")):
        return _headline_from_prose(rec)
    for raw in body.splitlines():
        line = raw.strip()
        if not line or len(line) < TITLE_MIN or len(line) > TITLE_MAX:
            continue
        if TITLE_NOISE_RE.match(line):
            continue
        if _is_site_name(line):
            continue
        # 至少 4 个汉字，且不是"一、报考条件"这种小节标题
        if len(CJK_RE.findall(line)) < 4:
            continue
        if re.match(r"^[一二三四五六七八九十0-9]+[、.．]", line):
            continue
        if line.endswith(("：", ":")):
            continue
        # 纯机构名（「地球与空间科学学院」「北京大学数学学院」）不是公告标题 ——
        # 继续往下找。A6 第一版就是在这里把院系名当成了标题，结果 30 条记录的标题
        # 变成「地球与空间科学学院」，用户看到一条以学院名命名的"公告"（B 的 A14）。
        if ORG_ONLY_RE.match(line):
            continue
        # **必须像一条公告标题**：公告词出现在**结尾那一段**，且整行不是一句话。
        # 第一版只要求"含公告词"，于是在北大院系页上捞出一堆垃圾：
        # 「(1) …申请表1份…」「2026年7月6日－8日」「在自由讨论环节，双方围绕…」
        # 「参营资格审核工作由数学学院组织专门委员会负责。」—— 比原来的机构名更糟。
        # 收紧成三条：① 不含句读（。，；|）；② 公告词落在最后 15 个字里；③ 不以括号/数字开头。
        if re.search(r"[。，；|]", line):
            continue
        if re.match(r"^[（(0-9\-—•·]", line):
            continue
        if not NOTICE_WORD_RE.search(line):
            continue
        return line, "fallback"
    return None, "none"


# 整个字符串就是一个机构名（可能带"北京大学"这类前缀），没有任何"通知/公告/简章"之类的词。
ORG_ONLY_RE = re.compile(
    r"^[\u4e00-\u9fff（）()·]{2,24}"
    r"(?:学院|研究院|研究所|研究中心|学部|学系|系|中心|实验室)$")


# ---------------------------------------------------------------------------
# 索引页（A14）：标题是机构名、正文是一堆机构名 —— 这是"招生目录的院系索引页"，
# 不是公告。真实样本（北京大学 3 条）：
#
#   标题：地球与空间科学学院
#   正文（27829 字）：招生目录 硕士 理学部 数学科学学院 物理学院 城市与环境学院 …
#   正文前 200 字里有 25 个机构名、**一个句号都没有**
#
# 判据两条同时成立（缺一条就会误伤）：
#   1. 正文前 200 字里 ≥8 个以 学院/研究院/中心/系/学部 结尾的机构名；
#   2. 前 200 字里**没有任何句末标点**（索引页是名词罗列，真公告的第一句一定有标点）。
#   ＋ 标题本身是机构名（这保证了不会把"招生专业目录"这种真文档判进去）。
#
# 实测：全库只有 3 条命中（都是北大的招生目录索引页），27 条"标题是机构名但正文是公告"
# 的一个都没误伤 —— 它们的正文里有句子、机构名也没那么多。
# ---------------------------------------------------------------------------
INDEX_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,12}(?:学院|研究院|研究中心|学部|学系|系|中心|实验室)")
INDEX_MIN_NAMES = 8
SENT_PUNCT_RE = re.compile(r"[。！？；!?;]")


def is_index_page(rec):
    """这条记录是不是"栏目/院系索引页"（不是公告）。返回 `(bool, 命中数)`。"""
    title = (rec.get("title") or "").strip()
    head = (rec.get("body_text") or "")[:200]
    if not head or not ORG_ONLY_RE.match(title):
        return False, 0
    names = len(INDEX_NAME_RE.findall(head))
    if names < INDEX_MIN_NAMES:
        return False, names
    if SENT_PUNCT_RE.search(head):
        return False, names
    return True, names


# ---------------------------------------------------------------------------
# 机构名标题 → 从正文找真标题（A14 的第二半）
#
# 31 条记录的标题就是院系名（「北京大学城市与环境学院」）。其中 3 条是招生目录索引页
# （判 index），另外 28 条**是真公告**，只是标题被抽成了院系名。
# 真标题就在正文里：
#     2026年北京大学城市与环境学院“全国优秀大学生暑期夏令营”报名通知
#     北京大学信息管理系关于举办“2026年全国优秀大学生夏令营”活动的通知
# 判据：正文前若干行里，取第一条**带公告词**（通知/公告/简章/章程/办法/细则/说明/
# 名单/公示/目录/指南/须知/方案）且长度 8~80 的行；拒绝"请…""各位…""为…"这种句子开头。
# ---------------------------------------------------------------------------
NOTICE_WORD_RE = re.compile(
    r"(通知|公告|简章|章程|办法|细则|说明|名单|公示|目录|指南|须知|方案|"
    r"注意事项|安排|考核|开通|更新|公布|发布|领取|结果|条件|要求|"
    r"夏令营|招生|报名|推免|复试|调剂)")
# 只收"一定是标题"的词（不含 考核/条件/安排 这类小节标题里也有的词）——
# 页面的 <h1>/<h2> 混着小节标题，用宽词表会把「四、思想政治素质和品德考核」当标题。
STRONG_TITLE_WORD_RE = re.compile(
    r"(招生|简章|章程|通知|公告|说明|办法|名单|公示|细则|指南)")
SENTENCE_HEAD_RE = re.compile(
    r"^(请|各位|为|根据|按照|现将|现就|经|依据|申请人|其中|该|本|其|另|如|若|凡|所有|每|"
    r"拥护|身体健康|具备|符合)")


def notice_title_from_body(rec, limit_lines=20):
    """标题只是机构名时，从正文里找一条像公告标题的行。返回 `(title|None, source)`。"""
    title = (rec.get("title") or "").strip()
    if not ORG_ONLY_RE.match(title):
        return None, "detail"
    body = rec.get("body_text") or ""
    for line in body.splitlines()[:limit_lines]:
        line = line.strip()
        if not (TITLE_MIN <= len(line) <= TITLE_MAX):
            continue
        if TITLE_NOISE_RE.match(line) or SENTENCE_HEAD_RE.match(line):
            continue
        if line.endswith(("。", "！", "？")):
            continue
        if ORG_ONLY_RE.match(line):
            continue
        if NOTICE_WORD_RE.search(line):
            return line, "fallback_body"
    return None, "detail"


# ---------------------------------------------------------------------------
# B 的 issues/011：`title_is_org_only` 太宽 —— 正文完好的记录被一刀切成 junk
#
# 9 条里 7 条是真公告（夏令营、博士招生、思政骨干、拟录取公示、新闻），
# 只有 2 条「首页/系史/历史阶段…」的导航列表该 junk。
# 修法：先**从正文首句回捞标题**，捞不到才判 junk，并且给出具体原因。
# ---------------------------------------------------------------------------
CSS_LINE_RE = re.compile(r"\.gpCms|\{|\}|font-|margin|padding|border|px\b|^\s*[.#][\w-]+")
TITLE_HINT_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9“”\"'（）()、·\-—…：:]{6,70}?"
    r"(?:通知|公告|简章|章程|说明|办法|细则|名单|公示|指南|须知|安排|方案)")


def _clean_body_lines(body, limit=12):
    """去掉 CSS/JS 行，返回前若干条有内容的行。"""
    out = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or CSS_LINE_RE.search(line):
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def _is_nav_list(body):
    """导航列表：一堆 2~8 字的短词（首页/系史/历史阶段/招生动态…），没有句子。"""
    lines = [l for l in (body or "").splitlines() if l.strip()]
    if len(lines) < 6:
        return False
    short = sum(1 for l in lines[:20] if len(l.strip()) <= 8)
    head = (body or "")[:300]
    return short >= 6 and not SENT_PUNCT_RE.search(head)


NUMBERED_NAV_RE = re.compile(r"^\s*\d{3}\s*[\u4e00-\u9fff]{2,12}(?:学院|学部|系|中心|研究院)")


def _is_numbered_nav(rec):
    """标题是「001信息与通信工程学院」、正文里 ≥5 个「编号+院系名」→ 院系编号导航页。"""
    if not NUMBERED_NAV_RE.match((rec.get("title") or "").strip()):
        return False
    body = rec.get("body_text") or ""
    return len(re.findall(r"\b\d{3}\s*[\u4e00-\u9fff]{2,12}(?:学院|学部|系|中心|研究院)", body)) >= 5


def _acceptable_title(t):
    """标题候选的统一闸门。三条都要过，否则宁可返回 None（记录保持 junk）。"""
    t = (t or "").strip()
    if not (6 <= len(t) <= 70):
        return None
    if _is_site_name(t) or SENTENCE_HEAD_RE.match(t):
        return None
    # 不以编号开头（"4．身体健康状况…"）、不像"字段名：值"（"报名时间：2025年…"）
    if re.match(r"^[0-9０-９]+[．.、)）]", t):
        return None
    if "：" in t[:12] or ":" in t[:12]:
        return None
    # 「（1）网上报名时间：…」这类是正文字段，不是标题
    if re.match(r"^[（(][0-9０-９]+[)）]", t):
        return None
    if re.search(r"^(网上)?(报名|申请|考试|提交|缴费)时间", t):
        return None
    if len(CJK_RE.findall(t)) < 6:
        return None
    return t


def title_from_body(rec):
    """从**缓存页面 + 正文**里捞一条能当标题的文字。返回 `title|None`。

    顺序（都对不上就返回 None，让它保持 junk —— 宁可不补，不能补错）：
      1. **缓存详情页的 `<title>`**：`莫斯科物理技术学院代表团访问北京大学物理学院-北京大学物理学院`
         这种，去掉 `-学院` 后缀就是好标题；
      2. 正文里**带公告词的短语**，前缀不能是"申请人须/请/为/经/其中/本/该"这类句子开头；
      3. **正文首句**（截到句号），同样要过前缀过滤，且必须含招生/通知类词。
    """
    title = (rec.get("title") or "").strip()
    if not ORG_ONLY_RE.match(title):
        return None
    # 导航列表页（A21）：正文只有「首页/系史/历史阶段…」这类菜单，页面 <title> 却可能是
    # 一篇文章的标题（缓存页里带着面包屑）。用那个标题去救，会把一张只有菜单的页面
    # 变成一条"看上去正常"的公告 —— 正文 307 字，用户点开只看到导航。
    if _is_nav_list(rec.get("body_text") or ""):
        return None

    cache = common.cache_path(rec.get("url") or "")
    if os.path.exists(cache):
        try:
            with open(cache, "rb") as f:
                html = common.decode(f.read())
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
            if m:
                t = common.clean_text(m.group(1)).strip()
                t = re.split(r"\s*[-_|—]\s*", t)[0].strip()
                # 站点名/机构名不算标题；但「…代表团访问北京大学物理学院」这种带动词的可以
                informative = (not ORG_ONLY_RE.match(t)) or bool(
                    re.search(r"访问|举办|召开|举行|宣讲|招募|通知|公示|名单|招聘", t))
                t2 = _acceptable_title(t) if informative else None
                if t2:
                    return t2
            # 缓存页的 `<h1>/<h2>` 常常就是真标题（A21 实测：北大的院系页
            # `<title>` 只有「北京大学数学学院」，但 `<h2>` 里是
            # 「…2026年“申请-考核制”博士研究生招生说明」）。
            # 页面上还有「一、申请条件」「四、思想政治素质和品德考核」这类**小节标题**，
            # 它们也含公告词（考核/办法/条件），必须按序号前缀排掉，否则标题会变成章节名。
            for hm in re.finditer(r"<h[12][^>]*>(.*?)</h[12]>", html, re.S | re.I):
                t = common.clean_text(re.sub(r"<[^>]+>", "", hm.group(1))).strip()
                # **不要按 `-` 切**：标题里本来就可能有「申请-考核制」这种连字符，
                # 切完会变成「…2026年“申请」（A21 实测，标题被截断且引号不闭合）。
                if len(t) < 10 or re.match(r"^[一二三四五六七八九十]+[、.．]", t):
                    continue
                if re.match(r"^[（(0-9]", t):
                    continue
                if not STRONG_TITLE_WORD_RE.search(t):
                    continue
                t2 = _acceptable_title(t)
                if t2:
                    return t2
        except OSError:
            pass

    for line in _clean_body_lines(rec.get("body_text")):
        m = TITLE_HINT_RE.search(line)
        if m:
            t = m.group(0).strip(" 　·-—…:：")
            t2 = _acceptable_title(t)
            if t2:
                return t2
        m = re.match(r"^(.{8,60}?)[。！？]", line)
        if m:
            cand = m.group(1).strip(" 　·-—…")
            t2 = _acceptable_title(cand)
            if t2 and NOTICE_WORD_RE.search(t2):
                return t2
    return None

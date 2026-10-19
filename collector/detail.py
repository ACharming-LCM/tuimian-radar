# -*- coding: utf-8 -*-
"""详情页解析：正文、发布时间、来源、附件。

归属：A（数据管道）。
"""
import re
import urllib.parse

from lxml import html as LH

import common

BODY_XP = (
    "//div[contains(@class,'wp_articlecontent')]"
    " | //div[contains(@class,'article-content')]"
    " | //div[contains(@class,'v_news_content')]"
    " | //div[@id='content']"
    # 以下几条是实测补上的（原先 16 个详情页解析失败，全是这几类没覆盖）：
    " | //div[contains(@class,'pageArticle')]"      # 北大化院
    " | //div[contains(@class,'page_article')]"     # 北大法学院
    " | //div[contains(@class,'item_content')]"     # 浙大物理实验中心（维普 CMS 的另一套栏目）
    " | //div[contains(@class,'sub_content')]"
    " | //div[contains(@class,'article')]"
)
TITLE_XP = (
    "//div[contains(@class,'article-title')]//h1"
    " | //div[contains(@class,'article-title')]//h2"
    " | //div[contains(@class,'article-title')]//h3"
    " | //h1[contains(@class,'arti_title')]"
    " | //h2[contains(@class,'arti_title')]"
    " | //h1[contains(@class,'title')]"
)
HEAD_XP = (
    "//div[contains(@class,'article-title')]"
    " | //*[contains(@class,'arti_update')]"
    " | //div[contains(@class,'infobox')]"
    " | //*[contains(@class,'arti_info')]"
)
FILE_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|txt)(\?|$)", re.I)

# 通用兜底用：明确是"版式噪声"的容器。清掉它们之后再找正文块。
#
# 坑（已踩）：**不要把 subRight / sub_right 放进来。** 北大化院的正文恰好就装在
# `div.subRight` 里，清掉它等于把正文删了。判断标准是"这个名字能不能装正文"，
# 不确定的一律不清。
NOISE_XP = (
    "//script | //style | //noscript"
    " | //div[contains(@class,'nav')] | //div[contains(@class,'menu')]"
    " | //div[contains(@class,'header')] | //div[contains(@class,'footer')]"
    " | //div[contains(@class,'breadcrumb')] | //div[contains(@class,'crumb')]"
    " | //div[contains(@class,'banner')] | //div[contains(@class,'copyright')]"
    " | //div[contains(@class,'share')] | //div[contains(@class,'search')]"
    " | //div[contains(@class,'mobile')] | //div[contains(@class,'sidebar')]"
    " | //ul[contains(@class,'menu')] | //nav"
)

# 坑（已踩）：国内 CMS 的"文件公示"经常把正文做成**内嵌 PDF 阅读器**，
# 形如 <div pdfsrc="/_upload/article/files/xx/yy/xxx.pdf" swsrc="...">。
# 它既不是 <a href> 也不是 <img>，只按链接后缀找附件的做法会整篇漏掉 ——
# 而漏掉的恰好是"推免工作细则"这种最关键的公示文件。
PDF_ATTR_XP = ".//*[@pdfsrc] | .//*[@swsrc] | .//*[@data-pdfsrc]"
IFRAME_XP = ".//iframe[@src]"


def _is_real_file_url(u):
    """路径必须指向真实文件，根路径/空路径一律不算。

    模板里的占位属性（`swsrc="/"`）解析出来就是站点首页，收进数据后会伪装成
    "附件发生了变化"，是最难查的一类噪声。
    """
    if not u:
        return False
    path = urllib.parse.urlparse(u).path
    return len(path) > 1 and path != "/"


def _block_text(el):
    """把块级标签还原成换行，得到保留段落的纯文本。"""
    s = LH.tostring(el, encoding="unicode")
    s = re.sub(r"<(br|/p|/div|/li|/tr|/h[1-6]|/table)[^>]*>", "\n", s, flags=re.I)
    try:
        node = LH.fromstring(f"<div>{s}</div>")
    except Exception:                                  # noqa: BLE001
        return common.text(el)
    lines = [ln.strip() for ln in node.text_content().split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _cleaned_body(doc):
    """通用兜底：清掉版式噪声后，取剩余文本最多的块。

    为什么需要它：高校 CMS 的正文容器类名五花八门（pageArticle / page_article /
    item_content / sub_content / article…），靠一个个加选择器永远追不完。
    实测 16 个失败里 9 个是"选择器没覆盖"，不是"页面没有正文"。
    兜底不追求完美，追求"别再整条丢掉" —— 多带一点导航文字，比一条记录正文为空好得多。

    用深拷贝，不改原树（同一个 doc 后面还要用来找标题、发布时间、附件）。
    """
    import copy
    try:
        node = copy.deepcopy(doc)
    except Exception:                                  # noqa: BLE001
        return None
    for bad in node.xpath(NOISE_XP):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)
    best = None
    for el in node.xpath("//div | //article | //section | //td"):
        t = " ".join(el.text_content().split())
        if len(t) < 200:                               # 太短的不可能是正文
            continue
        # 同长度时取层级更深的（更接近叶子，噪声更少）
        key = (len(t), len(el.xpath("ancestor::*")))
        if best is None or key > best[0]:
            best = (key, el)
    return best[1] if best else None


# 二进制文件的魔数。踩过的坑：lxml 的 HTML 解析器**极其宽容**，
# 把一份 PDF 的二进制内容也能解出一棵树，于是 `parse_detail` 会返回
# `why=no_body`（"这页没正文"），而真相是"这根本不是网页"。
# 两种情况对排查的意义完全不同：前者要改选择器，后者要改调用方。
BINARY_MAGIC = (b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x89PNG", b"\xff\xd8\xff")


def _strip_leading_title(body, title):
    """正文首行常常就是标题本身 —— 很多 CMS 把标题也渲染进正文容器。

    留着会让"标题 + 正文第一行"同屏出现两遍。
    **在抓取侧去掉比在前端兜底好**：B 做套磁信引用、匹配打分、变更检测都读 body_text，
    在这里清一次，所有下游同时受益；放前端就等于每个消费者各修一遍。

    保守处理：只在**首行与标题去空白后完全一致**时才删，不做模糊匹配 ——
    猜错会把正文第一句吃掉。
    """
    if not body or not title:
        return body
    lines = body.split("\n")
    if lines and "".join(lines[0].split()) == "".join(title.split()):
        return "\n".join(lines[1:]).lstrip("\n")
    return body


def parse_detail(raw, url):
    if raw and any(raw.startswith(m) for m in BINARY_MAGIC):
        return {"title": "", "publish_date": None, "source": None, "body_text": "",
                "attachments": [], "why": "is_binary_file", "body_how": "none"}

    doc = common.parse(raw)
    if doc is None:
        return None

    # **整页完整性检查：看起来被截断的，不许当成"我读全了"。**
    #
    # 坑（已踩，2026-10-17 实测出来的）：连接中途被 RST、或者代理提前结束响应时，
    # `r.read()` **可能正常返回一段半截的 HTML**，不抛异常。而 lxml 极其宽容，
    # 半截 HTML 照样能解出一棵树、正文照样能抽出来 —— 于是下游看到的是
    # "选择器命中 + 正文 821 字"，**看起来完全正常**。
    #
    # 后果：我们会对用户说「该公告未公布截止日」，而那句话是建立在一段残页上的。
    # 实测把一个 1400 字的正文截到 232 字，`body_state` 照样是 `ok`。
    #
    # 判据：这些 CMS 的页面**都以 `</html>` 收尾**。实测 190/190 条缓存页面都有
    # （零误报），所以"没有 `</html>`"就是一个便宜且可靠的截断信号。
    # 代价：真有站点不写 `</html>` 时会保守退化成 `unknown` —— 那是**对的方向**
    # （宁少不假：说不准就退到"我们判不了"）。
    html_truncated = bool(raw) and b"</html>" not in raw[:].lower()

    body_el = doc.xpath(BODY_XP)
    how = "selector"
    if body_el:
        body_el = max(body_el, key=lambda e: len(e.text_content()))
    else:
        # 没有任何已知选择器命中 —— 走通用兜底，别直接把这整条丢掉
        body_el = _cleaned_body(doc)
        how = "generic" if body_el is not None else "none"
    if body_el is None:
        # 连兜底都找不到正文：**不要返回 None**，返回一条"有标题、没正文"的记录，
        # 让 crawl 能把它归到"页面无正文"这一类，而不是混进"解析失败"里。
        # 失败必须可分类，否则永远查不清。
        return {"title": "", "publish_date": None, "source": None,
                "body_text": "", "attachments": [], "why": "no_body",
                "body_how": "incomplete" if html_truncated else "none"}
    # 截断优先于 selector/generic —— 正文可能只到了一半，来源再"正规"也不能算证据
    if html_truncated:
        how = "incomplete"

    body = _block_text(body_el)

    title_el = doc.xpath(TITLE_XP)
    title = common.text(title_el[0]) if title_el else ""
    if not title:
        # 兜底：<title> 常写成「标题_栏目_学院」或「标题 - 学院」，取标题那一段。
        #
        # 坑（已踩，而且是**肉眼可见**的）：原来写的是 re.split(r"[_|\-—]", ...)，
        # 把 `-` 也当分隔符。而中文公告标题里 `-` 极常见 —— 学年区间就是 `2026-2027`：
        #     <title> 南开大学2026-2027学年第一学期申请答辩学术博士平台评审名单
        #     → 标题被切成 `南开大学2026`
        # 实测 78 条里 4 条标题被切坏，而且因为列表按日期排，
        # **这 4 条正好占据了公告列表的前四位**——评委打开列表页看到的就是它们。
        # 单元测试测不出"标题难看"，门禁当时也没有这条不变量，只有真跑起来看页面才发现。
        #
        # 正确判据：**区分"标题里的连字符"和"站点后缀的分隔符"**。
        #   2026-2027            → 连字符两边都是**数字**，是年份区间，属于标题
        #   名单公示-招生网       → 两边都是**非数字**，是站点后缀，要切掉
        #   通知 - 研究生院       → 两边有空格，更要切
        # 按这个顺序试，第一个能切出"长度合理的前半段"的规则生效。
        t = doc.xpath("//title")
        if t:
            raw = common.text(t[0])
            cand = raw
            for pat in (r"\s+[_|—\-]\s+",              # 「标题 - 学院」
                        r"(?<=[^\d\s])[_|—\-](?=\D)",  # 「标题-招生网」；数字-数字不算
                        r"_"):                          # CMS 的「标题_栏目_学院」
                parts = re.split(pat, cand, maxsplit=1)
                if len(parts) > 1 and len(parts[0].strip()) >= 8:
                    cand = parts[0].strip()
                    break
            if 8 <= len(cand) <= 100:
                title = cand
    # 少数模板把日期直接拼在标题末尾；列表解析已有同一清理规则，详情兜底也要保持一致。
    title = re.sub(r"[\\s　]*\\d{4}(?:[-/.年]\\s*)\\d{1,2}(?:[-/.月]\\s*)\\d{1,2}日?[\\s　]*$", "", title).strip()
    body = _strip_leading_title(body, title)
    head_el = doc.xpath(HEAD_XP)

    head = common.text(head_el[0]) if head_el else ""
    # 发布日按**可信度**依次取，**绝不在任意文本里抓"第一个像日期的"**。
    #
    # 坑（已踩，而且这个比截止日严重）：原来最后一步是 `common.iso_date(head)` ——
    # 没匹配到标签时，就在整块 head 文本里抓第一个日期。而 `HEAD_XP` 在某些模板上
    # 会框进正文的一部分，于是**把正文里的日期当成了发布日**。
    # 实测南开这条：页面真实发布日是 2026-04-13（URL 和页面上都写着），
    # 我们存成了 **2026-05-14** —— 那是正文里「材料审核结果发布时间：2026年5月14日前」。
    # `publish_date` 既是排序键又是展示字段，等于给用户看了一个错的日期；
    # 而且它还反向害了截止日：截止日必须 ≥ 发布日，发布日被推后 31 天，
    # 正文里 2026-04-28 的报名截止就被这条噪声闸**正确地**拒掉了 ——
    # **一个错字段把另一个正确字段也拖下水，报出来却像"截止日抽不到"。**
    # `更新时间` 必须在这里（2026-10-18 补）：
    # 吉林大学研招网的页面写的是 `<span>更新时间：2025-07-02</span>`，
    # 而原来的标签集合只有「更新日期」—— 于是 43 条公告的发布日全是空。
    # 这不是"日期不存在"，是**我们不认这个写法**。标签本身足够专指，
    # 不会像裸「时间：」那样匹配到「截止时间」，所以放心加。
    DATE_LABEL = (r"(?:发布时间|发布日期|更新日期|更新时间|更新于|发表时间|刊登时间|"
                  r"录入时间|发布于)")
    m = re.search(DATE_LABEL + r"[：:]\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2})", head)
    publish = common.iso_date(m.group(1)) if m else None
    if not publish:
        # 标签在正文区、没有被 HEAD_XP 框住时（吉大就是这种）：
        # 在整个文档里找**带标签**的日期，仍然不碰"第一个像日期的字符串"。
        whole = common.text(doc)
        m = re.search(DATE_LABEL + r"[：:]\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2})", whole)
        publish = common.iso_date(m.group(1)) if m else None
    if not publish:
        # 维普 / 南湖之舟系把发布日**写在 URL 里**：/2026/0413/c83a592868/page.htm
        # 这个信号一直没用上，它比"在文本里找日期"可靠得多。
        m = re.search(r"/(20\d{2})/(\d{2})(\d{2})/", url or "")
        if m:
            publish = common.iso_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    if not publish:
        # 最后才看正文开头（北大院系站把发布时间写在正文第一行）。
        #
        # 坑（已踩，而且是**修上一个坑时新踩的**）：这里一度写成裸 `时间[：:]`，
        # 结果匹配上了「**截止**时间：2026年4月28日」——
        # 把报名截止日当成了发布日。**放宽一个正则的时候，
        # 要顺手问一句"它还能匹配到哪些我不想要的东西"。**
        # 所以这里只认上面那些明确标签，且窗口限在正文前 400 字。
        m = re.search(DATE_LABEL + r"[：:]\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2})", body[:400])
        publish = common.iso_date(m.group(1)) if m else None
    src = re.search(r"来源[：:]\s*([^\s\|]{1,30})", head or body[:400])

    attachments = []
    seen_url = set()

    def add_att(name, href, kind="file"):
        if not href or href in seen_url:
            return
        seen_url.add(href)
        attachments.append({"name": name or href.rsplit("/", 1)[-1], "url": href,
                            "parsed": False, "kind": kind})

    for a in body_el.xpath(".//a[@href]"):
        href = common.norm_url(url, a.get("href"))
        if href and FILE_EXT.search(href):
            add_att(common.text(a), href)

    # 内嵌 PDF 阅读器（自定义属性）
    #
    # 坑（已踩）：模板里常留 `swsrc="/"` 这类占位值，norm_url 一解析就成了站点首页
    # `https://graduate.nankai.edu.cn/`，被当成附件收进数据。
    # 处理：① **不收 swsrc** —— 它是同一份文档的 flash/视频版，收进来只会和 pdfsrc 重复；
    #       ② 解析后的路径必须是真实文件路径，根路径和空路径一律丢弃。
    for node in doc.xpath(PDF_ATTR_XP):
        target = node.get("pdfsrc") or node.get("data-pdfsrc")
        src_url = common.norm_url(url, target) if target else None
        if _is_real_file_url(src_url):
            add_att(None, src_url, kind="pdf_embed")

    for node in doc.xpath(IFRAME_XP):
        # 变量名必须和上面的 src(来源正则) 区分开 —— 见下方 return 前的教训注释
        iframe_src = common.norm_url(url, node.get("src"))
        # 坑（已踩）：不能把所有 <iframe> 都当附件。真实案例：南开大学研究生院的详情页
        # 里有个指向站点自身首页的导航 iframe，在 changes.md 里表现为"附件变了"，是纯噪声。
        # 只有看起来确实是文档/阅读器、且路径非根的 iframe 才算。
        if _is_real_file_url(iframe_src) and (
                FILE_EXT.search(iframe_src)
                or re.search(r"pdf|viewer|player|preview|office", iframe_src, re.I)):
            add_att(None, iframe_src, kind="iframe")

    # 坑（已踩，代价是整轮爬取崩掉）：上面 iframe 那段我最初把变量写成 `src`，
    # 直接遮蔽了第 72 行的 `src = re.search(来源...)`。于是 `src.group(1)` 报
    # AttributeError: 'str' object has no attribute 'group'，整个 crawl 挂掉。
    # 教训有两条：
    #   1. 一个函数里 `src` 这种短名只能有一个含义；
    #   2. 崩溃发生在写文件之前，所以已有数据没被破坏 —— **先算完再落盘**这个顺序救了数据。
    return {
        "title": title,
        "publish_date": publish,
        "source": src.group(1) if src else None,
        "body_text": body,
        "attachments": attachments,
        "body_how": how,          # selector / generic / none —— 用于评估选择器覆盖率
    }

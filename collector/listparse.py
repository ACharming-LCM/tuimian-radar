# -*- coding: utf-8 -*-
"""列表页解析：从任意 CMS 列表页里自动认出"公告条目"。

不写死选择器，靠结构特征打分：
  一个容器里 >=5 个直接子元素，其中 >=4 个同时含 <a href> 和日期 —— 它就是列表。

归属：A（数据管道）。
"""
import re

import common

DATE_IN_TEXT = re.compile(r"20\d{2}\s*[-/.年]\s*\d{1,2}")


def _children(el):
    # 大多数高校 CMS 用 li/tr/dl；上交研招网的公告列表则是
    # `<div class="announcement-list"><a class="item">…</a>…</div>`。
    # 这里只放宽到**直接子 a**，不扫所有后代，避免把整页导航误判成公告列表。
    return (el.xpath("./li") or el.xpath("./tr") or el.xpath("./dl")
            or el.xpath("./a[@href]"))


def _anchors(el):
    """列表项自身可以就是 `<a>`，也可以在内部包含 `<a>`。"""
    return el.xpath("self::a[@href] | .//a[@href]")


def _looks_like_item(el):
    if not _anchors(el):
        return False
    t = common.text(el)
    if DATE_IN_TEXT.search(t):
        return True
    return bool(el.xpath(".//span[contains(@class,'d')] | .//*[contains(@class,'date')]"))


def detect_list_container(doc):
    """返回得分最高的列表容器 element，找不到返回 None。"""
    best = None
    for el in doc.xpath("//ul | //div | //table"):
        kids = _children(el)
        if len(kids) < 5:
            continue
        hit = sum(1 for k in kids[:12] if _looks_like_item(k))
        if hit < 4:
            continue
        # 命中数优先；同分时子元素少的更"纯"（避免选到整个页面大 wrapper）
        key = (hit, -len(kids))
        if best is None or key > best[0]:
            best = (key, el, kids)
    return best


def parse_item(li, base_url):
    a = _anchors(li)
    if not a:
        return None
    href = common.norm_url(base_url, a[0].get("href"))
    if not href:
        return None

    item_text = common.text(li)
    # 上交研招网：`15 2026.10 标题`（日放在年月前面）。标题可能单独放在
    # `<div class="title">`，所以日期必须从**整条 item**取，不能只看标题节点。
    day_first = re.match(r"^\s*(\d{1,2})\s*(20\d{2})[./-](\d{1,2})\s*", item_text)
    if day_first:
        dd, yy, mm = day_first.groups()
        date = f"{yy}-{int(mm):02d}-{int(dd):02d}"
    else:
        date = None

    # 标题：优先 a/@title，其次 div.info/title 这类纯标题区，最后退化为 a 的文本
    title = (a[0].get("title") or "").strip()
    if not title:
        info = li.xpath(".//div[contains(@class,'info')] | .//*[contains(@class,'title')]")
        title = common.text(info[0]) if info else common.text(a[0])
    if day_first and title.startswith(day_first.group(0).strip()):
        title = title[len(day_first.group(0).strip()):].lstrip()
    title = re.sub(r"^\s*[\[\(（]?\d{1,2}[\]\)）]?\s*", "", title)   # 去掉前缀序号
    # 去掉**尾部日期**。有些站的列表把日期和标题放在同一个 <a> 里，
    # 于是标题变成「北京大学2027年…招生简章 2026-10-09」。
    # 只有真跑起来看列表页才会注意到 —— 单元测试不会觉得"标题后面带个日期"有什么不对。
    title = re.sub(r"[\s　]*\d{4}(?:[-/.年]\s*)\d{1,2}(?:[-/.月]\s*)\d{1,2}日?[\s　]*$", "", title)
    # 私用区图标（图标字体）先删掉：它们看着是空白，落盘后是脏字符，
    # 在 GBK 环境里还会让 print 抛 UnicodeEncodeError。
    title = common.clean_text(title)
    title = re.sub(r"\s+", " ", title).strip(" -|·")
    if len(title) < 2:
        return None

    # 日期：先试 CMS 常见的 span.d(日) + span.y(年-月)，再退化到正则
    d = li.xpath(".//span[contains(@class,'d')]/text()")
    y = li.xpath(".//span[contains(@class,'y')]/text()")
    if d and y:
        dd = re.sub(r"\D", "", d[0])
        ym = re.sub(r"\D", "-", y[0].strip())
        if dd and re.match(r"20\d{2}-\d{1,2}", ym):
            date = f"{ym.split('-')[0]}-{int(ym.split('-')[1]):02d}-{int(dd):02d}"
    if not date:
        date = common.iso_date(common.text(li))

    return {"title": title, "url": href, "publish_date": date}


def pagination_urls(raw, base_url, max_pages=4):
    """从第一页里找**明确写出来的分页链接**，最多返回 `max_pages` 个 URL（含首页）。

    不猜 `list2.htm` / `?page=2`：各校 CMS 规则不一致，猜错会把文章页当分页。
    只接受页码文字（2、3…）或“下一页”，并要求 URL 与首页同 host；数字页优先，
    因此北大 `index1.htm`（文字为2）和川大 `/gg/2` 都能工作。
    """
    if max_pages <= 1:
        return [base_url]
    doc = common.parse(raw)
    if doc is None:
        return [base_url]
    numbered, next_links = [], []
    base_host = common.canonical_url(base_url).split("/", 1)[0]
    for a in doc.xpath("//a[@href]"):
        text = common.text(a).strip()
        href = common.norm_url(base_url, a.get("href"))
        if not href or common.canonical_url(href).split("/", 1)[0] != base_host:
            continue
        number = re.fullmatch(r"(?:页面\s*)?(\d+)", text)
        if number and 2 <= int(number.group(1)) <= max_pages:
            numbered.append((int(number.group(1)), href))
        elif re.fullmatch(r"(?:下一页|下页|后页)\s*[>›»]*|[>›]", text):
            next_links.append(href)
    # 上交用 laypage 在浏览器生成页码；只读取源码明确给出的总量、页大小和跳转模板。
    source = common.decode(raw)
    template = re.search(r'let\s+url\s*=\s*"(https?://[^"<>]+\?page=)"\s*\+\s*obj\.curr', source)
    totals = re.findall(r"var\s+total\s*=\s*['\"](\d+)['\"]", source)
    limits = re.findall(r"var\s+limit\s*=\s*['\"](\d+)['\"]", source)
    if template and totals and limits and int(limits[-1]) > 0:
        last = (int(totals[-1]) + int(limits[-1]) - 1) // int(limits[-1])
        prefix = template.group(1)
        if common.canonical_url(prefix).split('/', 1)[0] == base_host:
            numbered.extend((n, prefix + str(n)) for n in range(2, min(max_pages, last) + 1))
    urls, seen = [base_url], {common.canonical_url(base_url)}
    for _, url in sorted(numbered):
        key = common.canonical_url(url)
        if key not in seen:
            seen.add(key)
            urls.append(url)
        if len(urls) >= max_pages:
            break
    if len(urls) == 1 and next_links:
        key = common.canonical_url(next_links[0])
        if key not in seen:
            urls.append(next_links[0])
    return urls


def parse_list_page(raw, base_url):
    doc = common.parse(raw)
    if doc is None:
        return []
    got = detect_list_container(doc)
    if not got:
        return []
    _, _, kids = got
    items, seen = [], set()
    for li in kids:
        it = parse_item(li, base_url)
        if it and it["url"] not in seen:
            seen.add(it["url"])
            items.append(it)
    return items

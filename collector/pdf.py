# -*- coding: utf-8 -*-
"""附件正文抽取：把 PDF / DOCX / XLSX 附件变成文本，补回"正文为空"的公告。

为什么必须做：国内高校 CMS 常把公示文件做成**内嵌 PDF 阅读器**
（`<div pdfsrc="....pdf">`），HTML 正文里一个字都没有。
实测浙大材料学院两份《推免工作细则》正文长度都是 0 —— 但 PDF 里各有 3397 字，
而这恰恰是全校最该被公示、用户最需要的一份文件。

XLSX 同理，而且更极端：南开研究生院的《部分录取名单公示》HTML 正文只有
一个附件文件名（body_len 二三十字），**名单公示的正文就是那张表**——
录取的人、学院、专业全在 xlsx 里，不抽等于这条公告没有内容。

依赖降级设计：fitz(PyMuPDF) → pypdf → 都没有就标记"未解析"，绝不抛异常打断整条管道。
本机 `D:\python.3.10.11` 三个库都有（fitz 1.27 / pdfplumber 0.11 / pypdf 6.19）。

归属：A。入口签名（B 不要改）：
    extract(raw, name) -> (text, note)
    enrich(record, refresh=False) -> dict   # 原地补 body_text / attachments，返回统计
"""
import os
import re

import common

MAX_PAGES = 40          # 公告 PDF 很少超过 40 页；超了后面的内容对匹配也没用
MAX_CHARS = 24000       # 单份附件抽取上限，防止一条记录把 JSONL 撑爆
MAX_ROWS = 500          # 单个 sheet 最多抽多少行：名单表动辄几千行，全抽进来既没用又撑爆 JSONL
DOC_EXTS = (".pdf", ".docx", ".xlsx")
# 注意 .xls（BIFF 老格式）留在跳过名单里：openpyxl 只认 OOXML，硬读会抛异常
SKIP_EXTS = (".doc", ".xls", ".ppt", ".pptx", ".zip", ".rar", ".swf")


def _ext(url):
    return os.path.splitext(url.split("?")[0])[1].lower()


def _page_text(page):
    """一页 PDF → 文本，**表格按行拼接**。

    坑（已踩，而且是肉眼可见的级别）：原来直接用 `page.get_text()`。
    表格在这种取法下会变成**一个单元格一行**：

        序号 / 学院 / 姓名 / 学号 / 专业 / 1 / 化学学院 / 王某某 / 2120230000 / ...

    后果有两个，都是 C 走查时先发现的（`journal/2026-10-17-C-走查.md` 的 #1 和 #6）：
      · 公告详情页正文变成一列竖排的词，根本没法读；
      · 套磁信从正文里挑引用句，只能挑到碎片（有的一条引文只有一个词「论文」）。

    修法：PyMuPDF 1.27 有 `find_tables()`。把**表格和普通文字块按纵向坐标排序后交错输出**，
    表格按行拼成 `a | b | c`，表格以外的块原样保留。

    顺带说明为什么不能只在渲染层修：正文是**数据**，数据里就是一列竖排的词，
    前端再怎么渲染也拼不回一行 —— 根因在抽取，不在这。
    """
    try:
        tabs = page.find_tables().tables
    except Exception:                                      # noqa: BLE001
        tabs = []
    if not tabs:
        return page.get_text()                             # 没有表格就走原来的路

    import fitz

    def inside(rect):
        """块的中心落在某个表格里 → 算表格的一部分，别再单独输出一遍。"""
        cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
        for t in tabs:
            r = fitz.Rect(t.bbox)
            if r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1:
                return True
        return False

    items = [(t.bbox[1], "t", t) for t in tabs]
    for b in page.get_text("blocks"):
        if not inside(fitz.Rect(b[:4])):
            items.append((b[1], "b", b))
    items.sort(key=lambda x: x[0])

    out = []
    for _, kind, obj in items:
        if kind == "t":
            for row in obj.extract():
                cells = [" ".join((c or "").split()) for c in row]
                cells = [c for c in cells if c]
                if cells:
                    out.append(" | ".join(cells))
            out.append("")
        else:
            txt = " ".join((obj[4] or "").split())
            if txt:
                out.append(txt)
    return "\n".join(out)


LIST_MARKER_RE = re.compile(r"^\s*(?:[（(]\s*[一二三四五六七八九十\d]+\s*[)）]"
                            r"|[一二三四五六七八九十]+\s*、|\d+\s*[.、)）]"
                            r"|[①-⑳]|[A-Za-z]\s*[.、)])")
SENT_END = "。！？!?；;：:"


def normalize_soft_linebreaks(text):
    """合并PDF排版产生的**软换行**，把被折断的一句话接回一行。

    判据（三条同时成立才合并）：
      1. 上一行确实以换行结尾，且两行都不是空行 —— 空行是段落分隔，不合并；
      2. 上一行末字不是句末标点（。！？；：等）—— 那句话已经结束了；
      3. 下一行开头是"接续"的形态：小写字母、数字、中文数字，或普通汉字，
         且不是列表/小标题编号（（一）、1.、一、、①…）。

    为什么第 3 条要包含普通汉字（超出"小写/数字/中文数字"）：真实附件里
    「2027 年骨干计划资格申请平台开\n放时间为…」是在**词中间**断的，
    下一行以「放」开头。不合并的话，"开放时间"被拆成两句，关键词和日期各在一边，
    这条截止日就永远抽不出来。段落之间通常是空行，第 1 条已经把它们挡住了。
    """
    if not text:
        return text
    out = []
    for line in text.splitlines(True):
        merged = False
        if out:
            prev_raw = out[-1]
            prev = prev_raw.rstrip("\r\n")
            cur = line.lstrip("\r\n")
            cur_body = cur.strip()
            if (prev_raw != prev and prev.strip() and cur_body
                    and prev.rstrip()[-1] not in SENT_END
                    and not LIST_MARKER_RE.match(cur_body)):
                head = cur_body[0]
                if (head.islower() or head.isdigit()
                        or "一二三四五六七八九十百千万〇零".find(head) >= 0
                        or "\u4e00" <= head <= "\u9fff"):
                    # 汉字之间不插空格：插了会把「开/放时间」变成「开 放时间」，
                    # 关键词就再也匹配不上——这正是南京骨干那一条抽不出来的原因。
                    a_ch, b_ch = prev.rstrip()[-1], cur.lstrip()[0]
                    if a_ch in "-－~～至到" or ("\u4e00" <= a_ch <= "\u9fff"
                                              and "\u4e00" <= b_ch <= "\u9fff"):
                        joiner = ""
                    else:
                        joiner = " "
                    out[-1] = prev.rstrip() + joiner + cur.lstrip()
                    merged = True
        if not merged:
            out.append(line)
    return "".join(out)


def extract_pdf(raw):
    """返回 (text, note, pages)。"""
    if not raw or not raw.startswith(b"%PDF"):
        return "", "不是 PDF 文件", 0

    # ① PyMuPDF —— 最快最稳
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        try:
            n = doc.page_count
            pages = [_page_text(doc[i]) for i in range(min(n, MAX_PAGES))]
        finally:
            doc.close()
        txt = normalize_soft_linebreaks("\n".join(pages))
        if txt.strip():
            return txt[:MAX_CHARS], None, n
        return "", "扫描版，无文本层（需 OCR，本机无引擎）", n
    except ImportError:
        pass
    except Exception as e:                                  # noqa: BLE001
        note = f"PyMuPDF 解析异常：{type(e).__name__}"
    else:
        note = None

    # ② pypdf —— 纯 Python 后备
    try:
        import io as _io

        from pypdf import PdfReader
        reader = PdfReader(_io.BytesIO(raw))
        n = len(reader.pages)
        txt = normalize_soft_linebreaks("\n".join((reader.pages[i].extract_text() or "")
                        for i in range(min(n, MAX_PAGES))))
        if txt.strip():
            return txt[:MAX_CHARS], None, n
        return "", "扫描版，无文本层（需 OCR，本机无引擎）", n
    except ImportError:
        return "", "没装 PDF 解析库（PyMuPDF / pypdf 都没有）", 0
    except Exception as e:                                  # noqa: BLE001
        return "", f"解析失败：{type(e).__name__}", 0


def extract_docx(raw):
    try:
        import io as _io

        import docx
        d = docx.Document(_io.BytesIO(raw))
        txt = "\n".join(p.text for p in d.paragraphs if p.text.strip())
        for t in d.tables:
            for row in t.rows:
                txt += "\n" + " | ".join(c.text.strip() for c in row.cells)
        return (txt[:MAX_CHARS], None, 0) if txt.strip() else ("", "文档无文本内容", 0)
    except ImportError:
        return "", "没装 python-docx", 0
    except Exception as e:                                  # noqa: BLE001
        return "", f"解析失败：{type(e).__name__}", 0


def extract_xlsx(raw):
    """返回 (text, note, pages)。pages 恒为 0（表格没有页的概念）。

    为什么必须抽：名单公示类公告的 HTML 正文就是一个附件文件名，
    **正文其实就是那张表**——不抽，这条公告在数据集里等于没有内容。

    格式约定：行内单元格用 " | " 连接（保住"姓名 | 学院 | 专业"的列关系），
    每个 sheet 前加一行 `【工作表：名字】`，空行整行跳过（合并单元格、
    表尾空白都会产生大量空行）。单 sheet 最多 MAX_ROWS 行、全文最多 MAX_CHARS 字。
    """
    try:
        import io as _io

        import openpyxl
        # read_only=True：名单表几千行，全量建对象太慢；data_only=True：取公式的缓存结果，
        # 名单里若有人名是拼出来的也不会拿到 "=A2&B2" 这种字符串。
        wb = openpyxl.load_workbook(_io.BytesIO(raw), read_only=True, data_only=True)
    except ImportError:
        return "", "没装 openpyxl", 0
    except Exception as e:                                  # noqa: BLE001
        return "", f"解析失败：{type(e).__name__}", 0

    try:
        parts = []
        truncated = False
        for ws in wb.worksheets:
            parts.append(f"【工作表：{ws.title}】")
            rows = 0
            for row in ws.iter_rows(values_only=True):
                cells = [str(v).strip() for v in row
                         if v is not None and str(v).strip()]
                if not cells:                               # 空行跳过
                    continue
                parts.append(" | ".join(cells))
                rows += 1
                if rows >= MAX_ROWS:
                    truncated = True                        # 只认"行数上限被撞到"，不看 ws.max_row
                    break                                   # （表尾格式化残留会虚报 max_row）
        txt = "\n".join(parts)
        if not txt.strip():
            return "", "表格无文本内容", 0
        if truncated:
            return txt[:MAX_CHARS], f"已截断（单表只取前 {MAX_ROWS} 行）", 0
        if len(txt) > MAX_CHARS:
            return txt[:MAX_CHARS], f"已截断（全文只取前 {MAX_CHARS} 字）", 0
        return txt, None, 0
    except Exception as e:                                  # noqa: BLE001
        return "", f"解析失败：{type(e).__name__}", 0
    finally:
        wb.close()


def extract(raw, name=""):
    ext = _ext(name or "")
    if ext == ".pdf":
        return extract_pdf(raw)
    if ext == ".docx":
        return extract_docx(raw)
    if ext == ".xlsx":
        return extract_xlsx(raw)
    return "", f"暂不支持的格式 {ext or '未知'}", 0


def enrich(record, refresh=False, verbose=True):
    """把记录里所有可解析附件的正文抽出来，接到 body_text 后面。

    **必须在 fields.extract_fields 之前调用** —— 截止日期、专业、材料清单
    大量藏在附件里，先补正文再抽字段，命中率完全不同。

    返回统计 dict。
    """
    stats = {"tried": 0, "ok": 0, "scanned": 0, "failed": 0, "chars": 0}
    added = []

    for att in record.get("attachments", []):
        url = att.get("url") or ""
        ext = _ext(url)
        if ext in SKIP_EXTS or ext not in DOC_EXTS:
            att.setdefault("note", f"暂不解析 {ext or '未知格式'}")
            att["text_len"] = 0
            continue

        stats["tried"] += 1
        raw, _ = common.fetch_binary(url, refresh=refresh)
        if not raw:
            att["parsed"], att["text_len"] = False, 0
            att["note"] = "下载失败"
            stats["failed"] += 1
            continue

        text, note, pages = extract(raw, url)
        att["text_len"] = len(text)
        att["parsed"] = bool(text)
        att["note"] = note

        if text:
            stats["ok"] += 1
            stats["chars"] += len(text)
            fname = att.get("name") or url.rsplit("/", 1)[-1]
            added.append(f"\n\n【附件正文：{fname}】\n{text}")
        elif note and "扫描版" in note:
            stats["scanned"] += 1
        else:
            stats["failed"] += 1

    if added:
        record["body_text"] = (record.get("body_text") or "") + "".join(added)
        record["body_len"] = len(record["body_text"])

    return stats

# -*- coding: utf-8 -*-
"""附件抽取回归测试：xlsx 名单表能抽出来 + 原来的 PDF 路径没被改坏。

不依赖 pytest，直接跑：

    cd collector && python tests/test_pdf.py

为什么要它：名单公示类公告的 HTML 正文只有一个附件文件名，
**正文其实就是那张 xlsx 表**——抽不出来这条公告在数据集里就是空的。
而这次改动是把 `.xlsx` 从 SKIP_EXTS 挪进 DOC_EXTS、又在 extract() 里加了分支，
动的是 enrich() 的分发路径，很容易顺手把 `.pdf` / `.docx` 那两路弄坏，
所以两个方向都要有断言。

数据是真的：JSONL 里**所有** xlsx 附件（含南开那 3 条名单公示）都自动找出来，
走 common.fetch_binary（命中本地缓存就不联网）+ pdf.extract，不做 mock。

归属：A 维护，B 加案例随便加，**但不要把已有的案例删掉改成通过**。
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common          # noqa: E402
import pdf             # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILED.append(name)


print("== PDF软换行归一化 ==")


def check_text(name, got, want):
    """注意：本文件的 check 是 (name, ok, detail)，别拿它比字符串 —— 非空串永远真。"""
    check(name, got == want, f"期望 {want!r} 实际 {got!r}")


check_text("日期区间软换行合并（全角连接号不插空格）",
           pdf.normalize_soft_linebreaks("开放时间为 2026-10-08－\n10-21"),
           "开放时间为 2026-10-08－10-21")
check_text("句末标点后的换行保留",
           pdf.normalize_soft_linebreaks("第一句。\n第二句"),
           "第一句。\n第二句")
check_text("词中间的汉字软换行合并（真实附件：开/放时间）",
           pdf.normalize_soft_linebreaks("申请平台开\n放时间为2026 年10 月8 日。"),
           "申请平台开放时间为2026 年10 月8 日。")
check_text("数字开头的软换行合并（三、报名时间 / 9 月14 日）",
           pdf.normalize_soft_linebreaks("三、报名时间 \n9 月14 日8:00 至9 月28 日17:00。"),
           "三、报名时间 9 月14 日8:00 至9 月28 日17:00。")
check_text("空行是段落分隔，不合并",
           pdf.normalize_soft_linebreaks("第一段没有句号\n\n第二段"),
           "第一段没有句号\n\n第二段")
check_text("下一行是列表编号时不合并",
           pdf.normalize_soft_linebreaks("报名材料如下\n（一）申请表"),
           "报名材料如下\n（一）申请表")


def build_xlsx(sheets):
    """sheets: [(表名, [[单元格, ...], ...]), ...] -> xlsx 字节。"""
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets:
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def load_notices():
    p = os.path.join(common.ROOT, "data", "out", "notices.jsonl")
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def find_attachments(notices, ext, kind=None, parsed_only=False):
    """按扩展名（可选 kind）从真实数据里挑附件，返回 [(记录, 附件), ...]。"""
    hits = []
    for rec in notices:
        for att in rec.get("attachments", []):
            url = att.get("url") or ""
            if os.path.splitext(url.split("?")[0])[1].lower() != ext:
                continue
            if kind and att.get("kind") != kind:
                continue
            if parsed_only and not att.get("parsed"):
                continue
            hits.append((rec, att))
    return hits


print("== xlsx 抽取格式（本地合成，不依赖网络）==")
raw = build_xlsx([("名单", [["序号", "学院", "姓名"],
                            [1, "物理学院", "张三"],
                            [None, None, None],                 # 空行要被跳过
                            [2, "材料学院", "李四"]]),
                  ("说明", [["备注", "以学校公示为准"]])])
text, note, pages = pdf.extract(raw, "demo.xlsx")
check("合表抽出非空文本", bool(text.strip()), f"{len(text)} 字 / note={note!r}")
check("行内用 ' | ' 分隔", "1 | 物理学院 | 张三" in text)
check("每个 sheet 有标题行",
      "【工作表：名单】" in text and "【工作表：说明】" in text)
check("sheet 标题在各自内容之前",
      text.index("【工作表：名单】") < text.index("张三")
      < text.index("【工作表：说明】") < text.index("以学校公示为准"))
check("空行整行跳过", "\n\n" not in text, f"换行结构={text!r}")
check("没截断时 note 为空", note is None, f"note={note!r}")
check("xlsx 没有页码概念", pages == 0, f"pages={pages}")

print("\n== xlsx 行列上限保护（MAX_ROWS / MAX_CHARS）==")
big = build_xlsx([("大表", [["序号", "姓名"]] + [[i, f"学生{i}"] for i in range(1, 700)])])
text, note, pages = pdf.extract(big, "big.xlsx")
lines = text.split("\n")
# 1 行 sheet 标题 + MAX_ROWS 行数据（表头也算一行）
check("单表最多 MAX_ROWS 行数据", len(lines) == pdf.MAX_ROWS + 1,
      f"实际 {len(lines)} 行（含 sheet 标题），MAX_ROWS={pdf.MAX_ROWS}")
check("截断位置正确（第 500 行留下、第 501 行丢弃）",
      "学生499" in text and "学生500" not in text)
check("截断时 note 说明原因", bool(note) and "截断" in note, f"note={note!r}")
check("截断不超过 MAX_CHARS", len(text) <= pdf.MAX_CHARS)

print("\n== xlsx 非法输入不抛异常 ==")
text, note, pages = pdf.extract(b"not a zip at all", "broken.xlsx")
check("坏文件返回空文本 + note", text == "" and bool(note), f"note={note!r}")
text, note, pages = pdf.extract(b"", "empty.xlsx")
check("空字节返回空文本 + note", text == "" and bool(note), f"note={note!r}")

print("\n== 真实数据：JSONL 里所有 xlsx 附件 ==")
# 坑（已踩）：这里原来硬断言"必须含 3 条南开『名单公示』xlsx"。
# 后来栏目过滤器把南开 /74（学位管理·申请答辩名单）判为不相关并丢掉，
# 那 3 条记录就不在数据里了 —— 于是测试红了，但**代码是对的、数据也是对的**。
# 教训：**测试要绑在代码的契约上，不能绑在某一天的数据上。**
# 现在的断言是数据无关的：只要 JSONL 里存在 xlsx 附件，它们就必须都能抽出来。
notices = load_notices()
print(f"  notices.jsonl 共 {len(notices)} 条记录")
xlsx_atts = find_attachments(notices, ".xlsx", kind="file")
print(f"  其中 xlsx 附件 {len(xlsx_atts)} 条（数量随栏目配置变化，不做硬断言）")
for rec, att in xlsx_atts:
    print(f"  -- {rec['school']} / {rec['title']}")
    raw, from_cache = common.fetch_binary(att["url"])
    check(f"附件可读（cache={from_cache}）", bool(raw),
          f"{att.get('name')} -> {len(raw) if raw else 0} bytes")
    if not raw:
        continue
    text, note, pages = pdf.extract(raw, att["url"])
    print(f"     抽出 {len(text)} 字 / note={note!r}")
    check("xlsx 抽出字数 > 0", len(text) > 0, f"{att.get('name')} -> {len(text)} 字")
    check("抽出的确是表格（有 ' | ' 列分隔）", " | " in text)

print("\n== 回归：原来的 pdf_embed 路径没被改坏 ==")
pdf_atts = find_attachments(notices, ".pdf", kind="pdf_embed", parsed_only=True)
check("JSONL 里能找到解析成功的 pdf_embed 附件", bool(pdf_atts),
      f"找到 {len(pdf_atts)} 条")
if pdf_atts:
    rec, att = pdf_atts[0]
    print(f"  -- {rec['school']} / {rec['title']}")
    raw, from_cache = common.fetch_binary(att["url"])
    check(f"PDF 可读（cache={from_cache}）", bool(raw),
          f"{att.get('name')} -> {len(raw) if raw else 0} bytes")
    if raw:
        text, note, pages = pdf.extract(raw, att["url"])
        print(f"     抽出 {len(text)} 字 / {pages} 页 / note={note!r}")
        check("pdf_embed 抽出字数 > 0", len(text) > 0, f"{len(text)} 字")

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

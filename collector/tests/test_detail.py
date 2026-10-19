# -*- coding: utf-8 -*-
"""详情页解析的契约测试（离线，走 data/raw 缓存）。不依赖 pytest：

    cd collector && python tests/test_detail.py

为什么需要它：`detail.parse_detail` 的返回结构是**下游一切的入口**，
而它同时要做四件不同的事（找标题、找发布时间、抽正文、收附件），
四种页面形态（article-title / arti_title / 内嵌 PDF / 带 iframe）里任何一条分支写错，
表现都是"整轮爬取崩掉"或"某学校的正文全空"。

真实教训：iframe 那次把局部变量写成 `src`，遮蔽了上面"来源"正则的 `src`，
导致 `src.group(1)` 抛 AttributeError，整轮 crawl 挂掉。测试就是为这种事准备的。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common        # noqa: E402
import detail        # noqa: E402

FAILED = []
SKIPPED = []

KINDS = {"file", "pdf_embed", "iframe", "image"}


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


def load_records():
    path = os.path.join(common.OUT_DIR, "notices.jsonl")
    if not os.path.exists(path):
        print("notices.jsonl 不存在，跳过（先跑一次 crawl.py）")
        return []
    import json
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def verify(rec, tag):
    """对一个真实缓存页跑 parse_detail，逐项校验契约。"""
    url = rec["url"]
    # 坑（已踩）：**文件型记录（.pdf/.docx）不能走 HTML 解析**。
    # 北大研招网把招生简章直接排成 .pdf 链接，这类记录的正文要经 pdf.extract，
    # 测试里必须分开处理，否则测的是"拿 HTML 解析器解 PDF"这种没意义的事。
    if os.path.splitext(url.split("?")[0])[1].lower() in (".pdf", ".docx"):
        import pdf as PDF
        raw, _ = common.fetch_binary(url)
        if not raw:
            SKIPPED.append(f"{tag}（文件无缓存）")
            return
        text, note, _pages = PDF.extract(raw, url)
        check_true(f"[{tag}] 文件型记录能抽出正文", len(text) > 0,
                   f"抽出 {len(text)} 字 / note={note!r}")
        return
    if not os.path.exists(common.cache_path(url)):
        SKIPPED.append(f"{tag}（无缓存）")
        return
    raw, _ = common.fetch(url)
    got = detail.parse_detail(raw, url)

    check_true(f"[{tag}] 返回了结果（不是 None）", isinstance(got, dict),
               f"url={url}")
    if not isinstance(got, dict):
        return

    # 契约字段：body_how 是新增的（selector / generic / file / none）。
    # why 只在"没拿到正文"时出现，所以用子集判断而不是相等。
    want = {"title", "publish_date", "source", "body_text", "attachments", "body_how"}
    check_true(f"[{tag}] 契约字段齐全", want <= set(got), f"实际 {sorted(got)}")
    check_true(f"[{tag}] 返回的是网页而不是文件",
               got.get("why") != "is_binary_file",
               f"why={got.get('why')} —— 这不是网页，测试选错样本了")
    check_true(f"[{tag}] title 是 str", isinstance(got["title"], str))
    check_true(f"[{tag}] body_text 是 str", isinstance(got["body_text"], str))
    # 这两条就是遮蔽 bug 的哨兵：抽到的是字符串或 None，绝不该是 re.Match
    check_true(f"[{tag}] source 是 str 或 None（不是 re.Match）",
               got["source"] is None or isinstance(got["source"], str),
               f"实际 {type(got['source']).__name__} = {got['source']!r}")
    check_true(f"[{tag}] publish_date 是 str 或 None",
               got["publish_date"] is None or isinstance(got["publish_date"], str),
               f"实际 {type(got['publish_date']).__name__}")
    if got["publish_date"] is not None:
        check_true(f"[{tag}] publish_date 形如 YYYY-MM-DD",
                   len(got["publish_date"]) == 10 and got["publish_date"][4] == "-",
                   got["publish_date"])

    atts = got["attachments"]
    check_true(f"[{tag}] attachments 是 list", isinstance(atts, list))
    for a in atts:
        check_true(f"[{tag}] 附件字段齐全且 kind 合法",
                   set(a) == {"name", "url", "parsed", "kind"} and a["kind"] in KINDS,
                   f"实际 {a}")
    # 假附件哨兵：站点根路径不该被当成附件
    check_true(f"[{tag}] 没有把站点根路径当附件",
               all(a["url"].rstrip("/").count("/") > 2 for a in atts),
               f"可疑 {[a['url'] for a in atts if a['url'].rstrip('/').count('/') <= 2]}")


print("== 用真实缓存页面校验 detail.parse_detail 的契约 ==")
records = load_records()
if not records:
    print("✅ 无数据可测（跳过）")
    sys.exit(0)

# 按学校/特征各挑一条，覆盖不同 CMS 形态
picks = []
for key, tag in [("南开大学", "iframe 分支"), ("浙江大学", "article-title 分支"),
                 ("北京大学", "北大物理"), ("哈尔滨工业大学", "哈工大")]:
    hit = next((r for r in records if r.get("school") == key), None)
    if hit:
        picks.append((hit, tag))
# 再补一条带 pd_embed 的
pdf_rec = next((r for r in records if any(a["kind"] == "pdf_embed" for a in r["attachments"])), None)
if pdf_rec:
    picks.append((pdf_rec, "内嵌 PDF 分支"))

print(f"  共挑选 {len(picks)} 条真实记录\n")
for rec, tag in picks:
    verify(rec, tag)

print(f"\n  跳过 {len(SKIPPED)} 条：{SKIPPED}" if SKIPPED else "")
print("== 标题兜底：不能把标题里的连字符当分隔符 ==")
#
# 回归用例。原来 detail.py 的兜底是 re.split(r"[_|\-—]", <title>)[0]，
# 把 `-` 也当分隔符，于是：
#     <title> 南开大学2026-2027学年第一学期申请答辩学术博士平台评审名单
#     → 标题被切成 `南开大学2026`
# 实测 78 条里 4 条被切坏，而且按日期排**正好占住公告列表前四位**。
# 这里用内联 HTML 钉住行为：**TITLE_XP 故意不命中**，强制走兜底分支。
TITLE_CASES = [
    ("南开大学2026-2027学年第一学期申请答辩学术博士平台评审名单",
     "南开大学2026-2027学年第一学期申请答辩学术博士平台评审名单",
     "年份区间里的 `-` 两边没有空格 → 不能切"),
    ("关于2027年推免工作的通知 - 研究生院",
     "关于2027年推免工作的通知",
     "「标题 - 学院」两边有空格 → 要切掉站点后缀"),
    ("2027年硕士研究生招生简章_招生信息_某某大学",
     "2027年硕士研究生招生简章",
     "CMS 的下划线分隔 → 要切"),
]
for raw_title, want, why in TITLE_CASES:
    # 正文要能被 BODY_XP 认出来，否则 parse_detail 会提前返回空标题 ——
    # 第一次写这个用例时用了 <div class="content">，三个用例全返回 ''，
    # 我差点以为修错了。class 用 BODY_XP 里有的 "article"。
    page = (f'<html><head><title>{raw_title}</title></head><body>'
            f'<div class="article"><p>'
            f'这是用来占位的正文内容，长度需要超过正文抽取的最小阈值，'
            f'否则解析函数会认为这个页面没有正文而提前返回。'
            f'</p></div></body></html>')
    # parse_detail 收的是**原始 bytes**（它要靠 magic 判二进制文件），
    # 不能直接喂 str —— 第一次写这个用例时喂了 str，报的是
    # "startswith first arg must be str or a tuple of str, not bytes"。
    got = detail.parse_detail(page.encode("utf-8"), "https://x.example.edu/a.htm")
    global_n = got.get("title")
    if global_n != want:
        FAILED.append(("标题兜底", raw_title))
        print(f"  FAIL  {raw_title}\n        期望 {want!r}\n        实际 {global_n!r}\n        {why}")
    else:
        print(f"  PASS  {raw_title[:34]}\n        → {global_n}")

print("\n== 发布日：优先级与两个踩过的坑 ==")
# 坑一（原来就有的）：`发布时间：` 没匹配上时，回退成"在整块 head 文本里抓第一个像日期的"。
# 而 HEAD_XP 在某些模板上会框进正文 → **把正文里的日期当成了发布日**。
# 实测南开：页面真实发布日 2026-04-13（URL 和页面都写着），我们存成 2026-05-14
# （正文里「材料审核结果发布时间：2026年5月14日前」）。
# 后果不止是显示错：`deadline` 要求 ≥ 发布日，发布日被推后 31 天，
# **正文里 2026-04-28 的报名截止就被噪声闸正确地拒掉了** ——
# 一个错字段把另一个正确字段拖下水，报出来却像"截止日抽不到"。
PUB_URL_TRUTH = """<html><head><title>关于延长报名时间的通知</title></head><body>
<div class="infobox">发布者：研究生院 浏览次数：10</div>
<div class="wp_articlecontent">各位考生：网上报名截止时间：2026年4月28日17:00。</div>
</body></html>"""
_d = detail.parse_detail(PUB_URL_TRUTH.encode("utf-8"),
                        "https://graduate.nankai.edu.cn/2026/0413/c83a592868/page.htm")
check("head 里没有发布日期标签时不乱抓日期（交给 URL）", _d["publish_date"], "2026-04-13")
check_true("**「截止时间」不能被当成发布日**",
           _d["publish_date"] != "2026-04-28", str(_d["publish_date"]))

# 坑二（修坑一时新踩的）：正则一度放宽成裸 `时间[：:]`，于是匹配上「截止时间：」。
# 放宽一个正则时要顺手问：它还能匹配到哪些我不想要的东西。
PUB_LABEL_WINS = """<html><head><title>某通知</title></head><body>
<div class="arti_info">发布时间：2026-10-10 浏览次数：3</div>
<div class="wp_articlecontent">正文里有别的日期 2027年1月1日。</div>
</body></html>"""
_d2 = detail.parse_detail(PUB_LABEL_WINS.encode("utf-8"),
                         "https://mse.zju.edu.cn/2026/1011/c50981a3213081/page.htm")
check("页面标签优先于 URL（浙大 MSE 就是两者不一致）", _d2["publish_date"], "2026-10-10")

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

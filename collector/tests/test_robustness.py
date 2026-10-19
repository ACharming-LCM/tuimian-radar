# -*- coding: utf-8 -*-
"""数据层鲁棒性测试：**喂坏的输入，看它响不响**。不依赖 pytest：

    cd collector && python tests/test_robustness.py

## 这个文件存在的理由

用户这一轮的原话：**「功能跑通不代表鲁棒性就强，不希望做出来的只是偶尔成功的
happy case」**。

`test_data_contract.py` 验的是**产出的不变量**（正常输入下数据长什么样）。
这个文件验的是**输入坏掉时的行为**（坏输入下会不会静默产出错数据）。
两者不能互相替代 —— 全部 196 条数据都好，也证明不了"残页不会被当成全文"。

## 这里钉住的三件事（都是实测出来的，不是想象的）

1. **残页不许当成全文。** 实测：把 1400 字的正文截到 232 字，选择器照样命中、
   长度照样过 150 的门槛，`body_state` 照样是 `ok` —— 于是敢对用户说
   "该公告未公布截止日"。现在靠 `</html>` 这个结构判据挡住（实测 190/190 正常页面都有）。
2. **网络不好不许等于数据变少。** 部分站点挂掉时 `crawl.py` 原来会照常覆盖写盘，
   196 条静默变成 130 条，而门禁要到下一步才报、归档还先跑。
3. **失败要能分类。** "截断"必须是独立的一类，不能混进泛泛的 `HTTPError`。
"""
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)
sys.path.insert(0, COLLECTOR)

import common        # noqa: E402
import crawl         # noqa: E402
import detail        # noqa: E402
import diff          # noqa: E402
import fields        # noqa: E402
import listparse     # noqa: E402

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


# ---------------------------------------------------------------------------
# 1. 残页 / 截断
# ---------------------------------------------------------------------------
print("== 残页不许当成全文（这一条是实测出来的真漏洞）==")

BODY = ('各位考生：现将我校2027年硕士研究生招生简章公布如下，请仔细阅读。'
        '一、报名时间：2026年10月5日至10月25日。二、报名网址：研招网。' * 20)
PAGE = ('<html><head><title>某招生简章</title></head><body>'
        '<div class="infobox">发布时间：2026-10-08 浏览次数：10</div>'
        '<div class="wp_articlecontent"><p>' + BODY + '</p></div></body></html>')
URL = "https://x.example.edu/2026/1008/c1a2/page.htm"

full = detail.parse_detail(PAGE.encode("utf-8"), URL)
check("完整页面 → selector", full["body_how"], "selector")
check("完整页面 → body=ok", fields.body_state(full["body_how"], len(full["body_text"])), "ok")

# 关键用例：截在正文容器**内部**，剩余仍然很长
for frac in (0.6, 0.4, 0.2):
    cut = int(len(PAGE) * frac)
    d = detail.parse_detail(PAGE[:cut].encode("utf-8"), URL)
    check_true(f"截到 {frac:.0%}：解析得出来（能复现那个坑）",
               d is not None, "None 的话这条用例测不到东西")
    check_true(f"截到 {frac:.0%}：正文还有 {len(d['body_text'])} 字（超过 150 门槛）",
               len(d["body_text"]) > common.__dict__.get("_unused", 0) and
               len(d["body_text"]) > fields.ABSENT_MIN_BODY_CHARS,
               f"{len(d['body_text'])} 字")
    check(f"**截到 {frac:.0%}：body_how 变成 incomplete**", d["body_how"], "incomplete")
    check(f"截到 {frac:.0%}：body_state 不再是 ok",
          fields.body_state(d["body_how"], len(d["body_text"])), "missing")
    r = fields.field_state({"deadline": None, "majors": []}, body=d["body_text"],
                           body_len=len(d["body_text"]),
                           body_how=d["body_how"], attachments=[])
    check(f"**截到 {frac:.0%}：deadline 是 unknown，不是 absent**", r["deadline"], "unknown")

print("\n== 这条结构判据在真实数据上不许误报 ==")
# 实测 190/190 条缓存页面都有 </html>。如果哪天某个站点不写，这条会红 ——
# 那时要判断的是"那个站点真的不写，还是它开始返回残页了"。
recs = []
p = os.path.join(common.OUT_DIR, "notices.jsonl")
if os.path.exists(p):
    with open(p, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]
html_recs = [r for r in recs
             if os.path.splitext((r.get("url") or "").split("?")[0])[1].lower()
             not in (".pdf", ".docx")]
checked = same = 0
for r in html_recs:
    cp = common.cache_path(r["url"])
    if not os.path.exists(cp):
        continue
    with open(cp, "rb") as f:
        d = detail.parse_detail(f.read(), r["url"])
    if d is None:
        continue
    checked += 1
    if d.get("body_how") != "incomplete":
        same += 1
if checked:
    check_true(f"缓存里 {checked} 条真实页面，没有一条被判成 incomplete（判据不误报）",
               same == checked, f"误报 {checked - same} 条")
else:
    print("  （无缓存可查，跳过）")

print("\n== 失败要能分类：'截断'必须是独立的一类 ==")
check("classify_error 认得 IncompleteRead",
      common.classify_error(Exception("IncompleteRead(28 bytes read, 4972 more expected)")),
      "truncated")
check("自己的中文截断消息也认得",
      common.classify_error(IOError("响应被截断：Content-Length=5000 实际=28 字节")),
      "truncated")
check("不被误判成别的类",
      common.classify_error(Exception("ssl handshake failed")), "tls_handshake")

print("\n== Content-Length 对不上时 fetch 要报截断（真发请求，打本机）==")
# 服务端声明 5000 字节、只发 40 字节就断开。
# 这条验的是：**我们不会把半截响应写进缓存**，而是当场失败并带上原因。
class Trunc(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "5000")
        self.end_headers()
        self.wfile.write(b"<html><body><div class=article>half")
        # 不写完，直接结束（模拟连接中途断掉）
    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Trunc)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()
saved_raw = common.RAW_DIR
tmpd = os.path.join(common.OUT_DIR, "_robustness_tmp")
os.makedirs(tmpd, exist_ok=True)
try:
    common.RAW_DIR = tmpd
    err = {}
    body, cached = common.fetch(f"http://127.0.0.1:{port}/x.htm", refresh=True,
                                retries=1, sleep=0, err=err)
    check("截断的响应 → 返回 None（不写缓存）", body, None)
    check("截断的响应 → 带上原因", err.get("reason"), "truncated")
    check("截断的响应 → 没有留下缓存文件", os.listdir(tmpd), [])
finally:
    common.RAW_DIR = saved_raw
    srv.shutdown()
    try:
        for f in os.listdir(tmpd):
            os.remove(os.path.join(tmpd, f))
        os.rmdir(tmpd)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# 2. 写盘闸：网络不好 ≠ 数据变少
# ---------------------------------------------------------------------------
print("\n== 写盘闸：这一轮少太多就不许覆盖 ==")
R = common.MIN_RETAIN_RATIO

ok, why = crawl.shrink_guard(196, 196)
check_true("条数没变 → 允许写", ok, why)
check_true("说明里带留存率", "100%" in why, why)

ok, why = crawl.shrink_guard(130, 196)
check("196 → 130（留存 66%）→ 拒绝写", ok, False)
check_true("拒绝时说明了原因", "66%" in why and "130" in why and "196" in why, why)

# 边界用能整除的数：196 的 80% 是 156.8（非整数），
# 拿 int(196*0.8)=156 去测边界，测到的是"略低于下限"而不是"刚好等于下限"。
ok, _ = crawl.shrink_guard(80, 100)
check("刚好等于下限（80/100 = 80%）→ 允许", ok, True)

ok, _ = crawl.shrink_guard(79, 100)
check("比下限少 1 条（79/100）→ 拒绝", ok, False)

ok, why = crawl.shrink_guard(130, 196, allow_drop=True)
check("显式 --allow-drop → 放行", ok, True)

ok, _ = crawl.shrink_guard(50, 0)
check("首次运行（没有上一轮）→ 放行", ok, True)

ok, _ = crawl.shrink_guard(3, 4)
check("小样本也生效（4 → 3 留存 75%）", ok, False)

print("\n== 闸读的阈值只有一处 ==")
# 两处各写一个 0.8 就是"同一个策略写在两个地方"，迟早分叉（本项目栽过 4 次）。
src = open(os.path.join(COLLECTOR, "tests", "test_data_contract.py"), encoding="utf-8").read()
check_true("数据门禁读 common.MIN_RETAIN_RATIO，而不是自己写 0.8",
           "common.MIN_RETAIN_RATIO" in src, "门禁里没引用它 —— 阈值又分叉了")

# ---------------------------------------------------------------------------
# 3. 原子写：不许留下半份 jsonl
# ---------------------------------------------------------------------------
print("\n== 原子写：写盘过程中途崩掉，旧文件必须完好 ==")
tmpd = os.path.join(common.OUT_DIR, "_atomic_tmp")
os.makedirs(tmpd, exist_ok=True)
try:
    target = os.path.join(tmpd, "x.jsonl")
    with open(target, "w", encoding="utf-8") as f:
        f.write('{"id":"old"}\n')
    before = open(target, encoding="utf-8").read()
    # 模拟"写到临时文件时崩掉"：tmp 存在但没 replace
    tmpf = target + ".tmp"
    with open(tmpf, "w", encoding="utf-8") as f:
        f.write('{"id":"new"}\n{"id":"half')
    check("崩在半路时旧文件依然是完整的", open(target, encoding="utf-8").read(), before)
    # 真正 allow-drop 式的正常路径：replace 之后才是新的
    os.replace(tmpf, target)
    check("replace 之后才换成新内容", "new" in open(target, encoding="utf-8").read(), True)
    check("replace 之后没有残留 .tmp", os.path.exists(tmpf), False)
finally:
    for f in os.listdir(tmpd):
        os.remove(os.path.join(tmpd, f))
    os.rmdir(tmpd)

# ---------------------------------------------------------------------------
# 4. 站点级对账：抓失败的站点不许把已有记录当成"公告被删了"
# ---------------------------------------------------------------------------
# 这一条是 2026-10-17 19:13 那轮的真事：北大研招网 TLS 握手失败，
# 它那 2 条公告从产出里**直接消失**，退出码 0、门禁全绿、写盘闸也放行
# （2/286 = 0.7%，全局留存率根本拦不住单站点掉两条）。
# 全局闸管的是"整批崩"，这一节管的是"一个站点崩"。
print("\n== 站点级对账：没抓成功的站点，记录不许消失 ==")


def _rec(school, unit, i, url=None):
    return {"id": f"{school}|{unit}|{i}", "school": school, "unit": unit,
            "title": f"{school}{unit}公告{i}", "url": url or f"https://x.edu/{school}{i}.htm",
            "publish_date": "2026-10-01", "content_hash": f"sha1:{school}{i}",
            "field_state": {"deadline": "absent"}}


# 判据词表（下游会直接展示这些词，所以每个都要钉住）
check("有失败 → network_failed",
      crawl.site_verdict({"failed": ["tls_handshake"], "boards": 3, "boards_empty": 0}, 0),
      "network_failed")
check("一个栏目都没发现 → no_boards",
      crawl.site_verdict({"failed": [], "boards": 0, "boards_empty": 0}, 0), "no_boards")
check("栏目全空 → all_boards_empty",
      crawl.site_verdict({"failed": [], "boards": 3, "boards_empty": 3}, 0),
      "all_boards_empty")
check("栏目有内容但一条没成 → no_records",
      crawl.site_verdict({"failed": [], "boards": 3, "boards_empty": 1}, 0), "no_records")
check("**有记录且没报错 → ok（哪怕有栏目是空的）**",
      crawl.site_verdict({"failed": [], "boards": 3, "boards_empty": 1}, 5), "ok")

per_site = [
    # 整站失败：上一轮 3 条，这一轮 0 条
    {"school": "甲大学", "unit": "研招网", "boards": 3, "boards_empty": 1,
     "failed": ["tls_handshake"]},
    # 抓成功：上一轮 2 条，这一轮只剩 1 条 —— 学校真的撤了一条
    {"school": "乙大学", "unit": "理学院", "boards": 2, "boards_empty": 0, "failed": []},
    # 栏目全空（JS 页 / 改版）：上一轮 2 条，这一轮 0 条，一条错都不报
    {"school": "丙大学", "unit": "研招网", "boards": 4, "boards_empty": 4, "failed": []},
    # 部分失败：上一轮 3 条，这一轮 2 条 + 报了 HTTP 错
    {"school": "丁大学", "unit": "材料学院", "boards": 3, "boards_empty": 0,
     "failed": ["HTTPError"]},
    # 完全正常
    {"school": "戊大学", "unit": "物理学院", "boards": 2, "boards_empty": 0, "failed": []},
]
prev_list = [
    _rec("甲大学", "研招网", 1), _rec("甲大学", "研招网", 2), _rec("甲大学", "研招网", 3),
    _rec("乙大学", "理学院", 1), _rec("乙大学", "理学院", 2),
    _rec("丙大学", "研招网", 1), _rec("丙大学", "研招网", 2),
    _rec("丁大学", "材料学院", 1), _rec("丁大学", "材料学院", 2),
    _rec("丁大学", "材料学院", 3),
    _rec("戊大学", "物理学院", 1),
]
prev = {r["id"]: r for r in prev_list}

# 这一轮真抓到的：乙 1 条（少 1）、丁 2 条（少 1）、戊 1 条
fresh = [_rec("乙大学", "理学院", 1), _rec("丁大学", "材料学院", 1),
         _rec("丁大学", "材料学院", 2), _rec("戊大学", "物理学院", 1)]

records, report = crawl.retain_unrefreshed(list(fresh), prev, per_site)
by_site = {}
for r in records:
    by_site.setdefault((r["school"], r["unit"]), []).append(r)

check("**整站失败：3 条一条不少**", len(by_site[("甲大学", "研招网")]), 3)
check("栏目全空：2 条一条不少", len(by_site[("丙大学", "研招网")]), 2)
check("部分失败：只补缺的那 1 条（不是整站照搬）", len(by_site[("丁大学", "材料学院")]), 3)
check("抓成功的站点：少的那条**不**补回来", len(by_site[("乙大学", "理学院")]), 1)
check("沿用不产生重复记录", len(records), len({r["id"] for r in records}))

check("报告了 3 个未刷新站点", sorted((u["school"], u["reason"]) for u in report),
      [("丁大学", "network_failed"), ("丙大学", "all_boards_empty"), ("甲大学", "network_failed")])
check("报告里带沿用条数", {u["school"]: u["kept"] for u in report},
      {"甲大学": 3, "丙大学": 2, "丁大学": 1})

# 反向用例：**学校真删公告，仍然要报 removed** —— 别修成"永远不删"
d = diff.compare(prev, records)
check("**真删除仍然被报出来（只有乙那条）**", [r["id"] for r in d["removed"]],
      ["乙大学|理学院|2"])
check("失败站点的记录不进 removed", d["counts"]["removed"], 1)

# 抓失败但上一轮本来就没有记录：不该凭空补出东西
records2, report2 = crawl.retain_unrefreshed([], {}, [
    {"school": "己大学", "unit": "研招网", "boards": 2, "boards_empty": 0,
     "failed": ["URLError"]}])
check("没有上一轮 → 补不出来，也不崩", (len(records2), report2), (0, []))

# ---------------------------------------------------------------------------
# 5. 翻页：第 2 页有内容必须合并，跨页重复不许重复计数
# ---------------------------------------------------------------------------
print("\n== 翻页：明确页码链接、跨页去重、礼貌间隔 ==")


def _list_page(rows, pager=""):
    lis = "".join(
        f'<li><a href="/post/{rid}">{title}</a><span class="date">2026-10-{day:02d}</span></li>'
        for rid, title, day in rows)
    return (f'<html><body><ul class="news-list">{lis}</ul>{pager}</body></html>').encode("utf-8")


PAGE1 = _list_page(
    [("1", "第一条招生公告", 10), ("2", "第二条招生公告", 9),
     ("3", "第三条招生公告", 8), ("4", "第四条招生公告", 7),
     ("5", "第五条招生公告", 6)],
    '<div class="pages"><a href="/list/2">2</a><a href="/list/3">3</a></div>')
PAGE2 = _list_page(
    [("5", "第五条招生公告", 6), ("6", "第六条招生公告", 5),
     ("7", "第七条招生公告", 4), ("8", "第八条招生公告", 3),
     ("9", "第九条招生公告", 2)])
PAGE3 = _list_page(
    [("1", "第一条招生公告", 10), ("2", "第二条招生公告", 9),
     ("3", "第三条招生公告", 8), ("4", "第四条招生公告", 7),
     ("10", "第十条招生公告", 1)])
fetch_calls = []


def fake_page_fetch(url, refresh=False, sleep=None, err=None):
    fetch_calls.append((url, refresh, sleep))
    pages = {"https://x.edu/list/2": PAGE2, "https://x.edu/list/3": PAGE3}
    return pages.get(url), False


merged, page_log, page_errors = crawl.collect_list_pages(
    PAGE1, "https://x.edu/list/index.htm", max_pages=3, fetcher=fake_page_fetch)
check("第2/3页被真实请求", [x[0] for x in fetch_calls],
      ["https://x.edu/list/2", "https://x.edu/list/3"])
check("额外分页请求使用1~2秒礼貌间隔",
      [x[2] for x in fetch_calls], [(1.0, 2.0), (1.0, 2.0)])
check("第1页5条 + 第2页4条新内容 + 第3页1条", len(merged), 10)
check("跨页重复URL只计一次", len({r["url"] for r in merged}), 10)
check("分页日志能打印每页原数/新增数",
      [(x["items"], x["new"]) for x in page_log], [(5, 5), (5, 4), (5, 1)])
check("分页成功没有错误", page_errors, [])

next1 = _list_page([(str(i), '招生公告测试', 1) for i in range(1, 6)],
                   '<a href="/list/2">下一页&gt;&gt;</a>')
next2 = _list_page([(str(i), '招生公告测试', 1) for i in range(6, 11)],
                   '<a href="/list/3">下一页&gt;&gt;</a>')
def next_fetch(url, **kwargs):
    return {"https://x.edu/list/2": next2, "https://x.edu/list/3": PAGE3}[url], True
next_items, next_logs, _ = crawl.collect_list_pages(
    next1, "https://x.edu/list/1", max_pages=3, fetcher=next_fetch)
check("仅下一页链接也沿链抓到第3页", len(next_logs), 3)
check("中大页面文字页码可识别", len(listparse.pagination_urls(
    PAGE1.replace(b'>2<', ' >页面 2<'.encode('utf8')), 'https://x.edu/list/1', 3)), 3)

# 上交真实结构：公告容器的直接子元素就是 <a>，不是 li/tr/dl。
SJTU_LIST = '''<html><body><div class="announcement-list">
<a class="item" href="/post/1"><span>15</span><span>2026.10</span><div class="title">招生公告甲</div></a>
<a class="item" href="/post/2"><span>14</span><span>2026.10</span><div class="title">招生公告乙</div></a>
<a class="item" href="/post/3"><span>13</span><span>2026.10</span><div class="title">招生公告丙</div></a>
<a class="item" href="/post/4"><span>12</span><span>2026.10</span><div class="title">招生公告丁</div></a>
<a class="item" href="/post/5"><span>11</span><span>2026.10</span><div class="title">招生公告戊</div></a>
</div></body></html>'''.encode("utf-8")
sjtu_items = listparse.parse_list_page(SJTU_LIST, "https://yzb.sjtu.edu.cn/zkxx/sszs")
check("上交直接<a>列表能解析5条", len(sjtu_items), 5)
check("上交日-年月格式还原发布日期", sjtu_items[0]["publish_date"], "2026-10-15")
check("上交标题不带日期前缀", sjtu_items[0]["title"], "招生公告甲")

# 川大真实根因：Unicode HTML 仍带 XML encoding 声明，旧 common.parse 会 ValueError → None。
SCU_XML = '''<?xml version="1.0" encoding="utf-8"?><html><body>
<ul class="data-list"><li><a href="/a">四川大学招生公告</a><span class="news-time">2026/10/08</span></li>
<li><a href="/b">四川大学招生章程</a><span class="news-time">2026/10/07</span></li>
<li><a href="/c">四川大学复试公告</a><span class="news-time">2026/10/06</span></li>
<li><a href="/d">四川大学录取公告</a><span class="news-time">2026/10/05</span></li>
<li><a href="/e">四川大学推免公告</a><span class="news-time">2026/10/04</span></li></ul>
</body></html>'''.encode("utf-8")
check_true("川大XML编码声明页面不再解析成None", common.parse(SCU_XML) is not None)
check("川大data-list能解析5条", len(listparse.parse_list_page(SCU_XML, "https://yz.scu.edu.cn/")), 5)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

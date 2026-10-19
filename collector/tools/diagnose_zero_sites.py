# -*- coding: utf-8 -*-
"""逐站排查"产出 0 条"的原因。只读，不改 data/。

    cd collector && python tools/diagnose_zero_sites.py
    cd collector && python tools/diagnose_zero_sites.py --refresh   # 强制重抓

## 为什么要单独一个工具

`freshness.json` 里这些站点全是 `boards=8 empty=8 failed=[]` ——
**看起来像"抓到了但栏目没内容"，实际原因完全不是一类东西**：

- 有的是栏目发现选错了（选到"培养工作""学位工作""竞赛栏"）
- 有的是列表页真 0 条（JS 渲染、需要翻页、结构不匹配）
- 有的是首页连一个招生栏目都没有（研招网在另一个域名）
- （已修）有的是在"生成候选"阶段就被过滤掉，`rejected` 里连痕迹都没有

**把它们混成一个"0 条"就没法修。** 这个工具给每一站一条具体病因。

判据全部落在可复现的事实上，不猜：
- 首页上"招生类链接"有几个（文字含 招生/推免/硕士/博士/夏令营/报名/调剂/复试）
- 自动发现选中了哪些栏目、丢了哪些（含丢弃原因）
- 每个选中栏目的列表页解析出几条
- 页面像不像 SPA（可见文本很少 + script 很多）
- 有没有登录墙字样
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common      # noqa: E402
import discover    # noqa: E402
import listparse   # noqa: E402

ADMIT = re.compile(r"招生|推免|免试|硕士|博士|夏令营|报名|调剂|复试|专业目录|简章")
LOGIN = re.compile(r"统一身份认证|请登录|登录后|login|cas\.|auth")
TAG_A = re.compile(r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")


def visible_text(raw):
    s = common.decode(raw or b"")
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    return " ".join(TAG.sub(" ", s).split())


def admission_links(raw):
    """首页上文字像招生公告的链接（去重后）。"""
    s = common.decode(raw or b"")
    out, seen = [], set()
    for href, inner in TAG_A.findall(s):
        txt = " ".join(TAG.sub(" ", inner).split())
        if not txt or len(txt) > 40:
            continue
        if not ADMIT.search(txt):
            continue
        u = common.norm_url("", href) or href
        if u in seen:
            continue
        seen.add(u)
        out.append((txt, u))
    return out


def looks_spa(raw):
    s = common.decode(raw or b"")
    scripts = len(re.findall(r"(?is)<script\b", s))
    txt = visible_text(raw)
    return scripts >= 8 and len(txt) < 1200, scripts, len(txt)


def diagnose(site, refresh=False):
    home = site["homepage"]
    d = {"school": site["school"], "unit": site["unit"], "homepage": home}
    raw, cached = common.fetch(home, refresh=refresh)
    if not raw:
        d["verdict"] = "unreachable"
        d["detail"] = "首页取不到（网络/证书层）"
        return d

    spa, nscript, ntext = looks_spa(raw)
    d["spa"] = spa
    d["scripts"] = nscript
    d["text_len"] = ntext
    d["login"] = bool(LOGIN.search(visible_text(raw)[:3000]))

    kept, rejected = discover.discover_boards(home, max_boards=8, with_rejected=True)
    d["kept"] = [(b.get("label") or "")[:26] for b in kept]
    d["rejected"] = [(b.get("label") or "")[:22] + " ← " + (b.get("reason") or "")[:24]
                     for b in rejected]

    # 首页上像招生的链接（这是"我们根本没生成候选"的对照物）
    links = admission_links(raw)
    d["admit_links"] = len(links)
    kept_urls = {b["url"] for b in kept}
    d["admit_links_not_kept"] = [(t[:30], u) for t, u in links if u not in kept_urls][:6]

    # 每个选中栏目的列表条数
    boards = []
    for b in kept:
        braw, _ = common.fetch(b["url"], refresh=refresh)
        items = listparse.parse_list_page(braw, b["url"]) if braw else []
        boards.append({"label": (b.get("label") or "")[:24], "n": len(items),
                       "has_content": bool(visible_text(braw)[:2000]) if braw else False})
    d["boards"] = boards
    d["board_items_total"] = sum(x["n"] for x in boards)

    # ---- 定性 ----
    if not kept:
        d["verdict"] = "no_board"
        d["detail"] = "自动发现一个栏目都没选中"
    elif d["board_items_total"] > 0:
        d["verdict"] = "parsed_but_dropped"
        d["detail"] = f"栏目解析出 {d['board_items_total']} 条，但没进产出（查详情/去重/过滤）"
    elif spa:
        d["verdict"] = "js_rendered"
        d["detail"] = f"静态页几乎没内容（可见文本 {ntext} 字、script {nscript} 个）"
    elif d["admit_links"] > 0:
        d["verdict"] = "wrong_boards"
        d["detail"] = (f"选中的 {len(kept)} 个栏目全 0 条，"
                       f"而首页有 {d['admit_links']} 个招生类链接没被选中")
    elif d["login"]:
        d["verdict"] = "login_wall"
        d["detail"] = "页面有登录/统一身份认证字样"
    else:
        d["verdict"] = "empty_lists"
        d["detail"] = "栏目选对了，但列表页确实解析出 0 条（结构不匹配/需要翻页）"
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--only", default=None)
    ap.add_argument("--json", default=None, help="把结果写到这个文件")
    a = ap.parse_args()

    cfg = json.load(open(os.path.join(os.path.dirname(HERE), "sites.json"), encoding="utf-8"))
    fresh_p = os.path.join(common.OUT_DIR, "freshness.json")
    zero_units = set()
    if os.path.exists(fresh_p):
        fr = json.load(open(fresh_p, encoding="utf-8"))
        zero_units = {(s["school"], s["unit"]) for s in fr["schools"] if not s.get("records")}

    sites = [s for s in cfg["schools"] if (s["school"], s["unit"]) in zero_units]
    if a.only:
        sites = [s for s in sites if a.only in s["unit"] or a.only in s["school"]]

    print(f"零产出站点点位 {len(sites)} 个\n")
    rows = []
    for i, s in enumerate(sites, 1):
        print(f"── [{i}/{len(sites)}] {s['school']} / {s['unit']}")
        d = diagnose(s, refresh=a.refresh)
        rows.append(d)
        print(f"   判定：{d['verdict']} —— {d['detail']}")
        if d.get("kept") is not None:
            print(f"   选中栏目：{d['kept']}  条数 {[b['n'] for b in d['boards']]}")
            if d.get("rejected"):
                print(f"   被丢的：{d['rejected'][:3]}")
            if d.get("admit_links_not_kept"):
                print(f"   首页上未被选中的招生链接 {d['admit_links']} 个，例：")
                for t, u in d["admit_links_not_kept"][:3]:
                    print(f"      · {t}  {u[:66]}")
        print()

    print("=" * 78)
    from collections import Counter
    c = Counter(d["verdict"] for d in rows)
    print("汇总：", dict(c))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print("明细 ->", a.json)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""加学校工具：探测候选域名 -> 生成 sites.json -> 跑栏目发现 -> 出覆盖率报告。

为什么需要它：猜域名猜错过两次（cs.zju.edu.cn 其实是邮件系统，math.zju.edu.cn 根本不存在）。
所以域名一律**探测后**才写进配置，探测结果（不通）本身就是要给用户看的数据 ——
它直接说明"每天更新"能覆盖到哪一片。

用法（在 collector 目录下）：
    python tools/add_sites.py                 # 探测 + 生成 sites.json + 出报告
    python tools/add_sites.py --no-probe      # 只用已探测结果重新生成（走缓存）

归属：A。产出：
    collector/sites.json      种子站点清单（只含探测通过的）
    data/out/coverage.md      覆盖率报告，带"需人工判定"列
    _scratch/probe_sites.log  探测原始结果
"""
import argparse
import concurrent.futures as cf
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common        # noqa: E402
import discover      # noqa: E402

TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.S | re.I)
KILLED = re.compile(r"邮件|邮箱|coremail|登录|管理信息系统|统一身份认证|cas|"
                    r"404|not found|无法找到|错误", re.I)
BOARD_GOOD = re.compile(r"招生|推免|免试|研究生|硕士|博士|夏令营|报考|培养")
_print_lock = threading.Lock()


def _try(url):
    """返回 (raw, reason)。reason 把失败**分类** —— 这是本工具最重要的输出。

    "抓不到"和"域名猜错了"是完全不同的两件事：
    前者是产品覆盖面的硬限制，后者只是我们还没找到正确域名。
    混在一起报一个数字，等于把可修的问题说成不可修。
    """
    p = common.cache_path(url)
    if os.path.exists(p):
        with open(p, "rb") as f:
            return f.read(), None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": common.UA})
        with urllib.request.urlopen(req, timeout=10, context=common._CTX) as r:   # noqa: SLF001
            body = r.read()
        with open(p, "wb") as f:
            f.write(body)
        return body, None
    except urllib.error.HTTPError as e:
        tag = "（WAF/反爬）" if e.code in (403, 412, 429) else ""
        return None, f"HTTP {e.code}{tag}"
    except urllib.error.URLError as e:
        s = str(getattr(e, "reason", e))
        low = s.lower()
        if "getaddrinfo" in low or "name or service not known" in low:
            return None, "DNS 不存在（域名猜错）"
        if "refused" in low:
            return None, "连接被拒绝（可能内网/仅特定网络）"
        if "timed out" in low or "timeout" in low:
            return None, "超时"
        return None, f"网络错误：{s[:36]}"
    except Exception as e:                                    # noqa: BLE001
        return None, type(e).__name__


def probe_one(school, unit, urls, boards=None):
    """返回 dict：ok / url / title / tried；显式栏目原样带到 sites.json。"""
    tried = []
    for u in urls:
        raw, reason = _try(u)
        if not raw:
            tried.append((u, reason or "失败"))
            continue
        m = TITLE_RE.search(raw[:6000])
        title = ""
        if m:
            try:
                title = common.decode(m.group(1)).strip()
            except Exception:                      # noqa: BLE001
                title = ""
        if KILLED.search(title):
            tried.append((u, f"是个登录/邮件系统：{title[:26]}"))
            continue
        tried.append((u, f"OK：{title[:34]}"))
        return {"school": school, "unit": unit, "homepage": u,
                "title": title, "ok": True, "tried": tried,
                "boards": boards or []}
    return {"school": school, "unit": unit, "homepage": None,
            "title": None, "ok": False, "tried": tried,
            "boards": boards or []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-probe", action="store_true", help="跳过网络探测，直接用上次结果")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(HERE, "candidates.json"), encoding="utf-8"))
    cands = cfg["candidates"] + cfg.get("departments", [])

    cache = os.path.join(common.ROOT, "_scratch", "probe_sites.json")
    if args.no_probe and os.path.exists(cache):
        results = json.load(open(cache, encoding="utf-8"))
        print(f"复用上次探测结果：{len(results)} 条")
    else:
        results = []
        print(f"探测 {len(cands)} 个站点（每站最多 {max(len(c['urls']) for c in cands)} 个候选域名）...")
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(probe_one, c["school"], c["unit"], c["urls"], c.get("boards"))
                    for c in cands]
            for i, f in enumerate(cf.as_completed(futs), 1):
                r = f.result()
                results.append(r)
                with _print_lock:
                    mark = "OK  " if r["ok"] else "FAIL"
                    print(f"  [{i:>2}/{len(cands)}] {mark} {r['school']}/{r['unit']}"
                          f"{'  -> ' + r['homepage'] if r['ok'] else ''}")
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        json.dump(results, open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # --no-probe 的旧探测缓存未带 boards；栏目配置始终以候选源为准。
    board_config = {(c['school'], c['unit']): c.get('boards', []) for c in cands}
    for r in results:
        r['boards'] = board_config.get((r['school'], r['unit']), [])
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]

    # ---- 1) 写 sites.json ----
    # school_slug 用「学校+单位」的 hash：中文转拼音不可靠，猜缩写会撞车，
    # 而 slug 会进记录主键（zju-69706-3201144），必须稳定且唯一。
    def slug(r):
        import hashlib
        return "u" + hashlib.sha1((r["school"] + r["unit"]).encode("utf-8")).hexdigest()[:8]

    schools = []
    for r in ok:
        site = {"school": r["school"], "school_slug": slug(r),
                "unit": r["unit"], "homepage": r["homepage"]}
        if r.get("boards"):
            site["boards"] = r["boards"]
        schools.append(site)

    out_cfg = {
        "_comment": "由 tools/add_sites.py 生成。加学校请改 tools/candidates.json 后重跑，不要手改本文件。",
        "_owner": "A",
        "_generated": time.strftime("%Y-%m-%d %H:%M"),
        "_probe_summary": {"candidates": len(results), "reachable": len(ok), "unreachable": len(bad)},
        "_limits_comment": "默认每栏最多4页；50个列表候选覆盖常见4页×14~20条的近期窗口，详情最多30条以控制WAF风险和服务器压力，不做无限历史回抓。",
        "limits": {"boards_per_site": 8, "pages_per_board": 4,
                   "items_per_board": 50, "details_per_board": 30},
        "schools": schools,
    }
    json.dump(out_cfg, open(os.path.join(os.path.dirname(HERE), "sites.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---- 2) 跑栏目发现 ----
    # top-N 用 8 不是 4：过滤器已经把噪声清掉了，放宽 N 能换回召回
    # （实测 16 个正确栏目：top-4 只保住 81%，top-8 保住 100%，而精度几乎不掉）。
    max_boards = out_cfg["limits"]["boards_per_site"]
    print(f"\n发现栏目（{len(ok)} 个可达站点，每站最多 {max_boards} 个）...")
    rows = []
    for r in ok:
        boards = discover.discover_boards(r["homepage"], max_boards)
        hit = [b for b in boards if BOARD_GOOD.search(b["label"])]
        if not boards:
            verdict = "❌ 未发现栏目"
        elif hit:
            verdict = "⚠️ 待人工判定"
        else:
            verdict = "❌ 栏目不相关"
        rows.append({**r, "boards": boards, "verdict": verdict})
        print(f"  {r['school']}/{r['unit']}: {len(boards)} 个栏目 -> {verdict}")

    # ---- 3) 覆盖率报告 ----
    buf = io.StringIO()
    p = lambda *a: print(*a, file=buf)              # noqa: E731
    p(f"# 覆盖率报告  {time.strftime('%Y-%m-%d %H:%M')}\n")
    p(f"候选站点 **{len(results)}** 个 → 公网可达 **{len(ok)}** 个 → "
      f"不可达 **{len(bad)}** 个\n")
    p("> **不可达本身就是结论**：这一批站点比赛期间抓不到，"
      "「每天更新」的真实覆盖面就是这个数字，不是 100%。\n")

    p("## 一、可达站点与自动发现的栏目\n")
    p("**判定方法**：看「发现的栏目」里有没有招生/推免/研究生类栏目。")
    p("`⚠️ 待人工判定` 不等于正确 —— 必须点开 url 看一眼，"
      "浙大研究生招生网就是自动发现到了「在职攻读硕士学位」这种错栏目。\n")
    for r in sorted(rows, key=lambda x: x["verdict"]):
        p(f"### {r['school']} · {r['unit']}  `{r['verdict']}`")
        p(f"首页：{r['homepage']}  ")
        p(f"标题：{r['title'] or '—'}\n")
        if r["boards"]:
            p("| 分数 | 栏目名 | 链接 | 人工判定 |")
            p("|---|---|---|---|")
            for b in r["boards"]:
                good = "✓" if BOARD_GOOD.search(b["label"]) else ""
                p(f"| {b['score']} | {b['label'][:28]} {good} | {b['url']} | |")
        else:
            p("_未发现任何栏目页_")
        p("")

    p("## 二、公网不可达的站点（已按原因分类）\n")
    p("**「DNS 不存在」= 我们域名猜错了，站点本身可能在，只是要人工找对地址。**")
    p("**「连接被拒绝 / 超时 / WAF」= 真的抓不到，属于产品覆盖面的硬限制。**\n")
    p("| 学校 | 单位 | 试过的域名与原因 |")
    p("|---|---|---|")
    for r in bad:
        tried = "<br>".join(f"`{u}`<br>　{s}" for u, s in r["tried"])
        p(f"| {r['school']} | {r['unit']} | {tried} |")

    # 失败原因归类统计
    p("\n### 失败原因分布\n")
    from collections import Counter
    tally = Counter()
    for r in bad:
        for _, s in r["tried"]:
            if "DNS" in s:
                tally["DNS 不存在（域名猜错，可修）"] += 1
            elif "拒绝" in s:
                tally["连接被拒绝（内网/仅特定网络）"] += 1
            elif "HTTP" in s:
                tally["HTTP 错误 / WAF 反爬"] += 1
            elif "超时" in s:
                tally["超时"] += 1
            elif "登录/邮件" in s:
                tally["域名被邮件/登录系统占用"] += 1
            else:
                tally[s[:24]] += 1
    p("| 原因 | 次数 | 可修吗 |")
    p("|---|---|---|")
    for k, v in tally.most_common():
        fixable = "✅ 可修（人工找域名）" if "域名" in k else "❌ 硬限制"
        p(f"| {k} | {v} | {fixable} |")

    report = os.path.join(common.OUT_DIR, "coverage.md")
    open(report, "w", encoding="utf-8").write(buf.getvalue())

    print(f"\n=== 探测：{len(ok)}/{len(results)} 可达")
    print(f"    sites.json  -> {len(schools)} 个站点")
    print(f"    coverage.md -> {report}")


if __name__ == "__main__":
    main()

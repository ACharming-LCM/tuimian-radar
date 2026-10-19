# -*- coding: utf-8 -*-
"""主入口：站点清单 -> 栏目发现 -> 列表 -> 详情 -> 结构化 JSONL + 人类可读摘要。

用法（在 collector 目录下跑）：
    python crawl.py                      # 全量（走缓存）
    python crawl.py --refresh            # 强制重新抓取
    python crawl.py --only 物理学院       # 只跑一个单位
    python crawl.py --details 0          # 只测列表页，不抓详情，快速验证选择器

归属：A（数据管道）。产出：data/out/notices.jsonl（符合 contracts/notice.schema.json）
     和 data/out/digest.md。
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common          # noqa: E402
import detail          # noqa: E402
import diff            # noqa: E402
import discover        # noqa: E402
import fields          # noqa: E402
import listparse       # noqa: E402
import pdf             # noqa: E402
import quality         # noqa: E402
import timeliness      # noqa: E402
import topic           # noqa: E402

COLA_RE = re.compile(r"/c(\d+)a(\d+)/")

# fast 模式要求上一轮产物至少有这么多条（见 `_check_fast_baseline`）。
# 本项目实测产物在 470~1120 条之间；掉到 100 以下只可能是"上一轮没跑成"或"文件被截断"。
FAST_MIN_PREV = 100

# 抽取器版本。**改它的理由必须写在这里**，因为 diff.py 拿它做变更归因：
# 版本一变，正文哈希差异就不再算"学校改了通知"，而算"我们改了抽取代码"（reparsed）。
# 这条规矩是踩出来的：PDF 表格拼接方式一改，67 个 PDF 的正文全变了，
# 不归因的话日报会报"学校改了 67 份通知"。
#   v4：PDF 表格按行拼接（此前是一个单元格一行，正文是竖排的词）
#   v5：新增 field_state 字段可信度三态。**它不改任何已有字段的值**，
#       但确实换了产出形状，所以照实升版本 —— 让下一轮 diff 把它归成 reparsed
#       而不是"学校改了 196 条公告"。
#       离线回填（tools/backfill_field_state.py）也写这个值，两条路必须一致。
#   v6：附件正文合并PDF软换行 + 截止日支持开放时间/第N轮报名/全角连接号/带时分的区间。
#       这两个改动都会改变附件正文和 deadline 的值，所以必须升版本 ——
#       否则下一轮会把自己重抽出来的日期报成"学校改了通知"。
EXTRACT_VERSION = "v6"


def make_id(slug, url):
    m = COLA_RE.search(url)
    if m:
        return f"{slug}-{m.group(1)}-{m.group(2)}"
    # 兜底 id 用**归一化后的 url**：否则同一条公告的 http/https 两个变体会拿到两个 id，
    # 判重就失效了。
    return f"{slug}-{hashlib.sha1(common.canonical_url(url).encode()).hexdigest()[:16]}"


# ---- 失败主机熔断（A16）----
#
# 坑（已踩）：8 页 / 60 详情的一轮里，最慢的不是"页面多"，是**连不上的站**。
# 每个死链接要 3 次重试 × 25 秒超时 + 退避 ≈ 80 秒；几百个这样的链接就是几小时，
# 而它们对"多抓到几条公告"毫无贡献。
# 所以：同一个 host 连续失败 3 次，后面它的 URL 直接跳过并记账（reason=host_skipped）。
# 这不是"假装抓到了"——跳过的条目会在统计里出现，下游能看到。
HOST_FAILS = {}
HOST_FAIL_LIMIT = 3


def _host_of(url):
    try:
        return url.split("://", 1)[1].split("/", 1)[0].lower()
    except IndexError:
        return ""


def fetch_guarded(url, stats=None, **kw):
    """带主机熔断的 fetch。返回 (raw, cached)。"""
    host = _host_of(url or "")
    if HOST_FAILS.get(host, 0) >= HOST_FAIL_LIMIT:
        if stats is not None:
            stats["skipped_dead_host"] = stats.get("skipped_dead_host", 0) + 1
        return None, False
    raw, cached = common.fetch(url, **kw)
    if not raw:
        HOST_FAILS[host] = HOST_FAILS.get(host, 0) + 1
        if stats is not None and HOST_FAILS[host] == HOST_FAIL_LIMIT:
            print(f"      [circuit] {host} 连续失败 {HOST_FAIL_LIMIT} 次，后续跳过")
    else:
        HOST_FAILS[host] = 0
    return raw, cached


def build_file_record(site, board, item, refresh, stats=None):
    """整条公告**本身就是一个文件**（.pdf / .docx），不是网页。

    坑（已踩）：北大研究生招生网把《2027年EMBA招生简章》这类文件**直接当列表条目**排出来，
    URL 就是 `https://admission.pku.edu.cn/docs/20261009163855012333.pdf`。
    旧逻辑拿 HTML 解析器去解析它，全部失败 —— 实测 16 个"详情解析失败"里有 5 个是这一类，
    而它们的正文完全可抽（那一份就有 2177 字 / 4 页）。
    """
    url = item["url"]
    raw, _ = common.fetch_binary(url, refresh=refresh)
    if not raw:
        return None
    text, note, pages = pdf.extract(raw, url)
    name = url.rsplit("/", 1)[-1]
    det = {
        "title": item["title"], "publish_date": item["publish_date"], "source": None,
        "body_text": text, "body_how": "file",
        "attachments": [{"name": name, "url": url, "parsed": bool(text),
                         "kind": "file", "text_len": len(text), "note": note}],
    }
    if stats is not None:
        stats["detail_file"] = stats.get("detail_file", 0) + 1
    return _assemble(site, board, item, det, refresh, attachments=False, stats=stats)


def build_record(site, board, item, refresh, attachments=True, stats=None, err=None,
                 retries=3, timeout=25):
    url = item["url"]
    if os.path.splitext(url.split("?")[0])[1].lower() in (".pdf", ".docx"):
        return build_file_record(site, board, item, refresh, stats)

    raw, _ = fetch_guarded(url, stats=stats, refresh=refresh, err=err,
                           retries=retries, timeout=timeout)
    det = detail.parse_detail(raw, url)
    if not det:
        return None
    return _assemble(site, board, item, det, refresh, attachments, stats)


def _assemble(site, board, item, det, refresh, attachments=True, stats=None):
    """把 detail 的产出拼成一条符合契约的记录，并跑附件补充 + 字段抽取。"""
    title = det["title"] or item["title"]
    # **`publish_date` 必须是字符串，没有日期就给空串，绝不能留 `None`。**
    # 坑（已踩）：放宽栏目发现后，清华物理系的「党团学工」栏目被抓进来，
    # 它那几页**列表上不带日期** → `publish_date` 是 `None` →
    # ① 违反 `notice.schema.json`（那里要求 `string`）
    # ② B 的排序测试直接崩：`'<' not supported between 'str' and 'NoneType'`
    # 一条脏数据同时打红两条门禁，而且报的是"schema 不合"和"排序不对"，
    # 看着像两个问题。**根因只有一个。**
    publish = det["publish_date"] or item["publish_date"] or ""

    rec = {
        "id": make_id(site["school_slug"], item["url"]),
        "school": site["school"],
        "school_slug": site["school_slug"],
        "unit": site["unit"],
        "board": board["label"],
        "title": title,
        "publish_date": publish,
        "url": item["url"],
        "source": det.get("source"),
        "body_text": det["body_text"],
        "body_len": len(det["body_text"]),
        "attachments": det["attachments"],
    }
    # 选择器 vs 兜底 vs 无正文 —— 这个统计是"数据层完整度"的直接指标，
    # 也是发现"某个 CMS 没被覆盖"的唯一途径。不记下来就会一直瞎猜。
    if stats is not None:
        how = det.get("body_how", "none")
        stats["body_" + how] = stats.get("body_" + how, 0) + 1

    pdf_stats = None
    if attachments:
        pdf_stats = pdf.enrich(rec, refresh=refresh)
        if stats is not None:
            for k in ("tried", "ok", "scanned", "failed", "chars"):
                stats["pdf_" + k] = stats.get("pdf_" + k, 0) + pdf_stats[k]
        if pdf_stats["scanned"]:
            # 扫描版附件没有文本层，本机无 OCR 引擎 —— 那部分正文我们是**真的没读到**。
            # 这个数必须传下去，否则 field_state 会把"没读到"说成"确实没有"。
            # 记进 stats 是为了让"我们哪块数据弱"在 runs 报表里就是一个能看见的数，
            # 而不是只藏在这一条记录里。
            stats["pdf_scanned_records"] = stats.get("pdf_scanned_records", 0) + 1

    ex = fields.extract_fields(title, rec["body_text"], publish=publish)
    rec.update({
        "deadline": ex["deadline"],
        "target_year": ex["target_year"],
        "degree_types": ex["degree_types"],
        "apply_mode": ex["apply_mode"],
        "majors": ex["majors"],
        "materials": ex["materials"],
        "contact": ex["contact"],
        "tags": ex["tags"],
        # version 记的是**抽取器版本**，它参与变更归因（见 diff.compare）：
        # 版本变了说明正文差异可能来自我们改了代码，而不一定是学校改了通知。
        # 值定义在文件头的 EXTRACT_VERSION，理由也写在那里。
        "extract": {"method": "rule", "version": EXTRACT_VERSION, "confidence": ex["confidence"]},
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
        "content_hash": "sha1:" + hashlib.sha1(rec["body_text"].encode("utf-8")).hexdigest(),
        # **只用于排序，不要用于隐藏内容**（见 fields.relevance_of 的注释）。
        # 列表默认按日期排，最新的行政/科研信息会占据前几位，
        # 建议前端/B 的接口按 (relevance desc, publish_date desc) 排。
        "relevance": fields.relevance_of(title, rec.get("board"), ex["apply_mode"]),
        # `body_how` 同时落盘 —— 否则服务端只能看到 `absent`/`unknown` 的结果，
        # 无法回答"凭什么这条敢说确实没有"，也没法复核。
        "body_how": det.get("body_how"),
    })

    # ---- 字段可信度三态：**必须在上面那个 update 之后单独算** ----
    #
    # 坑（已踩，而且是这一轮最贵的一个）：我一开始把它写在上面的字典字面量里，
    # 也就是 `"field_state": fields.state_for_record(rec, ...)`。
    # 但字典字面量是**在 update 生效之前**求值的 —— 那一刻 `rec` 里**还没有**
    # `deadline`、`majors`、`body_how`。于是：
    #   · `state_for_record` 读不到 deadline → 每条有截止日的记录都被判成 `absent`
    #   · 读不到 body_how → 退回"有正文就算 selector" → 兜底页也被判成 `ok`
    # 两条后果**同时**打红了数据门禁的两条不变量（一条说"absent 却有值"、
    # 一条说"body 重算对不上"），报出来像两个 bug，**根因只有一个**。
    # 这就是交接文档里"遇到多条门禁同时红，先怀疑共同根因"那条的现实版。
    #
    # 判定用的正文长度取 `det` 的、不取 `rec` 的：rec 的 body_text 在 pdf.enrich
    # 之后混进了附件正文，拿它量"选择器取到的正文有多长"会量错。
    rec["field_state"] = fields.state_for_record(
        rec, body=det["body_text"], body_len=len(det["body_text"]),
        pdf_stats=pdf_stats)
    # 质量分级（junk/thin/ok）：**只标记、不删除**。判据见 quality.py。
    # 必须放在 pdf.enrich 之后 —— 正文长度要把附件正文算进去，
    # 否则「正文只有附件文件名」的名单公示会被误判成 junk。
    rec["record_quality"], rec["quality_hits"] = quality.classify(rec)
    # 标题回退（A6）：标题是站点名但正文完好时，从正文首行把真标题捞回来。
    # 捞回来之后**必须重算 quality/topic** —— 它们的判据都看标题。
    rec["title_source"] = "detail"
    # 标题只是机构名（issues/011）：先从缓存页 <title> / 正文里捞真标题。
    _bt = quality.title_from_body(rec)
    if _bt:
        rec["title"] = _bt
        rec["title_source"] = "fallback_body"
        rec["record_quality"], rec["quality_hits"] = quality.classify(rec)
    # 索引页（A14）不是"标题坏了"，是"这一页根本不是公告"—— 不能靠回退标题救它。
    if rec["record_quality"] == "junk" and not any(
            x.startswith("body_is_index") for x in rec["quality_hits"]):
        fixed, src = quality.title_fallback(rec)
        rec["title_source"] = src
        if fixed:
            rec["title"] = fixed
            rec["record_quality"], rec["quality_hits"] = quality.classify(rec)
    # 主题分类（apply/non_apply/other）：必须带 topic_hits，**每条都能说出凭什么**。
    rec["topic"], rec["topic_hits"] = topic.classify(rec)
    # 时效字段：补不到发布日时 is_fresh 是 null（不知道有多新 ≠ 不新）
    pub, _src = timeliness.recover_publish_date(rec)
    if pub and not rec["publish_date"]:
        rec["publish_date"], rec["publish_date_source"] = pub, _src
    rec.update(timeliness.time_fields(rec))
    return rec


# A27：本轮写到哪个 out（异常退出时删 `<out>.tmp` 用）。只用于清理，不参与逻辑。
_CURRENT_OUT = None


def _discard_tmp(out):
    """删掉 `<out>.tmp`（不存在就静默）。**永远不碰 out 本身。**"""
    if not out:
        return
    tmp = out + ".tmp"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass


def checkpoint(records, out):
    """把当前已抓到的记录**写到 `<out>.tmp`**（A27）。崩了只丢正在跑的那一站。

    ## 为什么不再直接写 `out`（2026-10-19 事故）

    上一版是"每站写一次 out"，本意是"崩了只丢一站"。但它**每次都在截断线上文件** ——
    2026-10-19 06:25 的补跑真的这么干了：2751 条被截成 69 条，然后爬了 3 小时，
    这 3 小时里服务和门禁读到的都是 69 条。收尾那道 `shrink_guard` 完全没机会拦，
    因为**它发生在写盘之后**。

    现在的规矩：运行期间**只碰 `<out>.tmp`**，线上文件一个字节都不动；
    全部跑完、过了 `shrink_guard`，才用 `os.replace()` 原子替换。
    中途异常退出把 tmp 删掉，线上文件仍是上一轮的完整版本。
    """
    if not records:
        return
    tmp = out + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"      !! checkpoint 写盘失败：{e}")


def write_digest(records, stat, path):
    """写 `data/out/digest.md`（人读的抓取摘要）。

    单独成函数是为了让 `tools/rebuild_digest.py` 能复用同一套排版 ——
    全库视图被分批跑覆盖之后，得有办法按原样重建，不能靠"再跑一次全量"。
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 抓取摘要  {datetime.now():%Y-%m-%d %H:%M}\n\n")
        f.write(f"栏目 {stat.get('boards', 0)} 个 / 列表条目 {stat.get('listed', 0)} 条 / "
                f"详情成功 {stat.get('detail_ok', 0)} / 失败 {stat.get('detail_fail', 0)}\n\n")
        cur = None
        for r in records:
            key = f"{r['unit']} · {r['board']}"
            if key != cur:
                cur = key
                f.write(f"\n## {key}\n\n")
            flags = []
            if r["apply_mode"]:
                flags.append(r["apply_mode"])
            if r["deadline"]:
                flags.append(f"截止 {r['deadline']}")
            if r["attachments"]:
                flags.append(f"附件 {len(r['attachments'])}")
            if r["target_year"]:
                flags.append(f"{r['target_year']}年")
            f.write(f"- **{r['title']}**  \n  {r['publish_date']} · {' · '.join(flags) or '—'}  \n"
                    f"  {r['url']}\n")


def _check_fast_baseline(path):
    """fast 模式的前置校验：上一轮产物可用吗？返回 `None` 表示可用，否则返回原因。

    fast 只读每栏第 1 页，其余记录靠"带过上一轮"，所以它**不是幂等的独立模式**，
    而是"在上一轮之上打补丁"。上一轮要是没了/坏了，fast 会写出一份只有几百条的产物，
    而且退出码是 0 —— 这种"看起来正常的半份数据"比直接报错危险得多。

    判据（四条，都便宜）：
      1. 文件存在且不是空文件；
      2. 每一行都能解析成 JSON，且至少带 `id` / `url`；
      3. 条数 ≥ `FAST_MIN_PREV`（默认 100：本项目实测 470~1120 条，
         掉到 100 以下只可能是"上一轮没跑成"或"被截断"）；
      4. 不是"只写了一半"的形态：文件末尾有换行（原子写在替换前会写完整）。
    """
    if not os.path.exists(path):
        return f"上一轮产物不存在：{path}"
    if os.path.getsize(path) == 0:
        return f"上一轮产物是空文件：{path}"
    bad = 0
    n = 0
    last_chunk = b""
    with open(path, "rb") as f:
        for raw in f:
            last_chunk = raw[-1:]
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            n += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not rec.get("id") or not rec.get("url"):
                bad += 1
    if bad:
        return f"上一轮产物有 {bad} 行损坏（JSON 解析失败或缺 id/url）—— 像是被截断过"
    if n < FAST_MIN_PREV:
        return f"上一轮只有 {n} 条（< {FAST_MIN_PREV}）—— 不像是完整基线"
    if last_chunk not in (b"\n", b"\r"):
        return "上一轮产物最后一行没有换行符 —— 像是写到一半被中断"
    return None


def shrink_guard(n_new, n_prev, allow_drop=False):
    """这一轮能不能覆盖上一轮的产出？返回 `(能不能写, 说明)`。

    **抽成函数是为了能被测**。写在 `main()` 里的判据没法测 —— 而这条判据的失效
    代价是**静默丢掉已经抓到的公告**，那是这个项目最不能接受的一类错误。
    （同一课：一个只在部分站点上出错的判据最容易瞒过去。）

    判据只有一条：留存率低于 `common.MIN_RETAIN_RATIO` 就拒绝。
    例外只有两个 —— 没有上一轮（首次运行），或者显式传了 `--allow-drop`。
    """
    if not n_prev or allow_drop:
        return True, ""
    keep = n_new / n_prev
    if keep >= common.MIN_RETAIN_RATIO:
        return True, f"{n_prev} → {n_new} 条（留存 {keep:.0%}）"
    return False, (f"本轮只收到 {n_new} 条，上一轮是 {n_prev} 条"
                   f"（留存 {keep:.0%}，低于 {common.MIN_RETAIN_RATIO:.0%} 的下限）")


def collect_list_pages(first_raw, first_url, max_pages=4, refresh=False, fetcher=None):
    """抓一个栏目的分页并按公告 URL 去重，返回 `(items, pages, errors)`。

    `pages` 每项是 `{url, items, new, cached}`，方便主流程打印
    “第1页 N条 → 翻了 M页 → 共取 K条”。分页 URL 来自页面明确给出的页码、
    下一页链接或站点显式脚本模板；额外页未命中缓存时使用 1~2 秒礼貌间隔。

    `fetcher` 可注入是为了离线 mock：测试必须能证明“第2页有内容”真的被合并，
    不能靠公网恰好可用来验证核心逻辑。
    """
    fetcher = fetcher or common.fetch
    urls = listparse.pagination_urls(first_raw, first_url, max_pages=max_pages)
    items, seen, pages, errors = [], set(), [], []
    queued = {common.canonical_url(u) for u in urls}
    for i, url in enumerate(urls):
        if i == 0:
            raw, cached = first_raw, True
        else:
            err = {}
            raw, cached = fetcher(url, refresh=refresh, sleep=(1.0, 2.0), err=err)
            if not raw:
                errors.append({"url": url, "reason": err.get("reason", "unknown")})
                continue
        got = listparse.parse_list_page(raw, url)
        new_n = 0
        for it in got:
            key = common.canonical_url(it.get("url") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(it)
            new_n += 1
        pages.append({"url": url, "items": len(got), "new": new_n, "cached": cached})
        # 只有“下一页”的CMS可逐页前进；最多仍受配置预算约束。
        for candidate in listparse.pagination_urls(raw, url, max_pages=max_pages)[1:]:
            key = common.canonical_url(candidate)
            if len(urls) >= max_pages:
                break
            if key not in queued:
                queued.add(key)
                urls.append(candidate)
    return items, pages, errors


def site_verdict(srec, n_now):
    """这一站点本轮到底算不算"抓到了"？返回一个**能被下游直接展示**的词。

    这个判据只用来回答一件事：**没抓到的站点，不许把它已有的记录当成"公告被删了"。**

    为什么非要有它（实测）：2026-10-17 19:13 那轮，北大研招网 TLS 握手失败，
    它那 2 条公告从产出里**直接消失**了 —— 退出码 0、门禁全绿、
    `shrink_guard` 也放行（2/286 = 0.7%，远高于 80% 的全局下限）。
    全局留存率挡得住"整批崩"，挡不住"单站点掉了两条"。

    词表（都能直接进 `freshness.json` / `changes.json`，别再用别的写法）：
      · `ok`                本轮抓到了记录，也没报失败
      · `network_failed`    本轮有抓取失败（TLS / HTTP / 超时 / 截断）
      · `no_boards`         一个栏目都没发现（改版 / JS 渲染）
      · `all_boards_empty`  栏目都在，但每个都是空的
      · `no_records`        栏目有内容，却一条详情都没成（陈旧栏目跳过 / 详情全失败）
    """
    if srec.get("failed"):
        return "network_failed"
    if not srec.get("boards"):
        return "no_boards"
    if not n_now:
        if srec.get("boards_empty", 0) >= srec["boards"]:
            return "all_boards_empty"
        return "no_records"
    return "ok"


def retain_unrefreshed(records, prev, per_site):
    """站点本轮没抓成功时，**沿用上一轮它的记录**，而不是让它们消失。

    返回 `(records, report)`；report 每项是
    `{school, unit, reason, kept, previous}`，直接进 `changes.json` 的
    `site_unrefreshed`，让"我们没刷新"这件事在产物里可见，而不是静默少几条。

    ## 判据（三条，缺一条就会走偏）

    1. **站点本轮不成功**（`site_verdict != ok`）才沿用。站点成功就不沿用 ——
       否则学校真的撤了公告，我们也永远报不出来。
    2. **只补本轮缺的那些**（按归一化 url 比对）。同一站点部分栏目成功、
       部分失败时，成功的那些用本轮的，失败的才沿用上一轮的。
    3. **不改变记录内容**。沿用就是原样搬上一轮那条（`crawled_at` 也照旧，
       那是"我们最后一次真去抓它"的时间）。

    ## 残留风险（写在这里，不假装解决）

    如果某校官网**真的清空了招生栏目**，我们会把它当成"没抓成功"而一直沿用旧记录。
    兜底有两个：`freshness.json` 里该站点是 `network_failed`/`all_boards_empty`（不是 `ok`），
    以及 `--allow-drop`（它的语义本来就是"我确认数据真的变少了"）会跳过沿用。
    """
    fresh = {common.canonical_url(r.get("url") or "") for r in records if r.get("url")}
    now_n = {}
    for r in records:
        k = (r.get("school"), r.get("unit"))
        now_n[k] = now_n.get(k, 0) + 1

    old_by_site = {}
    for r in (prev or {}).values():
        old_by_site.setdefault((r.get("school"), r.get("unit")), []).append(r)

    carried, report = [], []
    for s in per_site:
        key = (s["school"], s["unit"])
        verdict = site_verdict(s, now_n.get(key, 0))
        s["status"] = verdict
        olds = old_by_site.get(key) or []
        if verdict == "ok" or not olds:
            continue
        kept = []
        for r in olds:
            u = common.canonical_url(r.get("url") or "")
            if u in fresh:
                continue
            fresh.add(u)
            kept.append(r)
        if not kept:
            continue
        carried.extend(kept)
        s["carried"] = len(kept)
        report.append({"school": s["school"], "unit": s["unit"], "reason": verdict,
                       "kept": len(kept), "previous": len(olds)})
    return records + carried, report


def main():
    # **打印失败不该等于抓取失败。** 计划任务是 GBK 环境，页面里任何一个没清干净的
    # 怪字符（图标字体、残缺编码）都会让中途某个 `print()` 抛 UnicodeEncodeError，
    # 整轮数据就此丢掉。2026-10-18 11:41 的计划任务第一次跑就栽在这上面：
    # 北大某栏目名里带 `\ue603`。
    common.safe_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新抓取")
    ap.add_argument("--only", default=None, help="只跑 unit 名包含该字符串的站点")
    ap.add_argument("--school", default=None, help="只跑 school 名包含该字符串的站点（按学校筛，不是按 unit）")
    ap.add_argument("--details", type=int, default=None, help="覆盖 details_per_board")
    ap.add_argument("--pages", type=int, default=None, help="覆盖 pages_per_board（含第一页）")
    ap.add_argument("--items", type=int, default=None,
                    help="覆盖 items_per_board（每栏候选条数）。2026-10-18 实测川大两栏各有 "
                         "104/98 条，被默认 50 卡住 —— 只抬 --details 没用，候选根本没进来")
    ap.add_argument("--fetch-retries", type=int, default=3,
                    help="每个 URL 最多尝试几次（默认 3）。深抓历史时调到 1 能省掉大量重试")
    ap.add_argument("--fetch-timeout", type=int, default=25,
                    help="单次请求超时秒数（默认 25）")
    ap.add_argument("--deep", action="store_true",
                    help="深挖档：每栏 8 页 / 120 候选 / 60 详情。**不设成默认** —— "
                         "实测每栏多翻 4 页要 +8~10 秒，全站 full 一轮多 ~20 分钟；"
                         "留给周更或补漏时用")
    ap.add_argument("--no-attachments", action="store_true", help="跳过附件下载与解析（快）")
    ap.add_argument("--no-diff", action="store_true", help="跳过与上一轮的变更对比")
    ap.add_argument("--max-stale-days", type=int, default=400,
                    help="栏目最新一条超过这个天数就不回抓详情（0 = 关闭该判定）")
    ap.add_argument("--fast", action="store_true",
                    help="增量快跑：每栏只读第 1 页，只对**上一轮没有的 URL**抓详情，"
                         "其余直接沿用上一轮记录。用于日更（5 分钟内跑完）；"
                         "**发现删除、纠正历史记录要用 --full**")
    ap.add_argument("--full", action="store_true",
                    help="显式全量（默认行为，写在这里是为了让日志里能看见用的是哪一档）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--allow-drop", action="store_true",
                    help="允许本轮的条数比上一轮少 20% 以上仍覆盖写盘。"
                         "**只在确认数据源真的变少时用** —— 默认那个闸是为了防止"
                         "部分站点抓取失败时把已有数据静默覆盖掉")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(os.path.join(here, "sites.json"), encoding="utf-8"))
    lim = cfg["limits"]
    if args.details is not None:
        lim["details_per_board"] = args.details
    if args.pages is not None:
        lim["pages_per_board"] = max(1, args.pages)
    if args.items is not None:
        lim["items_per_board"] = max(1, args.items)
    # `--fast`：每栏只看第 1 页。列表页第 1 页本来就是"最新的十几条"，
    # 日更要的是"有没有新东西"，不是"把历史重读一遍"。
    if args.fast:
        lim["pages_per_board"] = 1
    # `--deep`：深挖档。**只在这个开关下才抬上限** —— 实测（川大两栏）：
    # 4 页 51/50 条 → 8 页 98/104 条（+47/+54），代价是每栏多 8~10 秒。
    # 全站 199 栏都这么翻，full 一轮要多 ~20 分钟；日更用不着，所以不设成默认。
    if args.deep:
        lim["pages_per_board"] = 8
        lim["items_per_board"] = 120
        lim["details_per_board"] = 60

    sites = cfg["schools"]
    if args.only:
        sites = [s for s in sites if args.only in s["unit"]]
    if args.school:
        # `--only` 是按 unit 筛的（"研究生院"这种），测单校时完全不够用 ——
        # 想只跑川大，得按 school 筛。这个参数就是为 A15 的单校代价测量加的。
        sites = [s for s in sites if args.school in s["school"]]

    out = args.out or os.path.join(common.OUT_DIR, "notices.jsonl")
    # A27：记下本轮的目标文件（异常退出时只删它的 .tmp，绝不碰线上文件），
    # 并清掉上一轮残留的 tmp —— 残留物可能是上次被 kill 时写了一半的。
    global _CURRENT_OUT
    _CURRENT_OUT = out
    _discard_tmp(out)

    # ---- fast 模式的前置校验（A9）----
    #
    # fast 的正确性**依赖上一轮产物还在**：它只读每栏第 1 页，其余记录靠"带过上一轮"。
    # 这个依赖原来是隐式的 —— 一旦上一轮文件被删/被截断，fast 会**静默**写出一个
    # 只有 300 条的产物，而退出码是 0，没人知道。所以这里必须显式拦住：
    #   · 文件不存在 → 拒绝
    #   · 一条也读不出来 → 拒绝
    #   · 有条目坏（JSON 解析失败）→ 拒绝（截断的典型形态）
    #   · 条数明显偏少（< FAST_MIN_PREV）→ 拒绝
    # **拒绝时不做任何降级**：不偷偷跑 full，也不降级成"只写第 1 页"。
    # 因为"悄悄换一种语义继续跑"比直接报错更危险 —— 产物看起来正常，其实是半份。
    if args.fast:
        problem = _check_fast_baseline(out)
        if problem:
            print(f"\n❌ --fast 拒绝启动：{problem}")
            print("   fast 只读每栏第 1 页，其余记录必须从上一轮产物带过来。")
            print("   上一轮不可用时跑 fast 会**静默少掉绝大部分记录**，所以这里直接拒绝。")
            print("   处理办法：① 先跑一次 --full 生成基线；或 ② 确认只跑一档时用 --full。")
            sys.exit(3)

    # 缓存验证不应把数百份未变化的PDF重新解析一遍。原始页面仍由URL-SHA1缓存控制；
    # 对上一轮已成功结构化的同URL记录再加一层复用。强制刷新时完全禁用这条路。
    # **但复用不能跨抽取器版本**：v5 的附件正文和 deadline 是旧规则的结论，
    # 直接搬过来会让"改了抽取规则"这件事在缓存模式下永远不生效。
    prev = diff.load_jsonl(out)
    # fast 模式的复用**不设版本闸**：它的语义是"没新 URL 就原样带着上轮记录"，
    # 而不是"用当前代码重算"。要重算就 --full。
    prev_by_key = ({(r.get("school"), r.get("unit"), common.canonical_url(r.get("url") or "")): r
                    for r in prev.values()}
                   if (args.fast or not args.refresh) else {})
    prev_by_url = ({(r.get("school"), r.get("unit"), common.canonical_url(r.get("url") or "")): r
                    for r in prev.values()
                    if (r.get("extract") or {}).get("version") == EXTRACT_VERSION}
                   if not args.refresh else {})

    records, stat = [], {"boards": 0, "boards_empty": 0, "boards_stale": 0,
                         "list_pages": 0, "listed": 0, "detail_ok": 0, "detail_fail": 0,
                         "detail_nobody": 0, "detail_file": 0, "detail_duplicate": 0,
                         "detail_reused": 0, "new_urls": 0}
    # fast 模式的书keeping：哪些 URL 已经进了 records、哪些站点本轮跑过。
    seen_urls, visited_sites = set(), set()
    # 每所学校的新鲜度，最后落成 data/out/freshness.json（给 B 的 /api/freshness 用）。
    # **为什么要落这个文件**：产品最核心的承诺是"每天更新"，而在它之前这句话**不可验证** ——
    # 没有任何产物能回答"数据是什么时候的、哪次没抓到"。
    # 诚实本身就是产品力：说得出"我什么时候更新的、哪次没抓到"的工具，
    # 比只会说"实时更新"的可信得多。
    per_site = []
    for site in sites:
        visited_sites.add((site["school"], site["unit"]))
        print(f"\n=== {site['school']} / {site['unit']}  {site['homepage']}")
        srec = {"school": site["school"], "unit": site["unit"],
                "homepage": site["homepage"], "boards": 0, "boards_empty": 0,
                "failed": []}
        fail_before = {k: v for k, v in stat.items() if k.startswith("fail_")}
        # 同一公告常被挂进多个相邻栏目。最终产物本来就按 canonical URL 去重，
        # 详情阶段也应复用这个身份键，否则会重复解析正文和几十份附件。
        site_detail_seen = set()
        configured = site.get("boards") or []
        boards = ([{"label": b["label"], "own": b["label"], "url": b["url"],
                    "score": b.get("score", 99), "reason": "sites.json 显式栏目"}
                   for b in configured]
                  if configured else
                  discover.discover_boards(site["homepage"], lim["boards_per_site"]))
        if not boards:
            print("    [warn] 没发现栏目页")
        for b in boards:
            stat["boards"] += 1
            srec["boards"] += 1
            print(f"  -> [{b['score']:>2}] {b['label']}  {b['url']}")
            first_err = {}
            raw, cached = fetch_guarded(b["url"], stats=stat, refresh=args.refresh,
                                        err=first_err, retries=args.fetch_retries,
                                        timeout=args.fetch_timeout)
            if not raw:
                reason = first_err.get("reason", "unknown")
                stat["fail_" + reason] = stat.get("fail_" + reason, 0) + 1
                items, pages, page_errors = [], [], []
                print(f"      列表页失败[{reason}]")
            else:
                items, pages, page_errors = collect_list_pages(
                    raw, b["url"], max_pages=lim.get("pages_per_board", 4),
                    refresh=args.refresh)
                for pe in page_errors:
                    reason = pe["reason"]
                    stat["fail_" + reason] = stat.get("fail_" + reason, 0) + 1
                    print(f"      -- 分页失败[{reason}] {pe['url'][:70]}")
                stat["list_pages"] += len(pages)
                first_n = pages[0]["items"] if pages else 0
                cache_note = "（首页缓存）" if cached else ""
                print(f"      第1页 {first_n} 条 / 翻了 {len(pages)} 页 / "
                      f"共取 {len(items)} 条{cache_note}")
            stat["listed"] += len(items)

            # 内容判定 ①：空栏目。栏目名可能叫「招生信息」，页面上却一条都没有
            # （真实案例：浙大物理 /zsxx 是 2016 年留下的空壳栏目）。
            # 词表判定管不了这个 —— 名字对、内容空，只能看内容。
            if not items:
                stat["boards_empty"] += 1
                srec["boards_empty"] += 1
                print("      -> 空栏目，跳过")
                continue

            # 内容判定 ②：陈旧栏目。真实案例：复旦推免章程栏最新一条是 2023-09，
            # 栏目「正确」但三年没更新。抓它的详情纯属浪费 —— 而产品卖的是"今天有什么新的"。
            #
            # **2026-10-18 改成"探针 1 条"**：整栏跳过会让台账里只剩一句"没看"，
            # 分不清"这栏确实没新东西"和"我们根本没访问"。现在改成：
            # 仍然回抓**最新那 1 条**（成本 1 次请求），抓到了就证明"这栏最新就是这条"，
            # 抓不到就留下具体失败原因。成本极低，但把"没看"变成了"看过，确实旧"。
            newest = max((i.get("publish_date") or "" for i in items), default="")
            stale_probe = False
            if newest and args.max_stale_days > 0:
                try:
                    age = (datetime.now().date()
                           - datetime.strptime(newest, "%Y-%m-%d").date()).days
                except ValueError:
                    age = 0
                if age > args.max_stale_days:
                    stat["boards_stale"] += 1
                    stale_probe = True
                    print(f"      -> 最新一条 {newest}（{age} 天前），陈旧栏目：只探针抓最新 1 条")

            done_here = 0
            probe_limit = 1 if stale_probe else lim["details_per_board"]
            for it in items[: lim["items_per_board"]]:
                if done_here >= probe_limit:
                    break
                detail_key = common.canonical_url(it.get("url") or "")
                if not detail_key or detail_key in site_detail_seen:
                    stat["detail_duplicate"] += 1
                    continue
                old = prev_by_url.get((site["school"], site["unit"], detail_key))
                # fast 模式：**上一轮已经有的 URL 一律不抓详情**，直接带过来。
                # 这就是"5 分钟跑完"的全部秘密 —— 日更真正要抓的只有新出现的链接。
                if args.fast and old is None:
                    old = prev_by_key.get((site["school"], site["unit"], detail_key))
                # 栏目首页新鲜不代表后续页都新鲜；不要回抓多年前已失效的详情。
                # **但陈旧栏的探针例外**：探针的全部目的就是"去确认这栏最新那条到底是什么"，
                # 如果这里也按 400 天过滤，探针永远不会发出请求 —— 那就又变回"没看"了。
                if (not old and not stale_probe
                        and args.max_stale_days > 0 and it.get("publish_date")):
                    try:
                        item_age = (datetime.now().date() - datetime.strptime(
                            it["publish_date"], "%Y-%m-%d").date()).days
                    except ValueError:
                        item_age = 0
                    if item_age > args.max_stale_days:
                        continue
                site_detail_seen.add(detail_key)
                done_here += 1
                err = {}
                if old:
                    rec = dict(old)
                    rec["title"] = re.sub(
                        r"[\s　]*\d{4}(?:[-/.年]\s*)\d{1,2}(?:[-/.月]\s*)\d{1,2}日?[\s　]*$", "",
                        rec.get("title") or "").strip()
                    stat["detail_reused"] += 1
                else:
                    # **单条记录失败不许杀整轮。** 详情页里一个坏链接（例如全角冒号的
                    # `http：//…`）就足以让 `parse_detail` 抛异常；以前这等于整轮报废
                    # （20 分钟的数据一条不剩）。现在记一次失败、继续下一条。
                    try:
                        rec = build_record(site, b, it, args.refresh,
                                           attachments=not args.no_attachments, stats=stat,
                                           err=err, retries=args.fetch_retries,
                                           timeout=args.fetch_timeout)
                        stat["new_urls"] += 1
                    except Exception as e:                      # noqa: BLE001
                        stat["detail_failed"] = stat.get("detail_failed", 0) + 1
                        print(f"      !! 详情异常[{type(e).__name__}] {it['url'][:70]} {e}")
                        continue
                if rec:
                    if not rec["body_text"]:
                        stat["detail_nobody"] += 1
                        print(f"      NB  页面无正文 {it['url'][:74]}")
                        continue
                    records.append(rec)
                    seen_urls.add(common.canonical_url(rec["url"]))
                    stat["detail_ok"] += 1
                    extra = ""
                    if rec["attachments"]:
                        got = sum(a.get("text_len", 0) for a in rec["attachments"])
                        extra = f"  附件{len(rec['attachments'])}份/+{got}字"
                    print(f"      OK  {rec['publish_date']}  {rec['title'][:40]}{extra}")
                else:
                    stat["detail_fail"] += 1
                    # **失败必须带原因**，否则只能靠猜，而猜错的代价是去修不存在的问题
                    reason = err.get("reason", "unknown")
                    stat["fail_" + reason] = stat.get("fail_" + reason, 0) + 1
                    print(f"      --  解析失败[{reason}] {it['url'][:70]}")

        # 这所学校本轮的失败原因（用失败计数的增量算，不重扫一遍）
        fail_after = {k: v for k, v in stat.items() if k.startswith("fail_")}
        srec["failed"] = sorted(k[len("fail_"):] for k, v in fail_after.items()
                                if v > fail_before.get(k, 0))
        per_site.append(srec)
        # **每站落一次盘。** 以前只在最后写一次，中途任何异常都让整轮归零；
        # 现在最坏只丢当前这一站。写的是"此刻已抓到的全部记录"，
        # 后面的去重/排序仍会在正常结束时覆盖成最终版。
        checkpoint(records, out)

    # ---- fast：把"这一轮没出现在第 1 页、但上一轮有"的记录带过来 ----
    #
    # 为什么必须有这一步：fast 每栏只读第 1 页，第 2 页之后的老记录不会出现在 items 里。
    # 不补的话，一次 fast 就能让 notices 从 1119 掉到 300 —— 那不是"快"，那是"丢数据"。
    # 代价也要写明：**fast 永远不会发现"学校删了公告"**，也不会重算老记录的字段。
    # 要发现删除/纠正历史，必须跑 --full（写盘闸 + diff 都在这条路上）。
    if args.fast:
        carried = 0
        for (sch, unit, key), old in prev_by_key.items():
            if key in seen_urls:
                continue
            if (sch, unit) not in visited_sites:
                continue
            records.append(dict(old))
            seen_urls.add(key)
            carried += 1
        stat["carried_prev"] = carried
        print(f"\nfast：从上一轮带过 {carried} 条（第 1 页之外的历史记录）")

    # 去重（同一条公告可能挂在多个栏目下），按发布日期倒序
    #
    # 坑（已踩）：**必须按归一化 url 去重，不能按原 url。**
    # 浙大物理学院同一篇文章 http/https 都能打开，原样比 url 会留下两条 id 相同的记录，
    # 前端列表就会出现成对一模一样的卡片。
    # 同一身份有多个变体时：优先 https，其次保留正文更长的。
    uniq = {}
    for r in records:
        k = common.canonical_url(r["url"])
        cur = uniq.get(k)
        if cur is None:
            uniq[k] = r
            continue
        if (r["url"].startswith("https"), len(r["body_text"])) > \
           (cur["url"].startswith("https"), len(cur["body_text"])):
            uniq[k] = r
    records = sorted(uniq.values(), key=lambda r: (r["publish_date"] or ""), reverse=True)

    if not records:
        # 坑（已踩）：--details 0 是给 B/C 用的冒烟测试，它会跑出 0 条记录。
        # 如果照常写文件，就会把上一天抓好的 JSONL 清空，别人第二天一开工数据没了。
        print(f"\n=== 统计: {stat}")
        print("记录 0 条 —— 冒烟模式，不写产出文件（保护已有数据）")
        _discard_tmp(out)
        return

    # `prev` 在抓取前已加载：缓存模式可复用已结构化记录，也保证 diff 读到覆盖前快照。

    # ---- 站点级对账：抓失败的站点**不许**把已有记录当成"公告被删了" ----
    # 放在写盘闸之前：闸看的是"这一轮到底保住了多少条"，沿用之后才算得对。
    # `--allow-drop` 的语义是"我确认数据真的变少了"，那时就不沿用（见函数注释）。
    if args.allow_drop:
        for s in per_site:
            s["status"] = site_verdict(s, sum(
                1 for r in records
                if r["school"] == s["school"] and r.get("unit") == s["unit"]))
        unrefreshed = []
    else:
        records, unrefreshed = retain_unrefreshed(records, prev, per_site)
        if unrefreshed:
            print(f"\n站点未刷新 {len(unrefreshed)} 个（沿用上一轮记录，不计为删除）：")
            for u in unrefreshed:
                print(f"    - {u['school']} / {u['unit']}  [{u['reason']}] "
                      f"沿用 {u['kept']}/{u['previous']} 条")
        records = sorted(records, key=lambda r: (r.get("publish_date") or ""), reverse=True)

    # ---- 闸①：**这一轮比上一轮少太多，就不许覆盖** ----
    #
    # 上面那个 `if not records` 只挡住了"一条都没有"。但更常见、也更隐蔽的是
    # **部分站点挂了**：17 个站点里有 6 个 DNS 失败 / TLS 握手失败 / 改版后选择器
    # 失配，这一轮就少抓几十条，而代码会**照常把 196 条覆盖成 130 条**。
    #
    # 后果不是"少抓了一次"，是**已抓到的数据被删掉了** —— 而且：
    #   · 门禁里的"条数突变"要到**下一步**才报，那时文件已经写完了
    #   · `run_daily.py` 的归档在门禁**之前**，所以 `history/` 里存的也是残缺那份
    #   · 表现上一切正常：退出码 0、有新数据、界面能打开
    #
    # **"网络不好"绝不能等于"数据变少"。** 宁可这一轮什么都不写、报错让人来看，
    # 也不要静默丢数据 —— 丢掉的那些，下一轮不一定还能抓到（公告会翻页）。
    prev_n = len(prev or [])
    can_write, why = shrink_guard(len(records), prev_n, args.allow_drop)
    if not can_write:
        print(f"\n=== 统计: {stat}")
        print(f"❌ {why}")
        print("   **不写盘** —— 这一轮的产出更像'部分站点没抓到'，而不是'公告真的少了'。")
        print("   先查上面哪个站点报了失败；确认是数据源真的变少，"
              "再显式传 --allow-drop 重跑。")
        sys.exit(2)

    # ---- 闸②：**原子写** ----
    #
    # 原来直接 `open(out, "w")`。写到一半崩掉（Ctrl-C、断电、磁盘满）会留下
    # **半份 jsonl**：下一轮 `diff.load_jsonl` 读到的是残缺数据，
    # 而 `test_data_contract.py` 的 `load_jsonl` 会直接抛 JSONDecodeError ——
    # 报出来像"数据处理坏了"，实际是"上次没写完"。
    # 先写临时文件再 `os.replace`（同盘内原子替换），中途崩掉最多留下一个 .tmp。
    #
    # **A27 之后**：tmp 从第一站起就一直在写（checkpoint），这里只是把最终版重写一遍。
    # 线上文件在此刻之前**从来没被打开过**（只读），所以运行期间读它永远是完整的。
    tmp_out = out + ".tmp"
    with open(tmp_out, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp_out, out)

    # ---- 新鲜度落盘：给 B 的 GET /api/freshness 用 ----
    # 形状是**我先定、B 照着写**的（见 journal/2026-10-17-A-BC下一轮.md 第 0 环）。
    # 里面**不写"新鲜不新鲜"的判断** —— 阈值是接口层的事，数据层只报事实，
    # 免得两处各定一套标准、然后互相打脸。
    now = datetime.now()
    fresh_schools = []
    for s in per_site:
        # **必须按 school + unit 归属，不能只按 school。**
        # 坑（已踩，就在写这段的时候）：`sites.json` 里一所学校可能有多个站点
        # （浙大 = 研招网 + 材料学院 + 物理学院），只按 school 统计的话
        # **同一条记录会被算进每一个站点** —— 浙大三个站点各显示 15 条，
        # 加起来 45 条，而真实总数是 72。这种数字一旦进了接口，就会一路骗到 PPT。
        n = sum(1 for r in records
                if r["school"] == s["school"] and r.get("unit") == s["unit"])
        fresh_schools.append({
            "school": s["school"], "unit": s["unit"], "homepage": s["homepage"],
            "last_ok": now.isoformat(timespec="seconds"),
            "records": n,
            "boards": s["boards"], "boards_empty": s["boards_empty"],
            "failed": s["failed"],
            # 这一站本轮算不算"抓到了"（词表见 site_verdict）。
            # 和 `failed` 的区别：`failed` 只说"有没有报错"，`status` 还说得清
            # "栏目全空 / 一个栏目都没发现"——那些页面一条错都不报，但一样没抓到。
            "status": s.get("status", "ok"),
            # 这一站因为没抓成功而**沿用**了多少条上一轮的记录（0 = 没用上）。
            "carried": s.get("carried", 0),
            "freshness_hours": 0.0,
        })
    fpath = os.path.join(common.OUT_DIR, "freshness.json")
    # **分批跑不许覆盖全库视图**（A22 的教训）：`--school X` 只跑一所时，
    # `fresh_schools` 里也只有这一所；直接整文件覆盖，其余 28 所的新鲜度就没了
    # （2026-10-18 真发生：最后一批是清华，freshness 只剩清华两条、records=0）。
    # 所以：本轮跑过的站点用新值，没跑过的沿用上一份文件里的行。
    prev_rows = {}
    if os.path.exists(fpath):
        try:
            with open(fpath, encoding="utf-8") as f:
                old = json.load(f)
            for s in (old.get("schools") or []):
                key = (s.get("school"), s.get("unit"))
                if key[0] and key[1]:
                    prev_rows[key] = s
        except (json.JSONDecodeError, OSError):
            prev_rows = {}
    mine = {(s["school"], s["unit"]) for s in fresh_schools}
    for key, s in prev_rows.items():
        if key not in mine:
            fresh_schools.append(s)
    total_records = sum(int(s.get("records") or 0) for s in fresh_schools)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": now.isoformat(timespec="seconds"),
            "schools": fresh_schools,
            "totals": {"records": total_records,
                       "schools": len({s["school"] for s in fresh_schools}),
                       "sites": len(fresh_schools),
                       "failed_schools": sum(1 for s in fresh_schools if s["failed"])},
        }, f, ensure_ascii=False, indent=1)
    bad = [s["school"] for s in fresh_schools if s["failed"]]
    print(f"\n新鲜度 -> {fpath}"
          + (f"    ⚠️ 本轮有失败的学校：{bad}" if bad else "    本轮全部成功"))

    digest = os.path.join(common.OUT_DIR, "digest.md")
    # **分批跑不许覆盖全库摘要**（A22 同一个坑）：`--school X` 只跑一所时，
    # `records` 里只有那一所，整文件重写会让 digest.md 从"全库最新"变成"某一所最新"。
    full_run = len(sites) == len(cfg["schools"])
    if full_run:
        write_digest(records, stat, digest)
    else:
        print(f"\n（本次是分批跑，{digest} 保持上一份全库摘要不动）")

    print(f"\n=== 统计: {stat}")
    if common.URL_REPAIRED_COUNT or common.URL_SKIP_COUNT:
        print(f"链接修复 {common.URL_REPAIRED_COUNT} 条（全角冒号等）/ "
              f"跳过非法链接 {common.URL_SKIP_COUNT} 条"
              f"{'（示例：' + str(common.URL_SKIP_SAMPLES[:2]) + '）' if common.URL_SKIP_COUNT else ''}")
    print(f"模式 {'fast（第1页 + 只抓新 URL）' if args.fast else 'full'}  "
          f"新增 URL {stat['new_urls']} 条 / 沿用 {stat['detail_reused']} 条 / "
          f"带过历史 {stat.get('carried_prev', 0)} 条")
    print(f"记录 {len(records)} 条 -> {out}")
    print(f"摘要 -> {digest}")

    if not args.no_diff:
        d = diff.run(prev, records, extra={"site_unrefreshed": unrefreshed})
        # **防御**：`removed` 只允许出现"该站点本轮抓成功、且确实没有这条"的记录。
        # 上面的沿用逻辑应该已经保证了这一点；这里再核一遍，是因为这条不变量
        # 一旦破了，产物就会对用户说"公告被删了"，而事实是我们没抓到 —— 静默且致命。
        status_of = {(s["school"], s["unit"]): s.get("status", "ok") for s in per_site}
        stranded = [r for r in d["removed"]
                    if status_of.get((r.get("school"), r.get("unit")), "ok") != "ok"]
        if stranded:
            raise RuntimeError(
                "removed 里出现了未刷新站点的记录（沿用逻辑没兜住）："
                + ", ".join(f"{r.get('school')}/{r.get('unit')} {r.get('title')}"
                            for r in stranded[:5]))
        c = d["counts"]
        print(f"变更 -> data/out/changes.md  "
              f"(新增 {c['added']} / 有变化 {c['changed']} / 本轮未抓到 {c['removed']})")
    else:
        print("已跳过变更对比（--no-diff）")


def guarded_main(fn=None):
    """跑 `main()`，**任何异常退出都只删 `<out>.tmp`**（A27）。

    抽成函数是为了能被测（`tests/test_atomic_write.py`）：
    写在 `__main__` 里的清理逻辑没法在测试里触发，而这条逻辑失效的代价是
    "运行期间线上库被截断" —— 2026-10-19 已经真发生过一次。
    """
    try:
        (fn or main)()
    except BaseException:
        _discard_tmp(_CURRENT_OUT)
        raise


if __name__ == "__main__":
    # `kill -9` 跑不到这里，但那也没关系 —— 它同样没碰过线上文件，
    # 最多留一个 tmp，下次开跑前会被清掉。
    guarded_main()

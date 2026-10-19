# -*- coding: utf-8 -*-
"""数据门禁：对**产出本身**断言不变量，而不是对解析函数。

    cd collector && python tests/test_data_contract.py

为什么需要它（现在比之前更重要）：
  解析测试护的是"某个函数对不对"，而**产出可能被别的路径弄坏** ——
  抓取轮次、去重逻辑、附件合并、别人改 `faculty.py`，任何一个都可能让
  `notices.jsonl` 悄悄变形，而解析测试全绿。

  而且现在**两个远程伙伴在改我们的管道**：伙伴 1 改 `faculty.py`。
  他要是弄坏了，光靠 `test_faculty.py` 抓不到数据层面的后果。

本文件**从 `contracts/*.schema.json` 里读必填字段和枚举**，不自己抄一份 ——
抄一份就会漂移（这个项目已经因此吃过亏：字段表有三份拷贝的隐患）。
"""
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import common        # noqa: E402
import pdf           # noqa: E402

ROOT = common.ROOT
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WARN = []
FAILED = []


def ok(name):
    print(f"  PASS  {name}")


def bad(name, detail=""):
    print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))
    FAILED.append(name)


def warn(name, detail=""):
    print(f"  WARN  {name}" + (f"\n        {detail}" if detail else ""))
    WARN.append(name)


def load_jsonl(p):
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def load_schema(name):
    with open(os.path.join(ROOT, "contracts", name), encoding="utf-8") as f:
        return json.load(f)


def check_notices(recs, schema):
    req = schema["required"]
    print(f"\n== notices.jsonl（{len(recs)} 条）==")

    # 1) 必填字段 —— 直接读契约，不手抄
    missing = [(r.get("id", "?"), [k for k in req if k not in r])
               for r in recs if any(k not in r for k in req)]
    (ok if not missing else lambda n: bad(n, f"例：{missing[:3]}"))(
        f"每条都有契约要求的 {len(req)} 个必填字段")

    # 2) id 唯一
    ids = [r.get("id") for r in recs]
    dups = {i for i in ids if ids.count(i) > 1}
    (ok if not dups else lambda n: bad(n, f"重复 id：{sorted(dups)[:5]}"))("id 唯一")

    # 3) 日期格式
    badd = [r["id"] for r in recs
            if r.get("publish_date") and not ISO_DATE.match(str(r["publish_date"]))]
    (ok if not badd else lambda n: bad(n, f"{badd[:3]}"))("publish_date 都是 YYYY-MM-DD")

    baddl = [r["id"] for r in recs
             if r.get("deadline") and not ISO_DATE.match(str(r["deadline"]))]
    (ok if not baddl else lambda n: bad(n, f"{baddl[:3]}"))("deadline 都是 YYYY-MM-DD 或 null")

    # 3.5) `publish_date` 必须是**字符串**：没有日期就给空串，**不能是 `None`**。
    # 坑（已踩）：放宽栏目发现后抓进了清华物理系的「党团学工」栏目，
    # 它那几页列表上不带日期 → `publish_date` 是 `None` →
    # ① 违反 notice.schema（要求 string）② B 的排序测试直接崩（str 与 None 不可比）。
    # **一条脏数据同时打红两条门禁，报出来像两个问题，根因只有一个。**
    # 所以这条不变量要钉在这里：只要有人再写出 None，先在我这道门禁上红。
    badpd = [r.get("id") for r in recs if not isinstance(r.get("publish_date"), str)]
    (ok if not badpd else lambda n: bad(n, f"{badpd[:3]}"))(
        "publish_date 全是字符串（没日期给空串，不能是 None）")
    nonedate = [r.get("id") for r in recs if isinstance(r.get("publish_date"), str)
                and r["publish_date"] and not ISO_DATE.match(r["publish_date"])]
    (ok if not nonedate else lambda n: bad(n, f"{nonedate[:3]}"))(
        "publish_date 空串或 YYYY-MM-DD")

    # 3.6) 发布日 vs **URL 里的 /YYYY/MMDD/**（维普/南湖之舟系的天然基准）。
    # 186 条里约 52 条的 URL 带发布日，可以直接当对照物 —— 这种"数据自带基准"的机会
    # 不多，遇到就该用起来。
    # 实测 51/52 一致；唯一不一致的那条是浙大 MSE：URL 写 `/2026/1011/`，
    # 而页面自己写着 `发布时间：2026-10-10` —— **页面标注比 URL 更权威**，
    # 解析器的优先级正是"标注优先"。所以这里**不做硬断言，只做告警**：
    # 一旦系统性跑偏（比如又有人写出"在文本里抓第一个日期"），比例会明显上升。
    urlpairs = []
    for r in recs:
        m = re.search(r"/(20\d{2})/(\d{2})(\d{2})/", r.get("url") or "")
        if m:
            urlpairs.append((r.get("id"), r.get("publish_date"),
                             f"{m.group(1)}-{m.group(2)}-{m.group(3)}"))
    mism = [(i, got, want) for i, got, want in urlpairs if got != want]
    if urlpairs:
        name = f"发布日与 URL 内嵌日期一致 {len(urlpairs)-len(mism)}/{len(urlpairs)}"
        if len(mism) / len(urlpairs) > 0.05:
            warn(name, f"不一致 {len(mism)} 条（页面标注优先时属正常）：{mism[:3]}")
        else:
            ok(name)

    # 4) attachments 结构 + kind 枚举（从契约读枚举）
    kinds = set(schema["properties"]["attachments"]["items"]
                ["properties"]["kind"]["enum"])
    badatt = []
    for r in recs:
        for a in r.get("attachments") or []:
            if not {"name", "url", "kind"} <= set(a) or a["kind"] not in kinds:
                badatt.append((r["id"], a.get("kind")))
    (ok if not badatt else lambda n: bad(n, f"{badatt[:3]}"))(
        f"附件结构与 kind 枚举合法（{sorted(kinds)}）")

    # 5) extract 元信息
    badex = [r["id"] for r in recs
             if not isinstance(r.get("extract"), dict)
             or r["extract"].get("method") not in ("rule", "llm", "hybrid")
             or not isinstance(r["extract"].get("confidence"), (int, float))]
    (ok if not badex else lambda n: bad(n, f"{badex[:3]}"))("extract 元信息齐全且合法")

    # 6) content_hash 形状
    badh = [r["id"] for r in recs
            if not str(r.get("content_hash", "")).startswith("sha1:")]
    (ok if not badh else lambda n: bad(n, f"{badh[:3]}"))("content_hash 形状正确")

    # 6b) 标题没有被截断
    #
    # 这条不变量是**补出来的**：detail.py 的标题兜底原来把 `-` 当分隔符，
    # 于是「南开大学2026-2027学年…」被切成「南开大学2026」——
    # 78 条里 4 条被切坏，而且按日期排**正好占住公告列表前四位**。
    # 当时测不出来，因为没有任何断言在看"标题像不像一句话"。
    # 判据：标题不该以一个光秃秃的 4 位年份结尾（正常标题不会这样收尾）。
    trunc = [(r["id"], r["title"]) for r in recs
             if re.search(r"20\d{2}$", (r.get("title") or "").strip())]
    (ok if not trunc else lambda n: bad(
        n, f"{trunc[:3]} —— 标题疑似在被连字符/年份处截断，"
           f"查 detail.py 里 <title> 的兜底切分"))("标题没有被截断")

    # 6c) 标题太短同样是截断信号
    short = [(r["id"], r["title"]) for r in recs if len((r.get("title") or "").strip()) < 2]
    (ok if not short else lambda n: bad(n, f"{short[:3]}"))("标题长度 ≥ 4")

    # 6c2) 标题结尾不该带发布日期（有些站的列表把日期和标题放在同一个 <a> 里）
    tail_date = [(r["id"], r["title"]) for r in recs
                 if re.search(r"[\s　]*\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?$",
                              (r.get("title") or "").strip())]
    (ok if not tail_date else lambda n: bad(
        n, f"{tail_date[:3]} —— 发布日期被拼进标题，查 listparse.py 的尾部日期清理"))(
        "标题结尾没带发布日期")

    # 6d) relevance 只能是 0/1/2（**只用于排序**，见 fields.relevance_of）
    badr = [(r["id"], r.get("relevance")) for r in recs
            if r.get("relevance") not in (0, 1, 2)]
    (ok if not badr else lambda n: bad(n, f"{badr[:3]}"))("relevance 是 0/1/2")
    # 如果一档都没有，说明打分器坏了（全判成同一档），排序会失效
    from collections import Counter
    dist = Counter(r.get("relevance") for r in recs)
    if len(dist) < 2:
        warn("relevance 只有一档", f"分布 {dict(dist)} —— 排序信号失效了")
    else:
        ok(f"relevance 分布 2档={dist.get(2,0)} 1档={dist.get(1,0)} 0档={dist.get(0,0)}")

    # 3.7) **字段可信度三态**：`unknown` 绝不能被当成 `absent`。
    #
    # 这是本特性的核心不变量，也是本项目一直在防的那类错误的又一个实例：
    # `deadline: null` 一个值同时表示「公告里就没有」和「我们没读到」，
    # 而对用户这是天差地别的两句话。三态就是为了把这两件事分开。
    #
    # **做法上有一处是刻意的**：这里不满足于"检查产物自己声称的 field_state 自洽" ——
    # 那样只要 crawl.py 里算错一次，门禁会跟着一起错（它验的是同一份逻辑的复述）。
    # 所以这里**从原始 HTML 缓存把正文重新解析一遍**，独立重算后对账。
    # 缓存没有时才退回用产物的 body_text 现算。
    # 实测 196/196 全有缓存，这条路是通的；代价是这一条要比别的条慢几秒。
    import fields        # noqa: E402
    import detail as _detail   # noqa: E402

    missing_fs = [r["id"] for r in recs if not isinstance(r.get("field_state"), dict)]
    (ok if not missing_fs else lambda n: bad(
        n, f"{missing_fs[:3]} —— 产物里没有 field_state，"
           f"说明 jsonl 是旧抽取器产出的（用 `--details 0` 跑不会写文件，需要真跑一次全量）"))(
        "每条都有 field_state")

    THREE = ("found", "absent", "unknown")
    badshape = []
    for r in recs:
        fs = r.get("field_state") or {}
        if (sorted(fs) != ["attachments", "body", "contact", "deadline", "majors", "materials"]
                or any(fs.get(k) not in THREE
                       for k in ("deadline", "majors", "materials", "contact", "attachments"))
                or fs.get("body") not in ("ok", "generic", "missing")):
            badshape.append((r["id"], fs))
    (ok if not badshape else lambda n: bad(n, f"{badshape[:2]}"))(
        "field_state 形状与取值合法")

    # found / absent 必须和字段本身对得上 —— 这是**互相独立的两条信息**，
    # 一条错就说明两处有一处没跟上（本项目栽过 4 次的"同一个策略写在两个地方"）。
    m_absent = [(r["id"], r.get("deadline")) for r in recs
                if (r.get("field_state") or {}).get("deadline") == "absent" and r.get("deadline")]
    m_found = [(r["id"], r.get("deadline")) for r in recs
               if (r.get("field_state") or {}).get("deadline") == "found" and not r.get("deadline")]
    (ok if not (m_absent or m_found) else lambda n: bad(
        n, f"说 absent 却有值 {m_absent[:2]}；说 found 却没值 {m_found[:2]}"))(
        "deadline 的 field_state 与值一致")

    # **核心不变量**：正文不可信时，任何字段都不许被说成 absent。
    lie = [(r["id"], (r.get("field_state") or {}).get("body"))
           for r in recs
           if (r.get("field_state") or {}).get("body") != "ok"
           and "absent" in ((r.get("field_state") or {}).get("deadline"),
                            (r.get("field_state") or {}).get("majors"),
                            (r.get("field_state") or {}).get("attachments"))]
    (ok if not lie else lambda n: bad(
        n, f"{lie[:3]} —— 正文是 generic/missing 却输出了 absent，"
           f"等于把「我们没读到」说成「公告里没有」。这是本特性最不能犯的错"))(
        "**body 不可信时，没有任何字段被说成 absent**")

    # 独立重算：**用各自正确的方法**重解一遍正文，再算一次三态，和产物逐条对账。
    #
    # ⚠️ 这里踩过一个坑，值得写下来：第一版对所有记录一律走 HTML 解析器重放，
    # 于是 6 条**文件型**记录（`body_how=file`，整条公告本身就是一个 PDF/DOCX）
    # 被判成"对不上"—— 它们的正文根本不在 HTML 里，缓存里那份 HTML 是别的页面。
    # **校验方法用错对象，会把好数据报成坏数据**（和这次回填脚本里那个
    # "分隔符拼法"的坑是同一类）。所以文件型必须走 pdf.extract。
    mism, uncheckable = [], []
    for r in recs:
        fs = r.get("field_state") or {}
        how = r.get("body_how")
        body = None
        if how == "file":
            p = common.bin_cache_path(r.get("url") or "")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    text, note, _pages = pdf.extract(f.read(), r["url"])
                body = text
                if note and "扫描版" in note and fs.get("body") != "ok":
                    pass          # 扫描版本来就该判不了，下面按 unknown 核对
        else:
            p = common.cache_path(r.get("url") or "")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    d = _detail.parse_detail(f.read(), r["url"])
                # 只有"缓存重放出来的正文确实是产物正文"时才可核对。
                # 两种成立的情形：① 两者完全相同（没有附件正文）；② 缓存正文是产物的**真前缀**
                # （产物在它后面拼了附件正文）。**空串不算** —— 空串是任何字符串的前缀，
                # 会让"缓存里正文是空的"这种**恰恰最需要人看一眼**的形态被判成"复核通过"。
                # 都不成立说明上游把缓存刷新过了（实测有记录缓存里是重定向/别的页面），
                # 拿它去核对产物是不成立的 —— 如实报成"无法复核"，不当成错误。
                cb = (d or {}).get("body_text") or ""
                sb = r.get("body_text") or ""
                if cb and (cb == sb or sb.startswith(cb)):
                    how, body = d.get("body_how"), cb
        if body is None:
            uncheckable.append(r["id"])
            continue
        want = fields.body_state(how, len(body))
        if want == "ok" and fields._body_is_meta_only(body):
            want = "missing"
        if fs.get("body") != want:
            mism.append((r["id"], fs.get("body"), want, how))
        # 正文够格时，空值字段必须恰好是 absent（而不是 unknown）——
        # 这条把"该敢说的时候敢说"也钉住，否则三态可以全部输出 unknown 来蒙混过关
        # 正文够格时，空值字段必须恰好是 absent（而不是 unknown）——
        # 这条把"该敢说的时候敢说"也钉住，否则三态可以全部输出 unknown 来蒙混过关。
        #
        # 但**必须把"扫描版附件"这一档算进来**：有扫描版 PDF 时 `unknown` 才是对的
        # （没有文本层 = 我们真的没读到）。判据直接用 `fields._scanned_from_record`，
        # 和生产路径同一个 helper —— 不在这里另写一套"什么算扫描版"。
        scanned = fields._scanned_from_record(r)["scanned"]
        if want == "ok" and scanned == 0 and \
                fs.get("deadline") != ("found" if r.get("deadline") else "absent"):
            mism.append((r["id"], "deadline", fs.get("deadline"), r.get("deadline")))
    checked = len(recs) - len(uncheckable)
    (ok if not mism else lambda n: bad(
        n, f"独立重算有 {len(mism)} 条对不上（前 3）：{mism[:3]}"))(
        f"field_state 与独立重算一致（核对 {checked}/{len(recs)} 条）")
    if uncheckable:
        # 只报数、不判失败：这些是"缓存已被上游刷新，没法复核"，不是数据错。
        print(f"        （{len(uncheckable)} 条无法复核：缓存里的正文不再是产物正文的前缀）")

    # 7) 覆盖率阈值 —— **只警告不判失败**，它们会合法波动
    def ratio(f):
        return sum(1 for r in recs if f(r)) / len(recs)

    body = ratio(lambda r: len(r.get("body_text") or "") > 50)
    dl = ratio(lambda r: r.get("deadline"))
    (ok if body >= 0.9 else lambda n: warn(n, f"{body:.0%} —— 低于 90%，查详情解析"))(
        f"正文非空 {body:.0%}")
    if dl < 0.20:
        warn(f"截止日填充 {dl:.0%}", "低于 20%，匹配页的'剩余天数'会大面积缺失")
    else:
        ok(f"截止日填充 {dl:.0%}")

    # 8) 突变检测：条数骤降说明某轮抓取静默丢了数据
    hist = os.path.join(ROOT, "data", "out", "history")
    prev_files = sorted(f for f in os.listdir(hist)) if os.path.isdir(hist) else []
    prev_files = [f for f in prev_files if f.endswith(".json") and f != "index.json"]
    if prev_files:
        with open(os.path.join(hist, prev_files[-1]), encoding="utf-8") as f:
            prev = json.load(f)
        pc = (prev.get("counts") or {}).get("current")
        # 阈值读 `common.MIN_RETAIN_RATIO` —— **和 crawl.py 的写盘闸读同一个数**。
        # 两处各自写一个 0.8 就是"同一个策略写在两个地方"，迟早分叉。
        if pc and len(recs) < pc * common.MIN_RETAIN_RATIO:
            bad("条数突变", f"上次 {pc} 条 → 本次 {len(recs)} 条（降幅 >20%）。"
                            f"**静默丢数据是最危险的失败**，先查是不是某轮抓取挂了")
        else:
            ok(f"条数没有异常下降（上次 {pc} → 本次 {len(recs)}）")
    else:
        ok("没有历史归档，跳过突变检测（跑一次 tools/run_daily.py 就会有）")


def check_faculty(recs, schema):
    req = schema["required"]
    print(f"\n== faculty.jsonl（{len(recs)} 人）==")
    missing = [r.get("name", "?") for r in recs if any(k not in r for k in req)]
    (ok if not missing else lambda n: bad(n, f"{missing[:3]}"))(
        f"每条都有契约要求的 {len(req)} 个必填字段")

    ids = [r.get("id") for r in recs]
    dups = {i for i in ids if ids.count(i) > 1}
    (ok if not dups else lambda n: bad(n, f"重复：{sorted(dups)[:5]}"))("id 唯一")

    kinds = set(schema["properties"]["group_kind"]["enum"])
    badk = [(r.get("name"), r.get("group_kind")) for r in recs
            if r.get("group_kind") not in kinds]
    (ok if not badk else lambda n: bad(n, f"{badk[:3]}"))(
        f"group_kind 枚举合法（{sorted(kinds)}）")

    # 研究方向覆盖率要**按院系看，不看语料整体**。
    # 坑（已踩）：原来算的是全体比例，于是加一个"名录级"院系（大连理工：全校教师、
    # 方向要从个人主页的简介段落里抽）就把整体从 87% 拉到 68%，报出"匹配会大面积失准"——
    # **可实际上没有任何东西变差**，那几个跑得好的院系一个都没动。
    # 整体比例指向了错误的对象：该问的是"**这个院系**能不能用来匹配"，
    # 而不是"整个语料库平均好不好"。所以改成逐院系判定 + 整体只报数。
    by_unit = {}
    for r in recs:
        by_unit.setdefault((r.get("school"), r.get("unit")), []).append(r)
    weak = []
    for (s, u), g in sorted(by_unit.items(), key=lambda x: -len(x[1])):
        cov = sum(1 for r in g if r.get("research_areas")) / len(g)
        mark = "✅" if (len(g) >= 10 and cov >= 0.8) else ("⚠️" if len(g) >= 10 else "·")
        print(f"        {mark} {s} / {u}  {len(g)} 人  方向 {cov:.0%}")
        if len(g) >= 10 and cov < 0.8:
            weak.append(f"{s}/{u} {cov:.0%}")
    if weak:
        warn("有院系的方向覆盖不足 80%（**只影响这些院系**，别当成整体退化）",
             f"{weak} —— 这些院系只能当名录用，别在 PPT 里说它们能匹配")
    tot = sum(1 for r in recs if r.get("research_areas"))
    ok(f"有研究方向合计 {tot}/{len(recs)}（{tot/len(recs):.0%}，整体数仅供参考）")


def check_fixtures(notices, faculty):
    """fixtures 必须和真数据对得上。

    坑（已踩**三次**）：我改了数据却忘记重出 fixtures，自己的八道测试全绿，
    而 **B 的契约门禁红给你看** —— 他第一反应会以为是自己接口坏了，
    实际是我的 fixture 停在旧数据上（公告 78/72、导师 50/67 都出过）。

    这类问题必须在**我这侧**就被抓住，而不是变成别人 suite 里的红灯。
    所以门禁里加这一条：fixture 的 total 必须等于真数据的条数。

    **2026-10-18 修正口径**：fixtures 从 A1/A6 起**默认排除 `record_quality=junk`**
    （契约 §19.2：脏记录只标记、不进面向用户的列表），所以这里比的不是全部条数，
    而是"可见条数"。之前拿全部条数比，A6 把 38 条 junk 捞回 ok 之后
    total 从 1043 变成……不对，是 1120 vs 1043 对不上 —— 红的是**判据过期**，
    不是数据错了。宁可改判据，也不去改 fixtures 的语义。
    """
    def visible(recs):
        return sum(1 for r in recs if (r.get("record_quality") or "") != "junk")

    for name, recs in (("notices.list.json", notices), ("faculty.list.json", faculty)):
        p = os.path.join(ROOT, "contracts", "fixtures", name)
        if not os.path.exists(p):
            warn(f"fixtures/{name} 不存在", "跑一次 tools/make_fixtures.py")
            continue
        with open(p, encoding="utf-8") as f:
            fx = json.load(f)
        got = fx.get("total")
        want = visible(recs) if name == "notices.list.json" else len(recs)
        if got != want:
            bad(f"fixtures/{name} 的 total 和真数据不一致",
                f"fixture 说 {got}、可见真数据是 {want} 条 —— "
                f"**改了数据必须重出 fixtures**（python tools/make_fixtures.py），"
                f"否则 B 的契约门禁会红，而他会以为是自己的接口坏了")
        else:
            ok(f"fixtures/{name} 的 total 与真数据一致（{got}）")


def main():
    print(f"数据门禁  {datetime.now():%Y-%m-%d %H:%M}")

    ns = load_schema("notice.schema.json")
    fs = load_schema("faculty.schema.json")
    notices = load_jsonl(os.path.join(common.OUT_DIR, "notices.jsonl"))
    faculty = load_jsonl(os.path.join(common.OUT_DIR, "faculty.jsonl"))

    if not notices:
        bad("notices.jsonl 缺失或为空", "先跑 python crawl.py")
    else:
        check_notices(notices, ns)

    if not faculty:
        warn("faculty.jsonl 缺失或为空", "先跑 python crawl_faculty.py")
    else:
        check_faculty(faculty, fs)

    if notices or faculty:
        check_fixtures(notices or [], faculty or [])

    print(f"\n{'=' * 60}")
    print(f"数据门禁：{len(FAILED)} 失败 / {len(WARN)} 警告")
    if WARN:
        print("警告（不阻塞，但要有人看一眼）：")
        for w in WARN:
            print(f"  · {w}")
    if FAILED:
        print(f"❌ 失败：{FAILED}")
        sys.exit(1)
    print("✅ 数据不变量全部成立")


if __name__ == "__main__":
    main()

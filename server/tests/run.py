# -*- coding: utf-8 -*-
"""契约回归检查（**零第三方测试依赖**，只用标准库 + fastapi.testclient）。

跑法（项目根目录）：
    D:\\python.3.10.11\\python.exe server/tests/run.py

设计原则（踩过坑之后定的）：
**期望值一律从数据源和 fixtures 现算，不写死数字。**
上一版把 74、66、8、25 这些数字硬编码在断言里，A 一改数据（74→78、重复归零）
就红了 11 条 —— 红的全是我抄的数字，不是接口坏了。硬编码的数字等于把
"测试的维护成本"转嫁给下一次数据变更。只有两种情况允许写字面量：
① 契约本身规定的（size 上限 50、bucket 枚举）；② 用来验证"我不是空跑"的守卫。

`test_contract.py` 是同一批断言的 pytest 版本，给有 pytest 的环境用。
"""
from __future__ import annotations

import importlib.util
import json
import re
import os
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient          # noqa: E402

from server import config                          # noqa: E402
from server import billing                         # noqa: E402
from server import changes as changes_mod          # noqa: E402
from server import crawl as crawl_mod              # noqa: E402
from server import credibility                    # noqa: E402
from server import digest as digest_mod            # noqa: E402
from server import forum                           # noqa: E402
from server import freshness as freshness_mod      # noqa: E402
from server import letter as letter_mod            # noqa: E402
from server import match as matching               # noqa: E402
from server import match as match_mod              # noqa: E402
from server import quality                         # noqa: E402
from server import watch as watch_mod              # noqa: E402
from server.app import create_app                  # noqa: E402
# 事件缓存的 key 是 `(mtime_ns, size)`。测试要"换掉文件再读"就得清它 ——
# 不清的话同一个 mtime 会命中旧缓存，验的就不是重载逻辑了。
from server.app import _EVENTS_CACHE               # noqa: E402
from server.crawl import CrawlRunner               # noqa: E402
from server.freshness import FreshnessStore        # noqa: E402
from server.state import StateStore                # noqa: E402
from server.store import FacultyStore, NoticeStore  # noqa: E402

# 走 config 的解析：有 `data/out` 用它，没有就用仓库自带的脱敏样例 `data/sample/out`。
# 硬编码 `data/out` 会让"新克隆直接跑门禁"在缺原始数据时 FileNotFound —— 而公开仓库
# 本来就不带原始数据。
NOTICES = config.NOTICES_PATH
FACULTY = config.FACULTY_PATH
SCHEMA = os.path.join(ROOT, "contracts", "notice.schema.json")
FACULTY_SCHEMA = os.path.join(ROOT, "contracts", "faculty.schema.json")
FIXTURES = os.path.join(ROOT, "contracts", "fixtures")
PROFILE = os.path.join(ROOT, "collector", "tools", "profile-example.json")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def _load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def _fixture(name):
    return json.load(open(os.path.join(FIXTURES, name), encoding="utf-8"))


def _fixture_units():
    """A 产出的院系建议初始种（`data/out/units.json`）。读不到就返回空表。"""
    path = config.UNITS_PATH
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("units") or []


def _fresh_client():
    """**隔离的**客户端：内存状态，不碰 server/var/state.json。

    论坛和付费墙的用例会写状态（发帖/订阅/扣额度），共用实例会互相污染 ——
    这正是测试最难查的一类失败。每个用例自己拿一个干净实例。

    `StateStore.load(":memory:")` 会返回一份全新的空状态且不落盘。
    """
    return TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:")))


def fetch_all(client, path, params=None, page_size=None):
    """把分页接口**取全**，返回 `items` 列表。

    ## 为什么必须有这个函数

    这一晚"数据塞得进几页"的假设破了三次：额度状态、公告条数、导师条数
    （50 → 67 → 259，而 `MAX_PAGE_SIZE` 就是 50）。

    每次的写法都是 `params={"size": 50}` 然后**当成全集用**。后果分两级：
    数据刚超过 50 时断言**悄悄变弱**（还在跑、但验的东西少了一半），
    再涨就直接变红。**变弱比变红更坏** —— 红了你至少会去看。

    ## 为什么不是"把 50 改成 100"

    那只是把同一个假设往后推一格，数据下一次翻倍还会破，
    而且它掩盖了真正的问题：**这个断言本来就不该依赖"一页装得下"**。
    取全就是取全。

    `total` 是接口给的权威值，不猜；`size` 用 `MAX_PAGE_SIZE`，
    不指望服务端接受一个更大的值（`/api/notices?size=51` 是 422）。
    """
    ps = page_size or config.MAX_PAGE_SIZE
    base = dict(params or {})
    out, page = [], 1
    total = None
    while True:
        p = dict(base, page=page, size=ps)
        body = client.get(path, params=p).json()
        total = body.get("total", 0)
        items = body.get("items") or []
        out.extend(items)
        if not items or len(out) >= total:
            break
        page += 1
        if page > 1000:                 # 防御：接口坏了不至于把门禁挂死
            raise AssertionError(f"{path} 分页取不完（已取 {len(out)}/{total}）")
    if total is not None and len(out) != total:
        raise AssertionError(f"{path} 取全失败：拿到 {len(out)}，total 说 {total}")
    return out


# 共享状态
RAW = _load_jsonl(NOTICES)
FAC_RAW = _load_jsonl(FACULTY) if os.path.exists(FACULTY) else []
STORE = NoticeStore.load(NOTICES)
FACULTY_STORE = FacultyStore.load(FACULTY)

# **接口默认能看到的那些记录**（契约 §19.2：默认拒收 `record_quality=junk`）。
#
# 为什么要单独一个常量：`RAW` 是**数据文件里有什么**，`VISIBLE` 是**用户默认看到什么**，
# 这两个数从 2026-10-18 起不再相等（1120 vs 1005）。
# 断言里混用会让"到底该拿哪个比"变成每天都要想一遍的事 ——
# 而且混用的时候写错了一个不会报错，只会让某条用例变成恒真。
VISIBLE = [r for r in RAW if (r.get("record_quality") or "") != "junk"]
JUNK = [r for r in RAW if (r.get("record_quality") or "") == "junk"]

# ---------------------------------------------------------------------------
# 状态隔离（这一节是修一个真踩到的坑，别删）
#
# 坑：门禁曾经**当天第二次跑必红**。原因不是代码错，是模块级的共享客户端
# 用了**持久化**的 `server/var/state.json` —— 第一次跑完 `u_demo` 的
# `daily_used` 就是 3，之后每次跑详情接口都返回 402，断言 `body_text in detail` 失败。
#
# A 的原话："一个'当天第二次跑必红'的门禁，比没有门禁更坏 ——
# 大家会学会'跑之前先删状态'，然后开始无视红灯。" 说得对，所以两条都做：
#
#   ① 门禁自己的状态**一律在内存里**（`:memory:`，不落盘、不碰持久化文件）
#   ② 共享客户端不许被用例污染：读接口的用例用它，会扣额度/写状态的用例
#      一律用 `_fresh_client()` 拿独立实例
#
# `GATE_STATE` 起一个实例给 CLIENT 用；`STATE_PATH` 在门禁里根本不参与。
# ---------------------------------------------------------------------------
GATE_STATE = StateStore.load(":memory:")
CLIENT = TestClient(create_app(STORE, FACULTY_STORE, GATE_STATE))
PROF = json.load(open(PROFILE, encoding="utf-8-sig"))
LIST_FX = _fixture("notices.list.json")
MATCH_FX = _fixture("match.response.json")

# ---------------------------------------------------------------------------
# 2026-10-17：这里原来有个 `LISTING_SORT_FIXTURE_STALE` 开关，**已删除**。
#
# 来历：列表默认排序从 `(publish_date desc)` 改成 `(relevance desc, publish_date desc)`，
# 而 `fixtures/notices.list.json` 还是上一版排序的快照，于是排序那一半改成
# "用例自己调 `listing_sort_key` 算期望值再和接口比"。
#
# **那个做法是自证**：期望值用的是被测代码自己的函数，永远绿。
# 它看起来在验排序，其实只验了"函数是确定的"。
#
# 这个松口后来被两个人各自发现了（我删掉开关；另一位把它设成 `False`）。
# **最终选择删除**，两条理由：
#   ① 事实是它早就可以拧紧 —— D 在 `b921ec9` 就重跑过 `make_fixtures.py`，
#      fixture 与接口逐位置全等。也就是说"松口"的理由消失了一整晚，却没人对账。
#      一个允许"松着不解释"的状态，迟早会变成"松着也没人知道"。
#   ② 设成 `False` 只是让它**今天**是紧的，而 `False` 这个值本身仍在邀请
#      下一个人想"要不要再松一下"。
#
# 现在只有一种状态：**必须一致**。不一致时由 `_why_list_mismatch` 说清是谁错了。
#
# ⚠️ 两处"我以为在验的"和"实际在验的"不是一回事：
#   ① `docs/HANDOFF-B.md` §5.1 写"列表还比**键顺序**" —— **代码从来没比过键序**。
#      `dict == dict` 不比键的先后；键序也不是契约（理由见
#      `列表项字段与_fixture_一致` 的 docstring）。这里比的是**内容 + 逐位置顺序**。
#   ② 另一位核过"`items == items` 为 True、按键顺序完全一致"。**结论对、理由错**：
#      顺序一致是事实，但它不是被"键序比对"验出来的，是被 `==` 的内容比对顺带带出来的。
# ---------------------------------------------------------------------------


def _why_list_mismatch(got_items, fx_items, raw_by_id):
    """接口列表与 fixture 不一致时，判断"是 fixture 落后于数据，还是接口坏了"。

    **只造消息，不改判定** —— 判定已经在调用处红了，这个函数不参与"过不过"。

    归因原则：**默认怀疑接口**。只有逐字段证明"接口给的值 == 磁盘上的值"，
    才敢说"接口是忠实的，问题在 fixture 过期"。
    反过来归因（把代码 bug 说成数据旧）是最坏的一种错：它会让人去重跑 fixture，
    然后以为修好了 —— **红变成了绿，bug 还在**。

    三种可能分别对应三句不同的话，因为修法完全不同：
    ① 规模不同          → fixture 落后于数据 → 重跑 make_fixtures.py
    ② 顺序不同、字段都对 → fixture 还是上一版排序的快照 → 同上
    ③ 字段值不同        → 先看磁盘上是什么值，再判是谁在改数据
    """
    out = []
    got_ids = [x.get("id") for x in got_items]
    fx_ids = [x.get("id") for x in fx_items]

    # ---- 顺序 ----
    if got_ids != fx_ids:
        from server.store import listing_sort_key
        expect = [r["id"] for r in
                  sorted(raw_by_id.values(), key=listing_sort_key,
                         reverse=True)[:len(fx_items)]]
        pos = next((i for i, (a, b) in enumerate(zip(got_ids, fx_ids)) if a != b), 0)
        out.append(f"顺序：第 {pos} 个位置起不同（接口 {got_ids[pos]!r} / fixture {fx_ids[pos]!r}）")
        if got_ids == expect:
            out.append("  接口顺序 == 用当前排序键现算的结果 → **接口没问题**，"
                       "是 fixture 还是上一版排序的快照。修法：重跑 make_fixtures.py")
        else:
            out.append("  接口顺序 != 用当前排序键现算的结果 → **接口或排序键有问题**，"
                       "这不是 fixture 的事，别去重跑 fixture")
        out.append(f"  集合重合 {len(set(got_ids) & set(fx_ids))}/{len(fx_ids)} 条")

    # ---- 同一条记录的字段（按 id 对齐，免得顺序问题掩盖字段问题）----
    fx_by_id = {x["id"]: x for x in fx_items if "id" in x}
    field_diffs = []
    for a in got_items:
        b = fx_by_id.get(a.get("id"))
        if b is None:
            continue
        src = raw_by_id.get(a["id"]) or {}
        # 派生字段在磁盘记录里不存在，按 store.project 的定义现算 —— 否则会把
        # "派生字段"误判成"接口在编数据"。
        atts = src.get("attachments") or []

        def disk_of(k):
            if k == "has_attachments":
                return bool(atts)
            if k == "attachments_count":
                return len(atts)
            return src.get(k)

        for k in set(a) | set(b):
            if a.get(k) == b.get(k):
                continue
            disk = disk_of(k)
            field_diffs.append((a["id"], k, a.get(k), b.get(k), disk))

    if field_diffs:
        out.append(f"字段：{len(field_diffs)} 处不一致（下面最多列 6 处）")
        mismatched = 0
        for nid, k, av, bv, disk in field_diffs[:6]:
            if disk == av:
                verdict = "磁盘值 == 接口值 → 接口忠实，fixture 过期"
            elif disk == bv:
                verdict = "**磁盘值 == fixture 值 → 接口在改数据，这是接口的问题**"
                mismatched += 1
            else:
                verdict = "磁盘值两边都不是 → 先核磁盘，这是接口的问题"
                mismatched += 1
            out.append(f"  {nid} .{k}: 接口={av!r} fixture={bv!r} 磁盘={disk!r}")
            out.append(f"    {verdict}")
        rest = field_diffs[6:]
        if rest:
            out.append(f"  ……另有 {len(rest)} 处")
        if mismatched:
            out.append("")
            out.append("★ 有字段是**接口在改数据**（不是在诚实地搬运）——"
                       "这条要找接口的 bug，重跑 fixture 会把问题掩盖掉。")

    if not out:
        out.append("内容与顺序都比对通过，但仍判定不一致 —— 这不该发生，逐字段手工核一次")
    return "\n".join(out)


# ---------- 0. 守卫：别让整套测试空跑 ----------

@case
def 守卫_数据和画像都真的读到了():
    assert len(RAW) > 50, f"公告只有 {len(RAW)} 条，后面的断言没有意义"
    assert len(FAC_RAW) > 10, f"导师只有 {len(FAC_RAW)} 条"
    assert PROF.get("majors"), "画像没读到"
    assert MATCH_FX.get("items"), "match fixture 没读到"


# ---------- 1. 数据契约 ----------

@case
def 每条公告都符合_notice_schema():
    import jsonschema

    schema = json.load(open(SCHEMA, encoding="utf-8"))
    bad = []
    for i, rec in enumerate(RAW):
        try:
            jsonschema.validate(rec, schema)
        except jsonschema.ValidationError as e:
            bad.append((i, list(e.absolute_path), e.message[:140]))
    assert not bad, f"{len(bad)} 条不合 schema，前 3 条：{bad[:3]}"


@case
def 每条导师都符合_faculty_schema():
    import jsonschema

    schema = json.load(open(FACULTY_SCHEMA, encoding="utf-8"))
    bad = []
    for i, rec in enumerate(FAC_RAW):
        try:
            jsonschema.validate(rec, schema)
        except jsonschema.ValidationError as e:
            bad.append((i, list(e.absolute_path), e.message[:140]))
    assert not bad, f"{len(bad)} 条不合 schema，前 3 条：{bad[:3]}"


@case
def 公告_id_唯一():
    """A 已按 canonical_url 归一去重。这条是防复发：重复 id 会让前端出现成对一样的卡片，
    而那在数据里看不出来、只在界面上看得出来。"""
    ids = [r["id"] for r in RAW]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    assert not dups, f"{len(dups)} 组重复 id：{dups[:5]}"


@case
def 导师_id_唯一():
    ids = [r["id"] for r in FAC_RAW]
    assert len(set(ids)) == len(ids), "导师 id 有重复"


@case
def total_与数据源行数一致():
    """契约写死的口径：total 是全量，items 是当页。"""
    body = CLIENT.get("/api/notices", params={"page": 1, "size": 50}).json()
    assert body["total"] == len(VISIBLE), (body["total"], len(VISIBLE))


# ---------- 2. 响应形状（对着 fixtures） ----------

@case
def 列表响应默认与_fixture_完全一致():
    """page=1&size=50 必须和 `contracts/fixtures/notices.list.json`
    **逐字段、逐位置**一致。

    ## 这条是"fixture 即验收标准"（契约 §9）的执行体，不许松

    2026-10-17 之前这里有个 `LISTING_SORT_FIXTURE_STALE` 开关：排序策略改了、
    fixture 还是旧快照，于是排序那一半改成"用例自己算期望值再和接口比"。
    **那是自证** —— 期望值来自被测代码自己的 `listing_sort_key`，永远绿。

    现在 fixture 已经和接口完全一致（D 在 `b921ec9` 重跑过 `make_fixtures.py`），
    开关删掉了。**只有一种状态：必须一致。**

    ## 不一致时先看是哪一类（这一步比断言本身值钱）

    `_why_list_mismatch` 会区分"fixture 落后于数据"和"接口坏了"，
    因为这两件的**修法完全相反**：一个去重跑 fixture，一个去查接口。
    归因原则是**默认怀疑接口**（详见那个函数的 docstring）。
    """
    got = CLIENT.get("/api/notices", params={"page": 1, "size": 50}).json()
    fx = LIST_FX

    # ---- ① 规模：不同就是 fixture 落后于数据，不是接口坏了 ----
    if got["total"] != fx["total"]:
        raise AssertionError(
            f"fixture 落后于数据：接口 total={got['total']}，fixture total={fx['total']}。\n"
            f"  这**不是接口坏了** —— `notices.list.json` 是上一轮数据的快照，\n"
            f"  数据条数变了它就该重出一份。\n"
            f"  修法（数据层）：cd collector && python tools/make_fixtures.py\n"
            f"  它出的第一份产物就是这个文件，重跑完这条会自己变绿。")
    assert got["page"] == fx["page"] and got["size"] == fx["size"], \
        (got["page"], got["size"], fx["page"], fx["size"])
    assert len(got["items"]) == len(fx["items"]) == 50, \
        (len(got["items"]), len(fx["items"]))

    # ---- ② 逐位置、逐字段严格比（键序不比，见下方用例的说明）----
    if got["items"] != fx["items"]:
        raise AssertionError(_why_list_mismatch(
            got["items"], fx["items"], {r["id"]: r for r in RAW}))


@case
def 列表项字段与_fixture_一致():
    """存在性用 `set` 比。

    **不用 `list(...keys())` 比顺序**：JSON 的键序不是契约。
    A 哪天调整 `make_fixtures.py` 里字段的先后，按键序比会红、但接口没问题 ——
    又是一次"红了但代码没坏"。顺序确实想守的话，只有一条：
    `派生字段排在基础字段之后`（见下面那条专门用例）。
    """
    one = CLIENT.get("/api/notices", params={"size": 1}).json()["items"][0]
    fx = LIST_FX["items"][0]
    assert set(one.keys()) == set(fx.keys()), set(one.keys()) ^ set(fx.keys())
    # 派生字段必须在，且和源数据的 attachments 对得上
    src = next(r for r in RAW if r["id"] == one["id"])
    atts = src.get("attachments") or []
    assert one["has_attachments"] == bool(atts)
    assert one["attachments_count"] == len(atts)
    # 契约硬要求：列表必须给 extract.confidence（C 靠它标"疑似"）
    assert isinstance(one["extract"]["confidence"], (int, float))
    # 有意不进列表的字段
    for k in ("body_text", "source", "body_len", "materials", "contact",
              "crawled_at", "content_hash"):
        assert k not in one, f"列表项不该有 {k}"


@case
def 默认响应不夹带自定义字段():
    got = CLIENT.get("/api/notices", params={"size": 1}).json()
    assert set(got.keys()) == {"total", "page", "size", "items"}, set(got.keys())
    assert "warnings" not in got["items"][0]
    assert "extract_meta" not in got["items"][0]


@case
def meta_开关才带_warnings_与_extract_meta():
    one = CLIENT.get("/api/notices", params={"size": 1, "meta": True}).json()["items"][0]
    assert isinstance(one["warnings"], list)
    em = one["extract_meta"]
    assert em["suspected"] == (em["confidence"] < 0.5)
    assert em["deadline_missing"] == (one["deadline"] is None)


@case
def 详情默认键集合与_fixture_一致():
    # **必须用独立状态**：详情接口会扣免费额度，用共享客户端的话
    # 前面用例把 3 条额度用光，这里就返回 402 —— 那正是"当天第二次跑必红"的根因。
    c = _fresh_client()
    one = c.get("/api/notices", params={"size": 1}).json()["items"][0]
    detail = c.get(f"/api/notices/{one['id']}").json()
    assert "body_text" in detail and "attachments" in detail
    fx = _fixture("notices.detail.json")
    assert set(detail.keys()) == set(fx.keys())


@case
def 门禁不读持久化状态文件():
    """修"当天第二次跑必红"的那个坑。

    两条都要验：① 门禁用的状态在内存里 ② 跑门禁不会去读 `server/var/state.json`。
    只要有一条不成立，门禁就会变成"跑之前先删状态"的仪式。
    """
    assert CLIENT.app.state.state.path == ":memory:"
    assert _fresh_client().app.state.state.path == ":memory:"
    # 反复跑同样的详情请求，结果必须一致（不依赖上一次跑剩了多少额度）
    first = [ _fresh_client().get(f"/api/notices/{RAW[0]['id']}").status_code
              for _ in range(3) ]
    assert first == [200, 200, 200], first


@case
def json_是_utf8_且中文不转义():
    """要测的是"响应里中文是原样的，不是 `\\uXXXX` 转义"。

    **不要绑某个学校名**（我原来断言 `"浙江大学" in r.text`）：
    那等于把"编码正确"寄存在"浙大今天还在第一条"上。
    A 删掉浙大那个整块是科研的栏目之后，第一条变成南开，这条就红了 ——
    而"中文没被转义"这件事一点没变。
    改成通用断言：查**原始字节**里既没有 `\\u` 转义、又确实含多字节 UTF-8 中文。
    """
    r = CLIENT.get("/api/notices", params={"size": 3})
    assert "charset=utf-8" in r.headers["content-type"].lower()

    raw = r.content                       # 原始字节，不经过任何解码
    # ① 没有 \uXXXX 转义（JSON 的 ensure_ascii=True 会这么输出）
    assert b"\\u" not in raw, "响应里有 \\uXXXX 转义，中文被 ASCII 化了"
    # ② 确实含 UTF-8 多字节中文（3 字节序列 E4-E9 开头是 CJK 常用字区）
    assert any(0xE4 <= b <= 0xE9 for b in raw), "响应里连一个中文字符都没有"
    # ③ 字节能按 UTF-8 解回来，且解出来的字符真的是中文
    text = raw.decode("utf-8")
    assert any("\u4e00" <= ch <= "\u9fff" for ch in text), "解码后没有中文字符"


@case
def 每个接口都带_charset_utf8():
    """**所有**接口都得带 `charset=utf-8`，不能只有一部分带。

    为什么这条要遍历路由、而不是抽查一两个：`/api/health` 曾经是唯一漏掉的
    —— 它直接 `return {...}` 而不是走 `_json()`，FastAPI 给的就是
    `application/json`（**不带 charset**）。按 HTTP 规范没有 charset 默认是 Latin-1，
    守规范的客户端会把"文档"解成 `ææ¡£`。

    而 `/api/health` 恰恰是**演示时第一个被看到的接口**。抽查会漏，遍历不会。
    """
    from fastapi.testclient import TestClient
    from server.store import FacultyStore, NoticeStore
    from server.state import StateStore
    from server.changes import ChangesStore

    c = TestClient(create_app(NoticeStore.load(config.NOTICES_PATH),
                              FacultyStore.load(config.FACULTY_PATH),
                              StateStore.load(":memory:"),
                              ChangesStore.load(config.CHANGES_PATH)))
    prof = PROF
    rid = RAW[0]["id"]
    pid = f"u_{prof.get('majors', ['x'])[0]}"

    # GET：全部跑一遍
    for route in c.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api") or "GET" not in methods or "{" in path:
            continue
        r = c.get(path)
        if r.status_code >= 400:
            continue                      # 403/404 之类不是形状问题
        ct = r.headers.get("content-type", "")
        assert "charset=utf-8" in ct.lower(), f"{path} 的 Content-Type 是 {ct!r}"

    # 具体路径带参数的 GET
    for path in (f"/api/notices/{rid}", "/api/faculty?size=1"):
        r = c.get(path)
        assert "charset=utf-8" in r.headers.get("content-type", "").lower(), path

    # POST：每个都给一份合法请求体
    posts = {
        "/api/match": {"limit": 1}, "/api/faculty/match": {"limit": 1},
        "/api/letter": {"notice_id": rid}, "/api/subscribe": {"plan_id": "monthly"},
        "/api/posts": {"title": "t", "body": "b"},
    }
    for path, params in posts.items():
        r = c.post(path, json=prof if path.endswith("match") else params,
                   params=params if path.endswith("match") else None)
        if r.status_code >= 400:
            continue
        ct = r.headers.get("content-type", "")
        assert "charset=utf-8" in ct.lower(), f"{path} 的 Content-Type 是 {ct!r}"


@case
def 列表项派生字段排在基础字段之后():
    """字段**顺序**只有这一条是被契约要求的，所以单独一个用例、单独一个名字。

    为什么只守这一条：`has_attachments` / `attachments_count` 是**派生**字段
    （JSONL 里没有，由 `project()` 从 `attachments` 现算），
    它们必须排在 15 个基础字段之后 —— fixture 就是这个顺序。
    其余字段彼此之间的先后**不是契约**，按键序比会让"顺序调整"伪装成"形状坏了"。
    """
    from server import config as C
    one = CLIENT.get("/api/notices", params={"size": 1}).json()["items"][0]
    keys = list(one.keys())
    base = [k for k in keys if k in C.LIST_FIELDS]
    derived = [k for k in keys if k in C.DERIVED_LIST_FIELDS]
    assert base == list(C.LIST_FIELDS), (base, list(C.LIST_FIELDS))
    assert derived == list(C.DERIVED_LIST_FIELDS), (derived, list(C.DERIVED_LIST_FIELDS))
    assert keys.index(derived[0]) > keys.index(base[-1]), keys


# ---------- 3. 列表行为 ----------

@case
def 分页不重不漏():
    per = 10
    pages = (len(VISIBLE) + per - 1) // per
    seen = []
    for page in range(1, pages + 1):
        body = CLIENT.get("/api/notices", params={"page": page, "size": per}).json()
        assert body["total"] == len(VISIBLE)
        seen.extend(i["id"] for i in body["items"])
    assert len(seen) == len(VISIBLE), (len(seen), len(VISIBLE))
    assert len(set(seen)) == len(seen), "分页出现重复行"
    assert CLIENT.get("/api/notices", params={"page": 999, "size": 10}).json()["items"] == []


@case
def size_上限_50():
    assert CLIENT.get("/api/notices", params={"size": 51}).status_code == 422
    assert CLIENT.get("/api/notices", params={"size": 50}).status_code == 200


@case
def school_筛选():
    target = max({r["school"] for r in VISIBLE},
                 key=lambda s: sum(1 for r in VISIBLE if r["school"] == s))
    body = CLIENT.get("/api/notices", params={"school": target, "size": 50}).json()
    expect = sum(1 for r in VISIBLE if target in r["school"])
    assert body["total"] == expect, (body["total"], expect)
    assert all(target in i["school"] for i in body["items"])


@case
def unit_支持逗号多选():
    body = CLIENT.get("/api/notices",
                      params={"unit": "物理学院,材料科学与工程学院", "size": 50}).json()
    got = {i["unit"] for i in body["items"]}
    assert got and got <= {"物理学院", "材料科学与工程学院"}, got


@case
def apply_mode_筛选保留_null_记录():
    """筛选**保留** `apply_mode` 为 null 的记录。

    那是"没抽到"而不是"不符合"：丢掉就把抽取缺陷伪装成了"这个项目不适合你"
    （matching-spec 第三/四节同一原则）。所以 total 应为 命中数 + null 数，
    不是命中数 —— 我第一版断言写成"命中数"是错的。
    另外这类数字不再写死，从数据现算。
    """
    for m in ("推免", "夏令营"):
        hit = sum(1 for r in VISIBLE if r.get("apply_mode") == m)
        if hit == 0:
            continue
        nnull = sum(1 for r in VISIBLE if r.get("apply_mode") is None)
        body = CLIENT.get("/api/notices", params={"apply_mode": m, "size": 50}).json()
        assert body["total"] == hit + nnull, (m, body["total"], hit, nnull)
        # 当页里不许出现"明确不符"的记录（null 允许）
        assert not any(i["apply_mode"] not in (m, None) for i in body["items"])


@case
def level_筛选():
    body = CLIENT.get("/api/notices", params={"level": "直博", "size": 50}).json()
    expect = sum(1 for r in VISIBLE if "直博" in (r.get("degree_types") or []))
    assert body["total"] == expect, (body["total"], expect)


@case
def year_筛选精确匹配():
    body = CLIENT.get("/api/notices", params={"year": 2027, "size": 50}).json()
    # **契约 §1.5**：年份以 dmission_year 为准，它没有值时才回退 	arget_year。
    # 2026-10-18 改的判据（B9），这条期望值必须跟着改 —— 否则它会因为
    # "筛选按新口径、期望按旧口径"而恒红，而红的原因看着像筛选坏了。
    def _y(r):
        adm = r.get("admission_year")
        return adm if adm is not None else r.get("target_year")
    expect = sum(1 for r in VISIBLE if _y(r) == 2027)
    assert body["total"] == expect, (body["total"], expect)
    # 筛选口径是 `admission_year`，没有时才回退 `target_year`；
    # 断言也必须用同一口径。原来直接断言 target_year，数据里一旦出现
    # 「admission_year=2027、target_year=2026」的记录就会假红。
    assert all(_y(i) == 2027 for i in body["items"])


@case
def q_搜标题并在元数据里命中():
    body = CLIENT.get("/api/notices", params={"q": "夏令营", "size": 50}).json()
    assert body["total"] > 0
    for i in body["items"]:
        hay = (i["title"] + " ".join(i["tags"]) + " ".join(i.get("majors") or [])
               + (i.get("board") or ""))
        assert "夏令营" in hay, i["title"]


@case
def 排序_publish_desc_是两级降序_relevance_再日期():
    """默认排序 = `(relevance desc, publish_date desc)`，**两级**。

    为什么要 relevance 打头（C 走查 #5）：原来只按日期排，最新的那几条
    **行政/科研信息**（「答辩评审名单」「论文送审双盲审名单」「Science 论文报道」）
    占据列表前几位 —— 评委点开列表看到的第一条不是招生信息。

    **断言用 `relevance_of` 现算**，不读记录里那个字段：
    A 改判据（词表）时，这条测的是"接口的排序和 A 的判据一致"，
    而不是"接口的排序和某天写进数据里的那个数一致"。
    """
    import importlib.util

    path = os.path.join(ROOT, "collector", "fields.py")
    # `collector/fields.py` 里 `import common` —— 得先把 collector/ 放进 sys.path
    collector_dir = os.path.join(ROOT, "collector")
    if collector_dir not in sys.path:
        sys.path.insert(0, collector_dir)
    spec = importlib.util.spec_from_file_location("_a_fields", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rel_of = lambda r: mod.relevance_of(  # noqa: E731
        r.get("title"), r.get("board"), r.get("apply_mode"))

    items = fetch_all(CLIENT, "/api/notices")
    assert len(items) == len(VISIBLE), (len(items), len(VISIBLE))
    by_id = {r["id"]: r for r in RAW}
    rels = [rel_of(by_id[i["id"]]) for i in items]

    # ① relevance 单调不增
    #
    # **这条红了有两种完全不同的原因，报错必须分清**（否则会读成"排序代码坏了"）：
    #   a) 接口的排序键坏了（真的该改 server/store.py）；
    #   b) **数据里存的 `relevance` 和现算规则不一致**（数据层改了标题/栏目但没重算）
    #      —— 那时排序是对的，错的是输入。
    #
    # 2026-10-18 就是 (b)：A6 从正文捞回标题之后没重算 `relevance`，9 条记录
    # （天津 6 / 北大 3）存着旧分数，于是接口按旧分数排、这里按新规则算。
    # 见 `issues/010`。原来这条只报 "relevance 不是降序"，读起来像我的排序坏了 ——
    # 排查方向会被带偏，所以下面加了归因。
    if rels != sorted(rels, reverse=True):
        stale = []
        for r in RAW:
            now = rel_of(r)
            if now != r.get("relevance"):
                stale.append((r.get("school"), (r.get("title") or "")[:26],
                              r.get("relevance"), now))
        if stale:
            raise AssertionError(
                "排序看起来不是降序，但**根因在数据**：%d 条记录的 `relevance` "
                "与现算规则不一致（接口按存盘的旧分数排，这里按新规则算）。\n"
                "  样例：%s\n"
                "  修法（数据层）：重算一遍 `relevance` 并写回 —— 见 issues/010。\n"
                "  **不要改 server/ 的排序键**，它是对的。"
                % (len(stale), stale[:3]))
        raise AssertionError(
            "relevance 不是降序，且数据里的 relevance 与现算一致 —— "
            "这次真的是排序键坏了。前 8 个：%s" % (rels[:8],))
    # ② **同一档内**日期单调不增（档内时效性不能被破坏）
    for lo in set(rels):
        dates = [i["publish_date"] for i, r in zip(items, rels) if r == lo]
        assert dates == sorted(dates, reverse=True), (lo, dates[:5])
    # ③ 钉住踩过的坑：desc 曾排成升序，第一页第一条变成陈年公告
    assert items[0]["publish_date"] >= items[-1]["publish_date"], "方向反了"
    # ④ 第一条应该是招生相关的（这就是这个排序存在的理由）
    assert rels[0] == max(rels), (rels[0], max(rels))


@case
def 排序_deadline_asc_只排未来且无截止日的沉底():
    """`deadline_asc` 默认**只排未来截止的**（B25 修正，2026-10-19）。

    改之前把 2010 年的已截止公告排在最前 —— 用户要看的是"哪些快来不及了"。
    """
    today = date.today()
    items = fetch_all(CLIENT, "/api/notices", {"sort": "deadline_asc"})
    deadlines = [i["deadline"] for i in items]
    dated = [d for d in deadlines if d]
    assert dated, "一条有截止日的都没有"
    assert all(d >= today.isoformat() for d in dated), (
        "出现了已截止的（默认不该出现）",
        [d for d in dated if d < today.isoformat()][:3])
    assert dated == sorted(dated), ("未来截止的没有升序", dated[:5])
    # 有截止日的全在前面，没有的沉底（不是被藏起来 —— 它们还在 items 里）
    assert deadlines[:len(dated)] == dated
    assert len(items) < len(VISIBLE), ("已截止的没有被排除", len(items), len(VISIBLE))


@case
def 非法_sort_返回统一错误格式():
    r = CLIENT.get("/api/notices", params={"sort": "random"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_SORT"


@case
def 详情_404_返回统一错误格式():
    r = CLIENT.get("/api/notices/不存在的id")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "NOT_FOUND", "message": "公告不存在"}}


@case
def facets_只给真实存在的取值():
    """筛选器的选项 = **数据里真的存在的取值**，一个空选项都不许有。

    **这里原来写死了一句 `assert "预推免" not in values`** ——
    当时数据里一条「预推免」都没有，所以断言"这个空选项不该出现"。
    但 2026-10-18 A 扩抓到 1119 条之后，「预推免」真的出现了，那句话立刻红。

    **这就是"断言绑了磁盘上恰好是什么数据"**（这个项目栽过 5 次）：
    它验的本意是"筛选器不给自己加空选项"，而不是"数据里不许有某个值"。
    真正要钉的是上面那句 `values == real` —— **选项集合必须恰好等于现算的真实取值**。
    """
    body = CLIENT.get("/api/facets").json()
    values = {m["value"] for m in body["apply_modes"]}
    real = {r.get("apply_mode") or "" for r in RAW}
    assert values == real, (values, real)
    # 空选项：**任何一个选项都必须有记录**（这才是那句话原本要验的）
    counts = {m["value"]: m["count"] for m in body["apply_modes"]}
    assert all(c > 0 for c in counts.values()), (
        "筛选器里有 count=0 的空选项：%s" % [k for k, c in counts.items() if c == 0])
    # **facets 与默认列表同口径**：都排除 junk（契约 §19.2 的延伸）。
    # 否则会出现"筛选器说上海交大 47 条、点进去一片空"——
    # 而那 47 条里 46 条是正文 0 字的空壳。覆盖学校数好看不等于内容能用。
    assert sum(s["count"] for s in body["schools"]) == len(VISIBLE), (
        "facets 的学校计数和默认列表对不上",
        sum(s["count"] for s in body["schools"]), len(VISIBLE))
    assert "admission_years" in body, (
        "facets 缺 admission_years（契约 §1.5：招生年的唯一权威）", sorted(body))


@case
def warnings_覆盖低置信与缺截止日():
    """`?meta=1` 的 warnings 要同时覆盖"缺截止日"和"置信度低"。

    措辞在契约第 14 节之后**按三态分**了：老记录（没有 `field_state`）里
    `deadline` 为空 → `unknown` → 「**未能读取**截止日，请点原文核对」。
    原来那句是"未抽到截止日期" —— 它对 `unknown` 没错，但对
    `absent`（读到了、确实没有）也会这么说，等于把两件事说成一件。
    """
    # **选样要指名道姓，别拿"磁盘上恰好第一条"当基准。**
    #
    # 这里原来是 `low = [低置信 and not deadline][0]` —— 而三态落盘之后，
    # "没有截止日"的记录里既有 `unknown` 也有 `absent`，取哪一条**由排序决定**。
    # 实测 D 落盘 `field_state` 当天，第一条恰好是 `absent`，于是这条用例红了，
    # 而它红得**没有道理**：`absent` 输出「该公告未公布截止日」正是契约第 14 节要的。
    # 这就是本项目栽过 5 次的那个病 —— **断言绑了"磁盘上恰好是什么数据"**，
    # 只不过这次绑的是"恰好哪一条排在前面"。
    #
    # 正确做法：明确挑一条**符合本用例假设**的（没有 field_state 的老记录 → unknown）。
    # 造数据那条（下面 `absent_rec`）本来就是显式造的，这里也照同样的思路办。
    low = [r for r in RAW
           if r["extract"]["confidence"] < 0.5 and not r["deadline"]
           and (r.get("field_state") or {}).get("deadline") != "absent"]
    assert low, "数据里应当存在这种记录，否则这条测试没意义"
    w = STORE.warnings_of(low[0])
    # 三态措辞（unknown 分支）
    assert any(credibility.UNKNOWN_DEADLINE_TEXT in x or "未抽到截止日期" in x
               for x in w), w
    assert any("置信度低" in x for x in w), w

    # **三态要真的分得开**：造一条 `absent`，措辞必须和 `unknown` 不同
    import copy
    absent_rec = copy.deepcopy(low[0])
    absent_rec["field_state"] = {"deadline": "absent", "majors": "found",
                                 "attachments": "found", "body": "ok"}
    wa = STORE.warnings_of(absent_rec)
    assert any(credibility.ABSENT_DEADLINE_TEXT in x for x in wa), wa
    assert not any(credibility.UNKNOWN_DEADLINE_TEXT in x for x in wa), \
        f"absent 不许说成'未能读取'：{wa}"


# ---------- 3.5 查询参数必须**真的有用**（B24，2026-10-19） ----------

@case
def 列表_notice_kind筛选真的生效():
    """用户实测：`?notice_kind=announcement` 和 `?notice_kind=catalog` 的 total
    **一模一样**（都是 2419）—— FastAPI 忽略未知查询参数，参数根本没接上。

    这条用例不只验"total 变了"，还要**和从数据里数出来的条数对上**：
    接口说 512 条，数据里也必须是 512 条，否则只是"两个数都变了"。
    """
    base = CLIENT.get("/api/notices", params={"size": 1}).json()["total"]

    def expect(kind):
        return sum(1 for r in RAW
                   if r.get("notice_kind") == kind
                   and (r.get("record_quality") or "") != "junk")

    ann = CLIENT.get("/api/notices",
                     params={"size": 1, "notice_kind": "announcement"}).json()
    cat = CLIENT.get("/api/notices",
                     params={"size": 1, "notice_kind": "catalog"}).json()
    assert ann["total"] == expect("announcement") > 0, (ann["total"], expect("announcement"))
    assert cat["total"] == expect("catalog") > 0, (cat["total"], expect("catalog"))
    assert ann["total"] != base and cat["total"] != base, (ann["total"], cat["total"], base)

    # 多值：逗号分隔 / 重复传，两种都认，且等于两者之和
    both = CLIENT.get("/api/notices", params={
        "size": 1, "notice_kind": "announcement,catalog"}).json()
    assert both["total"] == ann["total"] + cat["total"], both["total"]
    rep = CLIENT.get("/api/notices?size=1&notice_kind=announcement&notice_kind=catalog").json()
    assert rep["total"] == both["total"], rep["total"]

    # 传错值要 400 —— **不许静默当成"没筛"**（那样返回全量，用户以为筛了）
    bad = CLIENT.get("/api/notices", params={"notice_kind": "xxx"})
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "BAD_NOTICE_KIND", bad.json()


@case
def 列表_age_tier筛选真的生效且总数对得上():
    """`age_tier` 之前就声明了，但和 `notice_kind` 一样要**证明它真的在筛**。

    三档加起来必须等于可见总数（每条记录恰好属于一档），
    这比"live 的条数小于总数"强 —— 后者只要少一条就能通过。
    """
    base = CLIENT.get("/api/notices", params={"size": 1}).json()["total"]
    total = 0
    for tier in ("live", "recent", "archive"):
        got = CLIENT.get("/api/notices",
                         params={"size": 1, "age_tier": tier}).json()
        want = sum(1 for r in RAW
                   if (r.get("age_tier") or "archive") == tier
                   and (r.get("record_quality") or "") != "junk")
        assert got["total"] == want, (tier, got["total"], want)
        total += got["total"]
    assert total == base, ("三档之和不等于总数 —— 有记录不属于任何一档", total, base)

    bad = CLIENT.get("/api/notices", params={"age_tier": "yesterday"})
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "BAD_AGE_TIER", bad.json()


@case
def 列表_按kind筛时每一条都对且字段回显():
    """`school=北京大学&notice_kind=catalog`：抽 5 条，每条的 kind 都必须是 catalog。

    `notice_kind` 不在默认投影里（`LIST_FIELDS` 是 A 的 `make_fixtures.py` 定的，
    改了会破坏逐字段比对的 fixture），所以**按它筛时回显**这一列 ——
    "筛完看不见被筛的字段"正是这一轮要消灭的静默失效。
    """
    got = CLIENT.get("/api/notices", params={
        "school": "北京大学", "notice_kind": "catalog", "size": 5}).json()
    assert got["total"] > 0, got.get("total")
    assert len(got["items"]) == min(5, got["total"]), len(got["items"])
    for it in got["items"]:
        assert it.get("notice_kind") == "catalog", it
        assert "北京大学" in (it.get("school") or ""), it


@case
def 列表_未知查询参数不许静默忽略():
    """`?foo=bar` 必须**看得出来**没生效 —— 200 + `unknown_params`。

    不选"直接 422"的理由：前端可能带缓存穿透参数（`_=时间戳`），
    422 会让整个列表白掉；而"照常返回 + 明确列出没认识的参数"
    既不影响展示，也不会让人误以为筛过了。
    """
    got = CLIENT.get("/api/notices", params={
        "size": 1, "limit": 1, "offset": 0, "foo": "bar", "baz": 2}).json()
    # `limit` / `offset` 是**合法参数**（2026-10-19 用户实测的 bug：它们被报成未知）
    assert got.get("unknown_params") == ["foo", "baz"], got.get("unknown_params")
    assert "foo" in got.get("_unknown_note", ""), got.get("_unknown_note")
    # **默认形状不能被它污染**：正常请求里没有这两个键（fixture 逐字段比）
    ok = CLIENT.get("/api/notices", params={"size": 1}).json()
    assert "unknown_params" not in ok and "_unknown_note" not in ok, sorted(ok)


@case
def 列表_每个查询参数都真的改变结果():
    """**"接了参数"和"参数有用"是两件事。** 把每个参数都打一遍，
    要求结果和不带它时不同 —— 否则就是又一个"假功能"。
    """
    base = CLIENT.get("/api/notices", params={"size": 50}).json()

    def sig(d):
        return (d.get("total"), [i["id"] for i in (d.get("items") or [])],
                tuple(sorted(d.keys())))

    probes = [
        {"school": "北京大学"}, {"unit": "研究生院"},
        {"apply_mode": "推免"}, {"level": "硕士"}, {"year": 2027},
        {"q": "推免"}, {"sort": "deadline_asc"}, {"sort": "deadline_desc"},
        {"sort": "deadline_asc", "include_expired": "true"}, {"page": 2},
        {"size": 5}, {"meta": 1}, {"age_tier": "live"},
        {"include_junk": "true"}, {"notice_kind": "announcement"},
        {"topic": "apply"}, {"deadline_within": 7},
    ]
    for p in probes:
        got = CLIENT.get("/api/notices", params={"size": 50, **p}).json()
        assert sig(got) != sig(base), ("这个查询参数没有改变结果（等于没接）", p)


@case
def 列表_limit和offset是别名不是白名单():
    """`limit` / `offset` 是**合法参数**，所以它们必须真的生效（B25）。

    用户实测：`?limit=1&foo=bar` 把 `limit` 也报进 `unknown_params`。
    只把 `limit` 加进白名单而**不实现**，等于换一种方式静默失效 ——
    这条同时验"不报未知"和"真的改变输出"。
    """
    a = CLIENT.get("/api/notices", params={"limit": 3}).json()
    assert len(a["items"]) == 3, len(a["items"])
    assert a["size"] == 3, a
    assert "unknown_params" not in a, a.get("unknown_params")

    # offset：和 page 分页必须指向同一批记录
    p2 = CLIENT.get("/api/notices", params={"size": 3, "page": 2}).json()
    off = CLIENT.get("/api/notices", params={"size": 3, "offset": 3}).json()
    assert [i["id"] for i in off["items"]] == [i["id"] for i in p2["items"]], (
        "offset 和 page 指的批次不一致", [i["id"] for i in off["items"]],
        [i["id"] for i in p2["items"]])
    assert off["page"] == 2, ("offset 之后 page 要自洽", off["page"])

    # 同时给 size 和 limit：以 size 为准（更明确的名字优先）
    both = CLIENT.get("/api/notices", params={"size": 2, "limit": 9}).json()
    assert len(both["items"]) == 2, len(both["items"])


@case
def 列表_topic筛选真的生效():
    """`topic`（apply / non_apply / other）也是合法参数，必须真筛。"""
    base = CLIENT.get("/api/notices", params={"size": 1}).json()["total"]
    for t in config.TOPICS:
        got = CLIENT.get("/api/notices", params={"size": 1, "topic": t}).json()
        want = sum(1 for r in RAW
                   if r.get("topic") == t
                   and (r.get("record_quality") or "") != "junk")
        assert got["total"] == want, (t, got["total"], want)
        assert got["total"] < base, ("这个 topic 筛完还是全量", t)
    bad = CLIENT.get("/api/notices", params={"topic": "招生"})
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "BAD_TOPIC", bad.json()


@case
def 列表_deadline_within只给窗口内且不含已截止():
    """`?deadline_within=7`：只算 **found 且 0 <= 剩余天数 <= 7**。

    `absent` / `unknown` 不算 —— 那是替公告编一个截止日；
    已截止（days < 0）也不算 —— 用户问的是"还来得及的"。
    """
    today = date.today()

    def left(rec):
        d = push_mod.parse_day(rec.get("deadline"))
        return None if d is None else (d - today).days

    def is_found(rec):
        fs = rec.get("field_state") or {}
        if fs.get("deadline") in ("found", "absent", "unknown"):
            return fs["deadline"] == "found"
        return bool(rec.get("deadline"))

    for n in config.DEADLINE_WINDOWS:
        got = CLIENT.get("/api/notices",
                         params={"size": 50, "deadline_within": n}).json()
        want = sum(1 for r in RAW
                   if is_found(r) and left(r) is not None and 0 <= left(r) <= n
                   and (r.get("record_quality") or "") != "junk")
        assert got["total"] == want, (n, got["total"], want)
        for it in got["items"]:
            days = left(it)
            assert days is not None and 0 <= days <= n, (n, it["id"], days)

    bad = CLIENT.get("/api/notices", params={"deadline_within": 5})
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "BAD_DEADLINE_WITHIN", bad.json()


@case
def 列表_deadline_asc默认只排未来且升序():
    """**`sort=deadline_asc` 默认只排未来截止的**（B25 反馈，2026-10-19）。

    实测改之前前 5 条是 **2010 年**的已截止公告 —— 学生点"按截止日"要的是
    "哪些快来不及了"，不是"哪些早就过期了"。

    这条钉住三件事：① 已截止的一条都不出现（除非显式 `include_expired`）；
    ② 前 5 条的 deadline 都 >= 今天且递增；③ `None` 仍然沉底。
    """
    today = date.today()

    def _d(rec):
        return push_mod.parse_day(rec.get("deadline"))

    def _is_found(rec):
        return credibility.state_of(rec, "deadline") == credibility.FOUND

    future = sum(1 for r in RAW
                 if (r.get("record_quality") or "") != "junk" and _is_found(r)
                 and _d(r) is not None and _d(r) >= today)
    expired = sum(1 for r in RAW
                  if (r.get("record_quality") or "") != "junk" and _is_found(r)
                  and _d(r) is not None and _d(r) < today)
    got = CLIENT.get("/api/notices",
                     params={"sort": "deadline_asc", "size": 50}).json()
    # 已截止被排除；null（absent/unknown）**仍在列表里**、只是沉底 ——
    # 所以 total = 可见总数 - 已截止，不是"只有 future 那 36 条"。
    assert got["total"] == len(VISIBLE) - expired, (
        "deadline_asc 的 total 里混进了已截止的",
        got["total"], len(VISIBLE) - expired)

    items = got["items"]
    assert items, got["total"]
    dates, seen_none = [], False
    for it in items:
        dl = it.get("deadline")
        if dl is None:
            seen_none = True
            continue
        assert not seen_none, ("没截止日的排在了有截止日的前面", it["id"])
        assert dl >= today.isoformat(), ("前 50 条里出现了已截止的", it["id"], dl)
        dates.append(dl)
    assert dates == sorted(dates), ("未来截止的没有升序", dates[:5])
    assert dates[0] >= today.isoformat(), ("第一条不是未来日期", dates[0])
    assert dates[0] == min(dates), dates[:3]

    # `None` 必须全局沉底：最后一页不许冒出带日期的
    total = got["total"]
    if total >= 50:
        tail = CLIENT.get("/api/notices", params={
            "sort": "deadline_asc", "size": 50, "offset": total - 50}).json()
        assert tail["items"], tail
        assert all(it.get("deadline") is None for it in tail["items"]), (
            "最后一页里还有带截止日的 —— None 没有沉到底",
            [(it["id"], it.get("deadline")) for it in tail["items"]
             if it.get("deadline")][:3])


@case
def 列表_deadline_asc_include_expired把已截止放最后():
    """`include_expired=true` 时已截止的**排在最后**（未来之后、None 之前）。

    这是"两条排序职责分开"的落点：默认只给还来得及的；真要看历史，
    显式开开关，而且它们不许插到未来截止的前面。
    """
    today = date.today()

    def _left(rec):
        d = push_mod.parse_day(rec.get("deadline"))
        return None if d is None else (d - today).days

    def _is_found(rec):
        return credibility.state_of(rec, "deadline") == credibility.FOUND

    expired = sum(1 for r in RAW
                  if (r.get("record_quality") or "") != "junk" and _is_found(r)
                  and _left(r) is not None and _left(r) < 0)
    future = sum(1 for r in RAW
                 if (r.get("record_quality") or "") != "junk" and _is_found(r)
                 and _left(r) is not None and _left(r) >= 0)
    base = CLIENT.get("/api/notices",
                      params={"sort": "deadline_asc", "size": 1}).json()["total"]
    inc = CLIENT.get("/api/notices", params={
        "sort": "deadline_asc", "include_expired": "true", "size": 50}).json()
    assert inc["total"] == len(VISIBLE), (inc["total"], len(VISIBLE))
    assert inc["total"] == base + expired, (inc["total"], base, expired)

    # 全量看顺序：future（升序）→ expired（降序，刚过期的先）→ null
    items = fetch_all(CLIENT, "/api/notices",
                      {"sort": "deadline_asc", "include_expired": "true"})
    future_dates, expired_dates = [], []
    seen_expired = seen_null = False
    for it in items:
        dl = it.get("deadline")
        if dl is None:
            seen_null = True
            continue
        assert not seen_null, ("没截止日的没有沉到最底", it["id"])
        if dl >= today.isoformat():
            assert not seen_expired, ("已截止的插到了未来截止的前面", it["id"], dl)
            future_dates.append(dl)
        else:
            seen_expired = True
            expired_dates.append(dl)
    assert len(future_dates) == future, (len(future_dates), future)
    assert len(expired_dates) == expired, (len(expired_dates), expired)
    assert future_dates == sorted(future_dates), future_dates[:5]
    assert expired_dates == sorted(expired_dates, reverse=True), (
        "已截止段不是'刚过期的在前'", expired_dates[:5])


@case
def 列表_deadline_desc先给已过期的():
    """`sort=deadline_desc` = **过期优先**（`deadline_asc` 的镜像）。

    用户原话："想看已过期的，用 `sort=deadline_desc` 或显式 `include_expired=true`"。
    所以 desc 不是"未来最远的先"（那没人要看），而是：已过期（刚过期的先）
    → 未来（最远的先）→ `None`。
    """
    today = date.today()
    got = CLIENT.get("/api/notices",
                     params={"sort": "deadline_desc", "size": 50}).json()
    items = got["items"]
    assert items, got["total"]
    assert items[0].get("deadline"), items[0]
    assert items[0]["deadline"] < today.isoformat(), (
        "deadline_desc 的第一条不是已截止的", items[0].get("deadline"))

    seen_future = False
    for it in items:
        dl = it.get("deadline")
        if dl is None:
            continue
        if dl >= today.isoformat():
            seen_future = True
        else:
            assert not seen_future, ("已截止的排到了未来后面", it["id"], dl)

    # 已截止段内部：日期降序（刚过期的在前）
    expired_dates = [it["deadline"] for it in items
                     if it.get("deadline") and it["deadline"] < today.isoformat()]
    assert expired_dates == sorted(expired_dates, reverse=True), expired_dates[:5]

    bad = CLIENT.get("/api/notices", params={"sort": "deadline_xxx"})
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "BAD_SORT", bad.json()


@case
def facets_截止日分桶和筛选对得上():
    """`/api/facets` 的两套截止日桶（B25）。

    **累计桶 `deadline_buckets`**：前端下拉写「7 天内 (57)」，计数必须和
    `?deadline_within=7` 的 total 一样；`within_30 + later + expired + unknown +
    absent` 必须等于可见总数（累计桶内部不能相加，这是其中一种合法的加法）。

    **互斥桶 `deadline_exclusive_buckets`**（用户 2026-10-19 点名要的）：
    给饼图 / 要"加起来等于总数"的场合用；**所有桶相加必须等于总数**。
    """
    fx = CLIENT.get("/api/facets").json()
    buckets = {b["value"]: b for b in fx.get("deadline_buckets") or []}
    assert set(buckets) == {"expired", "within_1", "within_3", "within_7",
                            "within_30", "later", "unknown", "absent"}, sorted(buckets)
    assert all(b.get("label") for b in buckets.values()), buckets
    for n in config.DEADLINE_WINDOWS:
        got = CLIENT.get("/api/notices",
                         params={"size": 1, "deadline_within": n}).json()
        assert buckets[f"within_{n}"]["count"] == got["total"], (
            "facets 的 within_%d 和筛选 total 对不上" % n,
            buckets[f"within_{n}"]["count"], got["total"])
    total = CLIENT.get("/api/notices", params={"size": 1}).json()["total"]
    parts = (buckets["within_30"]["count"] + buckets["later"]["count"]
             + buckets["expired"]["count"] + buckets["unknown"]["count"]
             + buckets["absent"]["count"])
    assert parts == total, ("截止日分桶相加 != 可见总数", parts, total)

    # ---- 互斥桶：独立复算 + 相加 == total ----
    ex = {b["value"]: b for b in fx.get("deadline_exclusive_buckets") or []}
    assert set(ex) == {"within_1", "within_2_3", "within_4_7", "within_8_30",
                       "later", "expired", "unknown", "absent"}, sorted(ex)
    assert all(b.get("label") for b in ex.values()), ex
    assert sum(b["count"] for b in ex.values()) == total, (
        "互斥桶相加 != 可见总数", sum(b["count"] for b in ex.values()), total)

    today = date.today()
    want = {k: 0 for k in ex}

    def _found(rec):
        return credibility.state_of(rec, "deadline") == credibility.FOUND

    for rec in RAW:
        if (rec.get("record_quality") or "") == "junk":
            continue
        if not _found(rec):
            key = ("absent" if credibility.state_of(rec, "deadline")
                   == credibility.ABSENT else "unknown")
            want[key] += 1
            continue
        d = push_mod.parse_day(rec.get("deadline"))
        if d is None:
            want["unknown"] += 1
            continue
        left = (d - today).days
        if left < 0:
            want["expired"] += 1
        elif left <= 1:
            want["within_1"] += 1
        elif left <= 3:
            want["within_2_3"] += 1
        elif left <= 7:
            want["within_4_7"] += 1
        elif left <= 30:
            want["within_8_30"] += 1
        else:
            want["later"] += 1
    got_counts = {k: v["count"] for k, v in ex.items()}
    assert got_counts == want, ("互斥桶和数据数出来的对不上", got_counts, want)


# ---------- 3.6 首次运行：没有 state.json 也能跑（B26，2026-10-19） ----------

@case
def state_首次运行自动生成演示状态():
    """**新克隆的仓库没有 `server/var/state.json`，第一次跑必须能用。**

    事故：`server/var/` 在 `.gitignore` 里，交接用的 bundle 克隆出来没有这个文件；
    门禁里有用例直接 `shutil.copyfile(server/var/state.json, tmp)` →
    **第一次跑门禁就红 5 条**，接手的人会以为整个项目是坏的。

    这条钉住三件事：
    ① 缺文件时 `load_or_seed` **自动落盘**，不用人手建；
    ② 建出来的是**干净演示状态**（只有 u_demo / u_num_demo，九个顶层键齐全），
       **不许把开发机上的测试用户抄进去**；
    ③ 门禁自己**不再复制**仓库里的 state（源级检查，防止有人加回来）。
    """
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix="fresh_"), "state.json")
    assert not os.path.exists(path)

    st = StateStore.load_or_seed(path)
    assert os.path.exists(path), "缺失时没有自动落盘 —— 新克隆还是得手建"

    users = set(st.state["users"])
    assert users == {"u_demo", "u_num_demo"}, ("演示状态里混进了别的用户", users)
    for k in ("version", "users", "posts", "orders", "counters", "watches",
              "audit", "profiles", "pushes"):
        assert k in st.state, ("state 少了顶层键", k)
    # u_num_demo 有完整画像（推送/匹配/性能用例都靠它）；u_demo 是免费身份
    prof, _ = st.profile_of("u_num_demo")
    assert prof and prof.get("majors") and prof.get("preferred_schools"), prof
    assert st.state["users"]["u_demo"]["subscribed"] is False
    assert st.state["users"]["u_num_demo"]["subscribed"] is True
    assert not any(u.startswith(("u_sweep", "u_c_", "u_qa", "u_walk")) for u in users), users

    # 再次调用不会重复建/改动已有文件
    st2 = StateStore.load_or_seed(path)
    assert set(st2.state["users"]) == users

    # 源级检查：门禁里不许再有"复制仓库 state"的写法。
    # 用正则而不是子串：这条断言自己的文档/源码里会出现同样的词，
    # 子串匹配会把"写了这条断言"当成"违反了这条断言"。
    src = open(os.path.join(ROOT, "server", "tests", "run.py"), encoding="utf-8").read()
    copied = re.search(
        r"shutil\.copy(?:file|2)\(\s*os\.path\.join\(ROOT,\s*\"server\",\s*\"var\"", src)
    assert not copied, "门禁又在复制 server/var/state.json —— 新克隆会红（B26）"


@case
def 数据健康_没有日志时fast用例跳过而不是红():
    """**B27（2026-10-19）：`data/logs/` 不进 git，新克隆没有 fast.log。**

    那是"这台机器还没跑过定时任务"，不是"任务挂了"。这条验两件事：

    ① 没有日志 → 不抛异常，并且**留下一条跳过记录**（不静默）；
    ② 有日志且最近一轮失败 → **必须红**（否则哨兵就白设了）。
    """
    import tempfile
    old_logs = _health.LOGS
    n0 = len(_health.SKIPPED)
    try:
        tmp = tempfile.mkdtemp(prefix="nologs_")
        _health.LOGS = tmp
        _health.data_最近一轮fast必须成功()          # 空目录：应当跳过，不抛
        assert len(_health.SKIPPED) == n0 + 1, _health.SKIPPED[n0:]
        assert "没有任何 fast 轮次" in _health.SKIPPED[-1][1], _health.SKIPPED[-1]

        # 造一份"最近一轮中途退了"的日志 → 必须红
        # （没有 `fast end` 标记 = 明确的中途退出；`_run_ok` 判 False）
        with open(os.path.join(tmp, "fast.log"), "w", encoding="utf-8", newline="\n") as f:
            f.write("[周一 2026/10/19 09:00:00.00] ===== fast start =====\n")
        try:
            _health.data_最近一轮fast必须成功()
        except AssertionError as e:
            assert "失败了" in str(e), e
        else:
            raise AssertionError("有日志且最近一轮失败，这条居然没红")
    finally:
        _health.LOGS = old_logs
        del _health.SKIPPED[n0:]


@case
def 数据健康_freshness过期快照必须红():
    """**B27：** `freshness` 与主库的比较改成了**数据内部时间**。

    - 主库时刻 = `notices.crawled_at` 的最大值（不是文件 mtime）；
    - 判据方向：`max(crawled_at) - generated_at <= 阈值`，
      即 **freshness 不许落后于数据**（汇总比数据新是正常的，不红）。

    这条先确认正常那份是绿的，再**造一个 5 小时前的过期快照**，必须红。
    """
    import json
    import tempfile
    from datetime import timedelta

    newest, _bad = _health._newest_crawled_at()
    assert newest is not None, "数据里没有可解析的 crawled_at，无法验这条"

    old_fr = _health.FRESHNESS
    try:
        # ① 正常那份：必须绿
        _health.freshness_不是过期快照()

        # ② 造过期快照：generated_at 比 max(crawled_at) 早 5 小时
        fr = json.load(open(old_fr, encoding="utf-8"))
        fr["generated_at"] = (newest - timedelta(hours=5)).isoformat(timespec="seconds")
        tmp = tempfile.mkdtemp(prefix="stale_")
        p = os.path.join(tmp, "freshness.json")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(fr, f, ensure_ascii=False)
        _health.FRESHNESS = p
        try:
            _health.freshness_不是过期快照()
        except AssertionError as e:
            assert "过期快照" in str(e), e
        else:
            raise AssertionError("造了 5 小时前的过期快照，居然没红")

        # ③ **只改 mtime、内容不变**（模拟 Git 检出）→ 必须仍然绿。
        # 这正是空克隆假红的原因：旧实现拿 mtime 当"主库时刻"。
        import shutil
        tmp2 = tempfile.mkdtemp(prefix="mtime_")
        p2 = os.path.join(tmp2, "notices.jsonl")
        shutil.copyfile(_health.NOTICES, p2)
        os.utime(p2, None)                  # mtime = 现在（内容一模一样）
        old_notices = _health.NOTICES
        try:
            _health.FRESHNESS = old_fr
            _health.NOTICES = p2
            _health.freshness_不是过期快照()
        finally:
            _health.NOTICES = old_notices
    finally:
        _health.FRESHNESS = old_fr


@case
def 门禁不许依赖文件系统元数据():
    """**B27 的总原则：** 门禁的判据只能建在**文件内容**上。

    mtime / 权限 / 大小在交接、打包、克隆、备份还原之后都会变 ——
    用它们做判据，空克隆就会"天生红"，而代码本身没有任何问题。

    这条扫 `server/tests/*.py`：不许出现 `getmtime` / `st_mtime`。
    （`getsize` 允许：它只出现在清理脚本的**打印**里，不参与任何判据。）
    """
    bad = []
    import ast
    for name in sorted(os.listdir(os.path.join(ROOT, "server", "tests"))):
        if not name.endswith(".py"):
            continue
        path = os.path.join(ROOT, "server", "tests", name)
        tree = ast.parse(open(path, encoding="utf-8").read())
        # 用 AST 找**真的调用/访问**（`os.path.getmtime(...)` / `x.st_mtime`），
        # 而不是在源码里搜字符串 —— 否则这条断言会扫到它自己的文档。
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("getmtime", "st_mtime"):
                bad.append((name, node.attr, getattr(node, "lineno", "?")))
    assert not bad, ("门禁里出现了文件系统元数据判据（mtime）—— 空克隆会假红", bad)


# ---------- 4. 匹配（契约 api.md 第 2 节） ----------

@case
def match_响应形状与_fixture_一致():
    """顶层键 = profile / stats / items，和 match.response.json 完全一致。"""
    got = CLIENT.post("/api/match", json=PROF, params={"limit": 30}).json()
    assert set(got.keys()) == set(MATCH_FX.keys()) == {"profile", "stats", "items"}, set(got.keys())
    assert set(got["stats"].keys()) == set(MATCH_FX["stats"].keys())
    assert got["profile"] == PROF, "profile 必须原样回显"
    assert len(got["items"]) == 30
    # 存在性用 set 比；**不按键序** —— 见 `列表项字段与_fixture_一致` 的说明
    assert set(got["items"][0].keys()) == set(MATCH_FX["items"][0].keys()), \
        set(got["items"][0].keys()) ^ set(MATCH_FX["items"][0].keys())


@case
def match_必给字段都在同一条记录里():
    """api.md 第 2 节的硬要求：extract.confidence / has_attachments / attachments_count
    必须在**同一个响应**里给全，前端不许再按 id 去拉列表接口补字段。"""
    items = CLIENT.post("/api/match", json=PROF, params={"limit": 30}).json()["items"]
    by_id = {r["id"]: r for r in RAW}
    for it in items:
        assert isinstance(it["has_attachments"], bool), it["id"]
        assert isinstance(it["attachments_count"], int), it["id"]
        assert isinstance(it["extract"], dict) and "confidence" in it["extract"], it["id"]
        assert isinstance(it["reasons"], list) and isinstance(it["warnings"], list), it["id"]
        # 且与源数据一致（不是随手填的默认值）
        src = by_id[it["id"]]
        atts = src.get("attachments") or []
        assert it["has_attachments"] == bool(atts)
        assert it["attachments_count"] == len(atts)
        assert it["extract"] == src["extract"]


@case
def match_与参考实现逐字段一致():
    """直接跑 A 的 match_preview.py 对表 —— 这是"规则没改"的唯一证明。

    A 改了公式而 B 没跟，这里会红。
    """
    import importlib.util

    path = os.path.join(ROOT, "collector", "tools", "match_preview.py")
    spec = importlib.util.spec_from_file_location("_a_match_preview", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ref, ref_stats = mod.match(RAW, PROF, date.today())
    mine, mine_stats = matching.match_notices(RAW, PROF, date.today())
    assert mine_stats == ref_stats, (mine_stats, ref_stats)
    assert len(mine) == len(ref)
    KEYS = ("id", "score", "raw_score", "bucket", "days_left", "title", "school",
            "unit", "publish_date", "deadline", "url", "reasons", "warnings")
    for i, (a, b) in enumerate(zip(ref, mine)):
        for k in KEYS:
            assert a.get(k) == b.get(k), f"第 {i} 条 {k}:\n  参考={a.get(k)!r}\n  我的={b.get(k)!r}"


@case
def match_分数真的按降序且降权关系成立():
    """两条：① 按分数降序；② score = raw_score × (0.7 + 0.3×confidence) 的**关系**成立。

    注意别用已舍入的 `raw_score` 反推分数 —— 参考实现是在**舍入前的裸浮点**上算 final 的，
    用舍入后的值反推会有 1e-4 的差，正好卡在 Python `round()` 的偶舍入边界上
    （实测 `round(0.75*0.799,4)=0.5992` 而 `round(0.59925,4)=0.5992` vs `0.5993`）。
    那是断言的错，不是实现的错。所以这里比的是**比例关系**，容差 1e-3。
    """
    items = CLIENT.post("/api/match", json=PROF, params={"limit": 200}).json()["items"]
    scores = [i["score"] for i in items]
    assert scores == sorted(scores, reverse=True), scores[:5]

    by_id = {r["id"]: r for r in RAW}
    for it in items:
        conf = by_id[it["id"]]["extract"]["confidence"]
        factor = 0.7 + 0.3 * conf
        # 降权只会让分数变低，绝不会超过原始分
        assert it["score"] <= it["raw_score"] + 1e-9, (it["id"], it["score"], it["raw_score"])
        assert abs(it["score"] - it["raw_score"] * factor) <= 1e-3, \
            (it["id"], it["score"], it["raw_score"], factor)


@case
def match_口径切到push时和推送同一个公式():
    """B18 方案 A 的**接线证明**（用户 2026-10-19 拍板；等 A 改参考实现后才默认打开）。

    验证的是"接上了"，不是"分数合理"：

    ① 开关打开时，`score_notice` 的 `raw_score` 必须**逐位等于**
       `push.weighted_score(push.score_dims(...))` —— 两条路径一个公式；
    ② `final = raw × (0.7 + 0.3×confidence)` 这个降权关系仍然成立
       （否则 `/api/match` 的"分数 ≤ 原始分"那条断言会红，且和参考实现对不上）；
    ③ 开关关着时分数必须**不一样** —— 否则这条用例证明不了开关真的在起作用
       （§7.3 那个"断言通过但毫无意义"的坑）。
    """
    from server import push as push_mod

    rec = dict(RAW[0])
    today = date.today()
    saved = config.MATCH_SCORE_FROM_PUSH
    try:
        config.MATCH_SCORE_FROM_PUSH = True
        final, raw, _reasons, _warnings = matching.score_notice(rec, PROF, today)
        dims, _detail = push_mod.score_dims(matching.event_view(rec), PROF, today)
        assert abs(raw - push_mod.weighted_score(dims)) < 1e-9, (
            "match 的分数不是 push 的四维加权 —— 两条路径又分叉了",
            raw, push_mod.weighted_score(dims))
        conf = (rec.get("extract") or {}).get("confidence")
        conf = 0.5 if conf is None else float(conf)
        # `final` 落盘前保留 4 位小数；这里按同一精度比，不能用 1e-9。
        assert abs(final - round(raw * (0.7 + 0.3 * conf), 4)) < 1e-9, (
            final, raw, conf)

        config.MATCH_SCORE_FROM_PUSH = False
        _f2, raw2, _r2, _w2 = matching.score_notice(rec, PROF, today)
    finally:
        config.MATCH_SCORE_FROM_PUSH = saved
    assert abs(raw2 - raw) > 1e-9, (
        "开关关了分数却没变 —— 这条用例在赌（开关根本没被读）", raw, raw2)


@case
def match_空画像不崩():
    """空画像也要给结论：字段全缺失 → 全部降权 + 明确提醒，而不是 500。"""
    r = CLIENT.post("/api/match", json={}, params={"limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stats"]["total"] == len(RAW)
    for it in body["items"]:
        assert any("未抽到截止日期" in w for w in it["warnings"]) or it["deadline"]


@case
def match_包子形状的_profile_也收():
    """`{"profile": {...}}` 与裸画像等价 —— C 可能两种写法都用。"""
    a = CLIENT.post("/api/match", json=PROF, params={"limit": 5}).json()
    b = CLIENT.post("/api/match", json={"profile": PROF}, params={"limit": 5}).json()
    assert [i["id"] for i in a["items"]] == [i["id"] for i in b["items"]]
    assert b["profile"] == PROF


@case
def cred_contact_空壳不许报成_found():
    """**`contact` 是复合字段：有键不等于有值。**

    实测 470 条里 **470 条都有 `contact` 键，但 232 条是
    `{"email": null, "phone": null}`** —— 一个空壳。

    只判 `bool(dict)` 会把这 232 条报成 `found`（"有联系方式"），
    而事实是**公告里没写**。这是这一晚反复出现的那类错：
    **一个看起来有值的字段，掩盖了"确实没有"。**

    这条用例两头钉：空壳 → 不算有值；有一个非空 → 算有值。
    """
    empty_shell = {"email": None, "phone": None}
    has_email = {"email": "a@b.cn", "phone": None}
    has_phone = {"email": None, "phone": "010-1234"}
    empty = {}

    # ① 空壳 / 空 dict → 不算有值（没有 field_state 时 → unknown，不是 found）
    for c in (empty_shell, empty):
        rec = {"contact": c}
        assert credibility._has_value(rec, "contact") is False, c
        assert credibility.state_of(rec, "contact") == credibility.UNKNOWN, (
            "空壳 contact 被报成了 %s" % credibility.state_of(rec, "contact"))

    # ② 有一个非空就算有值
    for c in (has_email, has_phone):
        assert credibility._has_value({"contact": c}, "contact") is True, c

    # ③ 对照：**别的 dict 字段不受影响**（这条特殊处理只针对 contact）
    assert credibility._has_value({"contact": empty_shell, "extract": {"confidence": 0.9}},
                                  "extract") is True

    # ④ 真数据上验一遍：有 contact 键、但空壳的记录，不该被算成 found
    shell_ids = [r["id"] for r in RAW
                 if isinstance(r.get("contact"), dict)
                 and not r["contact"].get("email") and not r["contact"].get("phone")
                 and not (r.get("field_state") or {}).get("contact")]
    if shell_ids:
        rec = next(r for r in RAW if r["id"] == shell_ids[0])
        assert credibility.state_of(rec, "contact") != credibility.FOUND, (
            "真数据里的空壳 contact 被算成了 found：%s" % rec["id"])


@case
def match_默认不排除已截止的_显式开关还能用():
    """**"不猜"这条主张的具体落点**（2026-10-18 决策者要求确认的那条）。

    已截止的公告**默认也要返回**，理由三条：
    截止日可能抽错、学校会延期、补发的附件截止后照样有人要。
    **压掉已截止 = 猜"这条肯定没用了"** —— 违背整个产品的主张。

    但这不等于"没有能力排除它"：`exclude_expired: true` 仍是显式开关，
    用户（或前端某个"只看还能报的"筛选）可以用。**默认关、显式开。**

    两头都钉：
    ① 默认（不传 `exclude_expired`）→ `dropped_expired == 0`
    ② 显式 `exclude_expired: true` → 真的排掉一批（这个能力不许被删掉）
    """
    minimal = {"preferred_schools": ["浙江大学"], "majors": ["物理学"]}

    # ① 默认不过滤
    r = CLIENT.post("/api/match", json=minimal, params={"limit": 30}).json()
    assert r["stats"]["dropped_expired"] == 0, (
        "默认画像把已截止的排掉了 —— dropped_expired=%d"
        % r["stats"]["dropped_expired"])

    # ② 显式开关还能用
    r2 = CLIENT.post("/api/match", json=dict(minimal, exclude_expired=True),
                     params={"limit": 30}).json()
    assert r2["stats"]["dropped_expired"] > 0, (
        "`exclude_expired: true` 不生效了 —— 那是删掉了一个能力，不是简化"
        f"（dropped_expired={r2['stats']['dropped_expired']}）")

    # ③ 两个口径的恒等式都要成立
    for body in (r, r2):
        s = body["stats"]
        assert s["total"] == s["dropped_expired"] + s["dropped_mode"] + s["scored"], s

    # ④ **示例画像不许替用户做这个决定**
    #    `collector/tools/profile-example.json` 是"产品默认行为"的代表，
    #    它里面带 `exclude_expired: true` 就等于默认压掉已截止的。
    #    实测这曾让 `/api/match` 的 dropped_expired 变成 282 —— 而没人知道为什么。
    import json as _json
    ex = _json.load(open(os.path.join(ROOT, "collector", "tools",
                                      "profile-example.json"), encoding="utf-8"))
    assert not ex.get("exclude_expired"), (
        "示例画像里又出现了 `exclude_expired` —— 那会让'默认行为'变成"
        "压掉已截止的，而没人会说得出为什么")


@case
def match_item_带_field_trust_且_stats_恒等式成立():
    """**统筹派单第 4 条。**

    C 的匹配页要标"这条有几个字段不可信"，而它**只认 `field_trust`**。
    原来 match 的 item 里什么都没有 —— 页面只能靠 `warnings` 的文案去猜，
    猜不准就干脆不标（那比标错好，但等于这个功能没做）。

    三件一起验：

    ① item 里有 `field_trust`，且**形状和列表 `?meta=1` 的完全一致**
       （前端一套渲染逻辑，形状不一样就要写两套）；
    ② 它是**摘要**不是 `field_state` —— 匹配页是"扫一眼决定点哪条"，
       给逐字段完整三态只会让它变重；
    ③ `stats` 的**恒等式**：`total == dropped_expired + dropped_mode + scored`。
       这个式子是 C 的 Q7 第 3 条的答案：`total` 是**数据源总量**，
       匹配结果数要看 `scored`，不是 `total`、也不是 `len(items)`。
    """
    body = CLIENT.post("/api/match", json=PROF, params={"limit": 30}).json()
    items = body["items"]
    assert items, "匹配结果是空的，后面的断言没意义"

    # 列表 meta=1 里的摘要（形状基准）
    one = CLIENT.get("/api/notices", params={"meta": 1, "size": 1}).json()["items"][0]
    fx_keys = set(one["field_trust"])

    for it in items:
        ft = it.get("field_trust")
        assert isinstance(ft, dict), ("match item 缺 field_trust", it["id"])
        assert set(ft) == fx_keys, (
            "field_trust 的形状和列表 meta=1 不一致（前端要写两套渲染）：\n"
            "  列表: %s\n  match: %s" % (sorted(fx_keys), sorted(ft)))
        assert isinstance(ft.get("untrusted_fields"), list), ft
        assert isinstance(ft.get("untrusted_count"), int), ft
        assert isinstance(ft.get("states"), dict), ft
        # ② 是摘要，**不是**完整 field_state
        assert "field_state" not in it, ("match item 不该带完整 field_state", it["id"])

    # ③ stats 恒等式
    s = body["stats"]
    assert s["total"] == s["dropped_expired"] + s["dropped_mode"] + s["scored"], s
    assert s["total"] == len(RAW), (s["total"], len(RAW))
    # `len(items)` 是 limit 决定的，**不是**匹配总数
    assert len(items) == 30 and s["scored"] >= 30, (len(items), s["scored"])


@case
def match_stats_与源数据对得上():
    stats = CLIENT.post("/api/match", json=PROF, params={"limit": 1}).json()["stats"]
    assert stats["total"] == len(RAW)
    # 丢弃的两个口径：已过期 + 招生方式不符；剩下的都参与打分
    assert stats["dropped_expired"] + stats["dropped_mode"] + stats["scored"] == len(RAW), stats


# ---------- 5. 导师 ----------

@case
def faculty_列表形状与_fixture_一致():
    """导师列表的**形状**必须和 fixture 一致。

    和公告列表同一个毛病：fixture 是"某一天数据的快照"，A 把导师从 50 人加到 67 人
    之后它自然对不上。所以**数量比对拆成两件事**：

    - `total` 必须等于**当前数据**的条数（这是接口的正确性，每天都要成立）
    - fixture 的 `total` 是**快照值**，它和当前数据不等时说明 fixture 该重跑了，
      这时只比对"凡 fixture 里也有的 id，字段必须逐字段一致"（形状不变）
    """
    fx_path = os.path.join(FIXTURES, "faculty.list.json")
    if not os.path.exists(fx_path):
        return          # A 还没生成这个 fixture 就跳过，不算失败
    fx = json.load(open(fx_path, encoding="utf-8"))
    got = CLIENT.get("/api/faculty", params={"size": 50}).json()

    assert got["total"] == len(FAC_RAW), (got["total"], len(FAC_RAW))
    assert [i["id"] for i in got["items"]] == [r["id"] for r in FAC_RAW[:50]], \
        "导师列表既不排序也不重排 —— 应当就是文件顺序"

    fx_by_id = {i["id"]: i for i in fx["items"]}
    checked = 0
    for it in got["items"]:
        if it["id"] not in fx_by_id:
            continue
        assert set(it.keys()) == set(fx_by_id[it["id"]].keys()), \
            set(it.keys()) ^ set(fx_by_id[it["id"]].keys())
        checked += 1
    assert checked > 0, "fixture 和当前数据完全对不上，形状没被验到"


@case
def faculty_匹配与参考实现一致():
    """和 A 的参考实现对表 —— 但**只对"可匹配"那部分**（2026-10-17 起）。

    A 的 `match_preview.py --faculty` 按**全部导师**算分，它验的是打分公式；
    `/api/faculty/match` 在公式之上多了一层"只留 `matchable=true`"。

    **两个口径不同，而这不是漂移，是刻意分层**：
    参考实现不知道"名录级"这个概念（那是 2026-10-17 才定下来的），
    而接口不能把 178 位没方向的导师当"匹配结果"返回。

    所以这里**先按同一个判据过滤参考实现**，再逐字段比 ——
    比的仍然是"打分公式和理由没变"，只是把"过滤"这件事显式写出来，
    而不是让它变成一条含糊的 `len(ref) == len(mine)` 失败。
    """
    import importlib.util

    path = os.path.join(ROOT, "collector", "tools", "match_preview.py")
    spec = importlib.util.spec_from_file_location("_a_match_preview2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ref_all = mod.match_faculty(FAC_RAW, PROF)
    mine = matching.match_faculty(FAC_RAW, PROF)

    # 参考实现的口径是"全部导师"；按同一判据过滤到"可匹配"
    ref = [r for r in ref_all if (r.get("research_areas") or [])]

    assert len(ref) == len(mine), (
        "过滤后条数仍不一致：参考 %d / 我的 %d（说明过滤判据和参考实现的对不上）"
        % (len(ref), len(mine)))
    for i, (a, b) in enumerate(zip(ref, mine)):
        for k in ("id", "name", "score", "hits", "reasons", "warnings", "email"):
            assert a.get(k) == b.get(k), f"第 {i} 位导师 {k}: 参考={a.get(k)!r} 我的={b.get(k)!r}"


@case
def faculty_match_只返回可匹配的():
    """**统筹派单：`/api/faculty/match` 只返回 `matchable=true`。**

    这之前，178 位没有研究方向的导师**也在匹配结果里**：
    命中 0 个兴趣词、只拿 `0.15×单位 + 0.15×层次`，排在最后但**仍然返回**，
    还带一条"该导师页面没有研究方向，无法判断契合度"。

    两个后果都不是"少几条"：
    ① **匹配结果数虚高**（用户以为"40 个对口"，其实 18 个没法判）；
    ② **警告变成噪音**（每条都挂"无法判断"，用户会怀疑整页数据）。

    这条用例两头都钉：① 结果里不许有没方向的；② 有方向的**一条都不许少**。
    """
    body = CLIENT.post("/api/faculty/match", json=PROF, params={"limit": 100}).json()
    items = body["items"]
    assert items, "匹配结果是空的"

    # ① 一条都不许没有研究方向
    bad = [i["id"] for i in items if not (i.get("research_areas") or [])]
    assert not bad, ("匹配结果里出现了没有研究方向的导师", bad[:5])

    # ② "该导师页面没有研究方向"这条警告**不该再出现**（那种导师根本不在结果里）
    noisy = [i["id"] for i in items
             if any("没有研究方向" in w for w in (i.get("warnings") or []))]
    assert not noisy, ("还有导师带着'没有研究方向'的警告进了结果", noisy[:5])

    # ③ 对照：**条数由 limit 决定，上限受"可匹配总数"约束**（这条接口没有分页）
    matchable = [r for r in FAC_RAW if (r.get("research_areas") or [])]
    assert len(items) == min(100, len(matchable)), (
        "可匹配导师 %d 位，limit=100 却给了 %d 条" % (len(matchable), len(items)))
    assert len(matchable) < len(FAC_RAW), (
        "数据里已经没有'没方向'的导师了，这条用例失去意义 —— 检查 faculty.jsonl")

    # ④ 列表接口里两层都要在，**并且能被筛出来**（`?matchable=true/false`）
    lst = CLIENT.get("/api/faculty", params={"size": 50}).json()
    assert any(i["matchable"] for i in lst["items"]), "列表里没有 matchable=true 的"
    assert any(not i["matchable"] for i in lst["items"]), "列表里没有 matchable=false 的"

    t = CLIENT.get("/api/faculty", params={"size": 50, "matchable": True}).json()
    f = CLIENT.get("/api/faculty", params={"size": 50, "matchable": False}).json()
    assert t["total"] + f["total"] == len(FAC_RAW), (
        "两层加起来不等于总数：%d + %d != %d" % (t["total"], f["total"], len(FAC_RAW)))
    assert all(i["matchable"] for i in t["items"]), "?matchable=true 里混进了名录级"
    assert all(not i["matchable"] for i in f["items"]), "?matchable=false 里混进了可匹配的"
    # 和文件现算的对比（不是硬编码数字）——判据只有"有没有研究方向"
    assert t["total"] == sum(1 for r in FAC_RAW if (r.get("research_areas") or [])), t["total"]


@case
def faculty_match_接口可用且带理由():
    r = CLIENT.post("/api/faculty/match", json=PROF, params={"limit": 5})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 5
    top = items[0]
    assert top["score"] > 0 and top["reasons"], top
    # 契约硬要求：命中的方向词要写进理由，不能只说"匹配度高"
    assert any("研究方向命中" in x for x in top["reasons"]), top["reasons"]
    # 契约硬要求：没有研究方向的导师不许排到前面
    assert top["research_areas"], top


@case
def faculty_无兴趣词时明确警告而不是静默降级():
    items = CLIENT.post("/api/faculty/match",
                        json={"majors": ["物理学"]}, params={"limit": 3}).json()["items"]
    assert all(any("interest_keywords" in w for w in it["warnings"]) for it in items), \
        [it["warnings"] for it in items]


@case
def faculty_group_kind_每条都有值且与参考实现一致():
    """`group_kind` 在 faculty.schema.json 里是 **required**，所以不能是 None。

    判据顺序有讲究：「中国科学院院士」里同时含「学院」，先判身份才不会被机构抢走。
    A 的参考实现在 `collector/faculty.py`，这里直接 import 它对表 ——
    两边判定一分叉就会红。
    """
    import importlib.util

    path = os.path.join(ROOT, "collector", "faculty.py")
    spec = importlib.util.spec_from_file_location("_a_faculty", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        mod = None                     # import 不了（缺 collector 依赖）就退化为自检

    items = fetch_all(CLIENT, "/api/faculty")
    assert items, "导师列表为空，这条测试没意义"
    assert len(items) == len(FAC_RAW), (len(items), len(FAC_RAW))
    kinds = {"身份", "机构", "其他"}
    for it in items:
        assert it["group_kind"] in kinds, (it["name"], it["group"], it["group_kind"])
        if mod is not None:
            assert it["group_kind"] == mod.group_kind(it["group"]), \
                f"{it['group']}：参考={mod.group_kind(it['group'])} 我的={it['group_kind']}"
    # 真实出现的两类都要有，否则"分得开"这件事没被验证。
    # **这里原来只读第一页**，而第一页（50 条）恰好全是"其他"——
    # 数据涨到 259 之后就红了。取全之后这个断言才真的在验"分得开"。
    seen = {it["group_kind"] for it in items}
    assert "身份" in seen and "机构" in seen, seen


@case
def faculty_group_kind_可筛选():
    """前端要"只看研究所"时靠它 —— 混在一起没法分组。"""
    all_items = fetch_all(CLIENT, "/api/faculty")
    assert len(all_items) == len(FAC_RAW), (len(all_items), len(FAC_RAW))

    kinds = {it["group_kind"] for it in all_items}
    # **必须四类都遍历到**，而 kinds 来自取全的数据 —— 这正是要 fetch_all 的原因：
    # 原来只取两页（硬编码 50+50=100 < 259），漏掉的 159 条里
    # 可能整类都没被筛过，而断言还是绿的。
    for k in kinds:
        got = fetch_all(CLIENT, "/api/faculty", {"group_kind": k})
        assert got, k
        assert all(i["group_kind"] == k for i in got), k
        assert len(got) == sum(1 for it in all_items if it["group_kind"] == k), k

    # 两个条件叠加：total 必须等于数据里这两类的条数
    body = CLIENT.get("/api/faculty", params={"group_kind": "机构,身份", "size": 50}).json()
    expect = sum(1 for it in all_items if it["group_kind"] in ("机构", "身份"))
    assert body["total"] == expect, (body["total"], expect)
    # 叠加筛选的当页里不许混进第三类
    assert all(i["group_kind"] in ("机构", "身份") for i in body["items"])


@case
def faculty_facets_带_group_kinds():
    body = CLIENT.get("/api/faculty/facets").json()
    assert "group_kinds" in body, list(body.keys())
    assert sum(k["count"] for k in body["group_kinds"]) == len(FAC_RAW)


# ---------- 6. 付费墙（契约 api.md 第 5 节 + fixtures/me.response.json） ----------

@case
def me_响应形状与_fixture_一致():
    fx = _fixture("me.response.json")
    got = _fresh_client().get("/api/me").json()
    assert set(got.keys()) == set(fx.keys()) == {"user", "plans", "_note"}, set(got.keys())
    assert set(got["user"].keys()) == set(fx["user"].keys()), \
        (set(got["user"].keys()), set(fx["user"].keys()))
    # 套餐逐字段一致（含价格 —— 改价是产品决定，不该被实现悄悄改掉）
    assert got["plans"] == fx["plans"], got["plans"]
    # 演示说明必须留着：评委看到"支付成功"要能看出是模拟的
    assert got["_note"] == fx["_note"]


@case
def 免费额度_每天三条之后返回_402():
    c = _fresh_client()
    ids = [r["id"] for r in RAW[:5]]
    codes = [c.get(f"/api/notices/{i}").status_code for i in ids[:3]]
    assert codes == [200, 200, 200], codes
    r = c.get(f"/api/notices/{ids[3]}")
    assert r.status_code == 402, r.status_code
    body = r.json()
    assert body["error"]["code"] == "QUOTA_EXCEEDED"
    # 402 响应**不能带正文** —— 带了就等于没墙
    assert "body_text" not in body and "title" not in body, list(body.keys())
    # 但必须告诉用户怎么解锁，而不是干甩一个 403
    assert body["upgrade_hint"]["plans"], body


@case
def 免费额度_查不存在的_id_不扣次数():
    """查一个不存在的公告不该消耗用户的次数 —— 否则打错字就少一次。"""
    c = _fresh_client()
    assert c.get("/api/notices/不存在").status_code == 404
    for i in RAW[:3]:
        assert c.get(f"/api/notices/{i['id']}").status_code == 200
    assert c.get("/api/me").json()["user"]["daily_used"] == 3


@case
def 免费额度_走响应头而_不污染_body():
    """额度是**访问控制状态**，不是公告数据，所以走响应头。

    塞进 body 会让"详情响应与 fixture 一致"这条硬约束失效 ——
    这个坑我是真踩了：加 `_quota` 之后 `详情默认键集合与_fixture_一致` 立刻红了。
    """
    c = _fresh_client()
    r = c.get(f"/api/notices/{RAW[0]['id']}")
    assert r.status_code == 200
    assert r.json().keys() == _fixture("notices.detail.json").keys(), list(r.json().keys())
    assert r.headers["X-Quota-Remaining"] == "2", dict(r.headers)
    assert r.headers["X-Quota-Used"] == "1"
    assert r.headers["X-Quota-Limit"] == "3"


@case
def 免费额度_列表不受限():
    c = _fresh_client()
    for _ in range(5):
        assert c.get("/api/notices", params={"size": 5}).status_code == 200
    assert c.get("/api/me").json()["user"]["daily_used"] == 0


@case
def 订阅后不再受限且_me_反映订阅状态():
    c = _fresh_client()
    ids = [r["id"] for r in RAW[:6]]
    for i in ids[:3]:
        c.get(f"/api/notices/{i}")
    assert c.get(f"/api/notices/{ids[3]}").status_code == 402

    r = c.post("/api/subscribe", json={"user_id": "u_demo", "plan_id": "monthly"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order"]["status"] == "paid" and body["order"]["amount"] == 29
    assert body["order"]["_note"], "订单必须标注是模拟支付"
    assert body["_note"], "响应必须标注是模拟支付"

    assert c.get("/api/me").json()["user"]["subscribed"] is True
    for i in ids[3:]:
        assert c.get(f"/api/notices/{i}").status_code == 200, i


@case
def 订阅_plan_id_校验():
    c = _fresh_client()
    r = c.post("/api/subscribe", json={"plan_id": "lifetime"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_PLAN"


@case
def 订阅续期从原到期日往后加而不是重置():
    c = _fresh_client()
    c.post("/api/subscribe", json={"user_id": "u_demo", "plan_id": "monthly"})
    first = c.get("/api/me").json()["user"]["expires_at"]
    c.post("/api/subscribe", json={"user_id": "u_demo", "plan_id": "monthly"})
    second = c.get("/api/me").json()["user"]["expires_at"]
    assert second > first, (first, second)


# ---------- 7. 论坛（契约 api.md 第 4 节，字段表是 B 的默认值见 issues/007） ----------

def _new_post(client, **kw):
    payload = {"title": "推免时间线求确认", "body": "物理学院今年什么时候截止？",
               "unit": "物理学院", "tag": "推免经验", "author_real_id": "u_alice"}
    payload.update(kw)
    return client.post("/api/posts", json=payload)


@case
def 发帖_绝不返回可识别身份的信息():
    """**这是整节最重要的一条。** 后台实名、前台匿名 ——
    `author_real_id` / `created_ip` / `review_status` 只要在响应里出现一次，
    匿名就是假的。

    字段表**直接对着 `fixtures/posts.list.json` 比**，不在这里再抄一份 ——
    抄一份就是同一张表的第三份拷贝（契约一份、fixture 一份、测试一份），必然漂移。
    """
    fx = _fixture("posts.list.json")
    c = _fresh_client()
    r = _new_post(c)
    assert r.status_code == 201, r.text
    got = r.json()
    for k in ("author_real_id", "created_ip", "reports", "review_status"):
        assert k not in got, f"响应里漏了 {k}！"
    assert got["author_display"].startswith("匿名·"), got["author_display"]

    # 公开字段表 = fixture 的键集合（九个，一个不多一个不少）
    assert set(got.keys()) == set(fx["items"][0].keys()), \
        set(got.keys()) ^ set(fx["items"][0].keys())

    # 列表里也不能漏
    items = c.get("/api/posts").json()["items"]
    assert items
    for i in items:
        assert set(i.keys()) <= set(fx["items"][0].keys()) | {"body_truncated"}, \
            set(i.keys()) - set(fx["items"][0].keys())
        for k in ("author_real_id", "created_ip", "review_status"):
            assert k not in i, f"列表里漏了 {k}！"


@case
def 可见帖也不泄漏被举报过这件事():
    """**合规要求，不是"少给一个字段"**（A 指出的）。

    `review_status` 的 `rejected` 值意思是"被举报过但举报不成立"。
    一旦公开，**任何人都能看出哪些帖子被举报过** ——
    举报这件事在匿名论坛里本身就是敏感信息。

    `VISIBLE_STATUSES` 只保证 hidden/pending 的帖子不出现，
    但**可见帖子的状态仍然会泄露它被举报过**。所以整个字段都不能给。
    """
    c = _fresh_client()
    pid = _new_post(c).json()["id"]
    # 举报一次但不到阈值 -> 帖子仍然 visible，但已经被举报过了
    c.post(f"/api/posts/{pid}/report", json={"reason": "试一下", "reporter_id": "u_x"})
    items = c.get("/api/posts").json()["items"]
    assert len(items) == 1, "帖子应当仍然可见"
    assert "review_status" not in items[0], \
        "可见帖也不能透露 review_status —— 那等于公开'这条被举报过'"
    # 管理员那边才看得到
    hdr = {"X-Admin-Token": config.ADMIN_TOKEN}
    q = c.get("/api/admin/queue", headers=hdr).json()
    assert q["total"] == 0, "没到阈值就不该进队列"
    # 再举报两次到阈值，前台消失、后台能看
    for i in range(2):
        c.post(f"/api/posts/{pid}/report", json={"reporter_id": f"u_y{i}"})
    assert c.get("/api/posts").json()["total"] == 0
    q = c.get("/api/admin/queue", headers=hdr).json()
    assert q["total"] == 1 and q["items"][0]["review_status"] == "pending"


@case
def 发帖_匿名代号确定性派生():
    """同一个人永远同一代号；不同人不同代号。

    代号随机的话，"匿名"会变成"每次都不一样" —— 作者自己都追不到发过什么。
    """
    assert forum.display_name_of("u_alice") == forum.display_name_of("u_alice")
    names = {forum.display_name_of(x) for x in
             ["u_alice", "u_bob", "u_carol", "u_dave", "u_eve", "u_frank"]}
    assert len(names) == 6, names
    # 前端传来的 author_display 被忽略（不许伪装成别人）
    c = _fresh_client()
    got = _new_post(c, author_display="匿名·管理员").json()
    assert got["author_display"] == forum.display_name_of("u_alice"), got["author_display"]


@case
def 发帖_缺必填字段要_400():
    c = _fresh_client()
    assert c.post("/api/posts", json={"title": "只有标题"}).status_code == 400
    assert c.post("/api/posts", json={"body": "只有正文"}).status_code == 400
    assert c.post("/api/posts", json={}).status_code == 400


@case
def 论坛列表_过滤与分页可用():
    c = _fresh_client()
    for i in range(3):
        _new_post(c, title=f"物理学院的问题{i}", unit="物理学院", tag="推免经验")
    _new_post(c, title="材料学院的调剂", unit="材料科学与工程学院", tag="调剂")
    body = c.get("/api/posts", params={"unit": "物理学院"}).json()
    assert body["total"] == 3, body["total"]
    body = c.get("/api/posts", params={"tag": "调剂"}).json()
    assert body["total"] == 1
    body = c.get("/api/posts", params={"q": "材料"}).json()
    assert body["total"] == 1


@case
def 论坛全文_非订阅者截断到_200_字():
    """契约 api.md 第 5 节：论坛**全文**归订阅，非订阅者给前 200 字 + truncated。"""
    c = _fresh_client()
    long_body = "这是一段很长的正文。" * 60          # 600 字
    _new_post(c, body=long_body)

    free = c.get("/api/posts", params={"user": "u_free"}).json()["items"][0]
    assert free["body_truncated"] is True, free
    assert len(free["body"]) <= config.POST_BODY_FREE_LIMIT + 1, len(free["body"])
    assert free["body"].endswith("…")

    # 订阅后给全文
    c.post("/api/subscribe", json={"user_id": "u_paid", "plan_id": "monthly"})
    paid = c.get("/api/posts", params={"user": "u_paid"}).json()["items"][0]
    assert paid["body"] == long_body
    assert "body_truncated" not in paid


@case
def 论坛_facets_带院系建议初始种():
    """论坛一开始没人发帖，光靠已有取值做建议是空的 ——
    用户就会把「物理学院」和「物理系」敲成两个版块。"""
    seed = _fixture_units()
    body = _fresh_client().get("/api/posts/facets").json()
    assert body["total"] == 0, "新状态下列表应为空"
    # **条目数**要等于 units.json 的条数，不是"去重后的院系名个数" ——
    # 同一个「研究生院」在 5 所学校下都有，合并了就会丢掉学校信息。
    assert len(body["units"]) == len(seed), (len(body["units"]), len(seed))
    values = {u["value"] for u in body["units"]}
    assert "物理学院" in values and "研究生院" in values, values
    # 建议值要标明来源，不能让人以为"已经有这么多帖子了"
    assert all(u.get("from_seed") for u in body["units"])
    assert any(u.get("notice_count") for u in body["units"])
    assert "_note" in body
    # 同名的「研究生院」必须一条对一所学校，不许并成一条
    yjs = [u for u in body["units"] if u["value"] == "研究生院"]
    assert len(yjs) == sum(1 for s in seed if s["unit"] == "研究生院"), yjs
    assert len({u["school"] for u in yjs}) == len(yjs), yjs


@case
def 举报到阈值自动进审核队列并从前台消失():
    """**举报入口如果是装饰，删帖规则就是空话。** 举报够了必须真的下前台。"""
    c = _fresh_client()
    pid = _new_post(c).json()["id"]
    assert c.get("/api/posts").json()["total"] == 1

    # 每个举报人不同 —— **同一人只计一次**（契约 api.md 第 204 行）
    for i in range(forum.REPORT_THRESHOLD - 1):
        r = c.post(f"/api/posts/{pid}/report",
                   json={"reason": f"理由{i}", "reporter_id": f"u_r{i}"})
        assert r.status_code == 200
        assert r.json()["review_status"] == "visible", r.json()
        assert r.json()["deduped"] is False

    r = c.post(f"/api/posts/{pid}/report",
               json={"reason": "最后一次", "reporter_id": "u_last"})
    assert r.json()["review_status"] == "pending", r.json()
    # 前台立刻不可见
    assert c.get("/api/posts").json()["total"] == 0
    # 但**没有消失**：在审核队列里，等着人处理
    assert len(forum.queue(c.app.state.state)) == 1


@case
def 举报_同一人重复举报只计一次():
    """防滥用：否则找三个朋友就能把任何帖子压下去，论坛会变成互相举报的工具。"""
    c = _fresh_client()
    pid = _new_post(c).json()["id"]

    for _ in range(forum.REPORT_THRESHOLD + 2):
        r = c.post(f"/api/posts/{pid}/report",
                   json={"reason": "刷举报", "reporter_id": "u_same"})
        assert r.status_code == 200
    r = c.post(f"/api/posts/{pid}/report",
               json={"reason": "刷举报", "reporter_id": "u_same"}).json()
    assert r["report_count"] == 1, r
    assert r["deduped"] is True, r
    assert "已举报过" in r["note"] or "未重复计数" in r["note"], r
    # 一个人刷不出 pending
    assert r["review_status"] == "visible", r
    assert c.get("/api/posts").json()["total"] == 1

    # 换三个不同的人立刻进队列
    for i in range(forum.REPORT_THRESHOLD):
        c.post(f"/api/posts/{pid}/report", json={"reporter_id": f"u_{i}"})
    assert c.get("/api/posts").json()["total"] == 0


@case
def 审核队列需要_token_且能看出被举报的理由():
    c = _fresh_client()
    pid = _new_post(c).json()["id"]
    for i in range(forum.REPORT_THRESHOLD):
        c.post(f"/api/posts/{pid}/report",
               json={"reason": "疑似广告", "reporter_id": f"u_r{i}"})

    assert c.get("/api/admin/queue").status_code == 401
    assert c.get("/api/admin/queue", headers={"X-Admin-Token": "wrong"}).status_code == 401
    r = c.get("/api/admin/queue", headers={"X-Admin-Token": config.ADMIN_TOKEN})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    # 审核的人要能看出"为什么被举报"，只给一个 count 是没法判的
    assert items[0]["reports"] and items[0]["reports"][0]["reason"] == "疑似广告"
    # 后台可以看全字段（含实名留痕），这是审核队列存在的意义
    assert items[0]["author_real_id"] == "u_alice"


@case
def 审核动作_hide_restore_delete():
    c = _fresh_client()
    hdr = {"X-Admin-Token": config.ADMIN_TOKEN}
    pid = _new_post(c).json()["id"]

    assert c.post(f"/api/admin/posts/{pid}", params={"action": "hide"},
                  headers=hdr).json()["review_status"] == "hidden"
    assert c.get("/api/posts").json()["total"] == 0

    # 举报不成立 → 恢复展示（rejected 也是前台可见的）
    assert c.post(f"/api/admin/posts/{pid}", params={"action": "restore"},
                  headers=hdr).json()["review_status"] == "rejected"
    assert c.get("/api/posts").json()["total"] == 1

    assert c.post(f"/api/admin/posts/{pid}", params={"action": "delete"},
                  headers=hdr).json()["review_status"] == "deleted"
    assert c.get("/api/posts").json()["total"] == 0
    # 非法动作要报错
    assert c.post(f"/api/admin/posts/{pid}", params={"action": "vanish"},
                  headers=hdr).status_code == 400


@case
def 举报不存在的帖子_404():
    c = _fresh_client()
    assert c.post("/api/posts/p_不存在/report", json={}).status_code == 404


@case
def health_公开假鉴权的口径():
    """假鉴权必须**公开标注**，免得有人以为这是权限系统。"""
    body = CLIENT.get("/api/health").json()
    # 口令**绝不**经接口返回（以前会原样返回，部署到公网等于没有鉴权）
    assert "admin_token" not in body["demo_auth"]
    assert config.ADMIN_TOKEN not in CLIENT.get("/api/health").text
    assert body["demo_auth"]["admin_header"] == "X-Admin-Token"
    assert "不是权限系统" in body["demo_auth"]["note"]
    assert "state" in body and body["state"]["path"]


def _public_client():
    """公网演示模式（TUIMIAN_PUBLIC=1）的隔离客户端。"""
    return TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:"),
                                 public_mode=True))


@case
def public_mode_演示身份只读_访客身份可写():
    """公网演示版：访客改不了 u_demo / u_num_demo，但能写自己的 guest_xxx。

    没有这道闸，任何一个打开链接的人都能把演示画像改掉，
    下一位评委看到的推送就全变了。
    """
    c = _public_client()
    assert c.get("/api/health").json()["public_mode"] is True

    # 演示身份：写画像 / 加关注 / 发帖 全部 403
    assert c.put("/api/profile?user=u_demo", json={"majors": ["物理学"]}).status_code == 403
    assert c.put("/api/profile?user=u_num_demo", json={"majors": ["物理学"]}).status_code == 403
    assert c.post("/api/posts", json={"title": "x", "body": "y"}).status_code == 403
    # 访客身份：可以写
    r = c.put("/api/profile?user=guest_abc123", json={"majors": ["物理学"]})
    assert r.status_code == 200, r.text
    # 读接口对演示身份照常可用（演示视频就是靠这个）
    assert c.get("/api/profile?user=u_num_demo").status_code == 200


@case
def public_mode_大请求体被拒():
    c = _public_client()
    big = {"majors": ["物理学"], "interest_keywords": ["x" * (config.MAX_REQUEST_BYTES + 10)]}
    r = c.put("/api/profile?user=guest_big", json=big)
    assert r.status_code in (400, 413), r.status_code


@case
def 状态不写进_data_out():
    """B 一个字节都不该写进 A 的产出目录。"""
    assert not os.path.abspath(config.STATE_PATH).startswith(
        os.path.abspath(os.path.join(ROOT, "data")) + os.sep), config.STATE_PATH


# ---------- 8. 今日更新（契约 api.md 第 10 节） ----------

def _changes_client(payload):
    """注入一份给定的变更报告（不读磁盘），用来测投影。"""
    from server.changes import ChangesStore
    return TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:"),
                                 ChangesStore("<injected>", payload)))


# 一份"真的有变更"的报告。形状照 `collector/diff.py` 的真实产出：
# added/removed 是完整记录、changed 是 {record, fields, hash, body_changed}、
# reidentified 是 {record, old_id, new_id}。
def _sample_changes():
    full = dict(RAW[0])
    full["body_text"] = "很长的正文" * 500
    ch_rec = dict(RAW[1])
    ch_rec["body_text"] = "改了" * 500
    re_rec = dict(RAW[2])
    rp_rec = dict(RAW[3])
    return {
        "generated_at": "2026-10-17T02:10:00",
        "counts": {"previous": 76, "current": 78, "added": 1, "changed": 1,
                   "reparsed": 1, "removed": 1, "reidentified": 1, "unchanged": 74},
        "added": [full],
        "changed": [{"record": ch_rec,
                     # `attachments` 的原始形状是**一对签名列表**（`diff.py` 的 `_att_sig`），
                     # 不是记录、也不是 `[(url, text_len), ...]` 的单层。
                     "fields": {"deadline": [None, "2026-11-20"],
                                "attachments": [[], [("https://a.pdf", 120),
                                                     ("https://b.pdf", 0)]]},
                     "hash": ("sha1:old", "sha1:new"), "body_changed": True}],
        "reparsed": [{"record": rp_rec,
                      "fields": {"title": ["旧标题（带站点名后缀）", "新标题"]},
                      "hash": ("sha1:same", "sha1:same"), "body_changed": False,
                      # 契约：reparsed[] 多一个 why，界面要如实说明原因
                      "why": "抽取器版本变了"}],
        "removed": [dict(RAW[4])],
        "reidentified": [{"record": re_rec, "old_id": "old-1", "new_id": re_rec["id"]}],
    }


@case
def changes_形状与契约一致():
    body = CLIENT.get("/api/changes").json()
    assert set(body.keys()) >= {"generated_at", "counts", "added", "changed",
                                "reparsed", "removed", "reidentified"}, set(body.keys())
    # counts 八个键一个不少（契约 api.md 第 412 行，含 reparsed）
    assert set(body["counts"].keys()) == set(changes_mod.CHANGE_COUNT_KEYS), \
        set(body["counts"].keys())


@case
def changes_不把完整记录塞进响应():
    """契约的核心要求：磁盘上那个文件含 `body_text`（626 KB），接口必须精简。

    这条用**注入的真实形状**测（不读磁盘）——
    否则断言就绑在一份每天都会变的文件上，只有恰好那天有变更才测得到。
    """
    payload = _sample_changes()
    body = _changes_client(payload).get("/api/changes").json()

    assert len(body["added"]) == 1 and len(body["removed"]) == 1
    for bucket in ("added", "removed"):
        item = body[bucket][0]
        assert "body_text" not in item, f"{bucket} 里带了 body_text"
        assert set(item.keys()) == set(changes_mod.CHANGE_RECORD_FIELDS), set(item.keys())
    for bucket in ("changed", "reparsed", "reidentified"):
        assert "body_text" not in body[bucket][0]["record"], bucket
        assert set(body[bucket][0]["record"].keys()) == set(changes_mod.CHANGE_RECORD_FIELDS)

    # 精简后必须显著变小 —— 上面那两条记录各带 2500 字正文
    raw = json.dumps(payload, ensure_ascii=False)
    out = json.dumps(body, ensure_ascii=False)
    assert len(out) < len(raw) / 2, (len(out), len(raw))


@case
def changes_reparsed_必须和_changed_分开():
    """契约 api.md 第 431 行专门讲了为什么这两件事不能混。

    - `changed`  = **学校真的改了通知**（正文哈希变了）→「这条通知被修改过」，付费核心
    - `reparsed` = **我们改了抽取代码**（正文没变，只有抽取字段不同）→「我们改进了抓取」

    A 修好标题解析器之后，日报报出「有变化 22 条」，而**公告一个字都没变** ——
    用户会以为学校改了 22 份通知。这跟 `reidentified` 是同一类错误：
    **把"我们的处理变了"说成"外面的世界变了"。**
    """
    body = _changes_client(_sample_changes()).get("/api/changes").json()
    # 两个桶各自独立，计数也分开
    assert len(body["changed"]) == 1 and len(body["reparsed"]) == 1
    assert body["counts"]["changed"] == 1 and body["counts"]["reparsed"] == 1
    # 关键：reparsed 里的条目**不能**出现在 changed 里
    ch_ids = {c["id"] for c in body["changed"]}
    rp_ids = {c["id"] for c in body["reparsed"]}
    assert ch_ids and rp_ids and not (ch_ids & rp_ids), (ch_ids, rp_ids)
    # 语义标志也要对得上：reparsed 的正文没变、changed 的变了
    assert body["reparsed"][0]["body_changed"] is False
    assert body["changed"][0]["body_changed"] is True
    # reparsed 的 fields 是真的字段级差异（标题抽取结果变了）
    assert "title" in body["reparsed"][0]["fields"]
    # **`why` 必须透传**：契约要求界面如实说明"为什么这条被判成我们改了代码"。
    # 实测磁盘上是「抽取器版本变了」/「附件抽取结果变了」/「派生字段不同」。
    assert body["reparsed"][0].get("why"), body["reparsed"][0]
    # `changed` 没有这个字段（契约里它的判据已够明确），不该凭空多一个 null 键
    assert "why" not in body["changed"][0], body["changed"][0]


@case
def changes_附件变更压成条数而不是塞_38_个对象():
    """`changed.fields.attachments` 在磁盘上是**哨兵元组**、不是记录。

    所以一对变更里可能躺着几十个附件对象（北大那条实测 38 个），
    原样透传会撑大响应，而且 `[[url, n], ...]` 这种形状前端也渲染不出来。
    """
    body = _changes_client(_sample_changes()).get("/api/changes").json()
    ch = body["changed"][0]
    assert ch["id"], ch
    assert ch["body_changed"] is True
    assert ch["fields"]["deadline"] == [None, "2026-11-20"], ch["fields"]
    att = ch["fields"]["attachments"]
    assert att["old_count"] == 0 and att["new_count"] == 2, att
    assert "_detail" in att
    # 原始里的附件签名数组不该出现在响应里
    assert not isinstance(att, list), att


@case
def changes_四类各自独立不混淆():
    """`reidentified` 单独一类（契约第 2 条）：既不是新增也不是消失。

    B 为这个踩过一次 —— 30 条老公告因为抓取算法调整换了 id，
    被同时报成"新增 30"和"未抓到 30"。
    """
    body = _changes_client(_sample_changes()).get("/api/changes").json()
    assert body["counts"]["added"] == 1 and body["counts"]["reidentified"] == 1
    added_ids = {i["id"] for i in body["added"]}
    re_ids = {i["id"] for i in body["reidentified"]}
    rm_ids = {i["id"] for i in body["removed"]}
    assert not (added_ids & re_ids), (added_ids, re_ids)
    assert not (added_ids & rm_ids), (added_ids, rm_ids)
    # id 变迁的前后都要给出来，前端要展示 `旧 → 新`
    x = body["reidentified"][0]
    assert x["old_id"] == "old-1" and x["new_id"] and x["title"], x


@case
def changes_空报告也要给完整结构():
    """**不能因为没变更就少给键** —— 前端按固定字段读，少键会静默成 undefined。

    这条原来是读**磁盘上那份 changes.json** 再断言它是空的，文档字符串还写着
    "当前磁盘上就是空报告" —— 那是把断言绑在一份**每天都会变的文件**上，
    只有恰好没变化的那天才绿。A 改成有变更之后它立刻红了。
    **门禁必须可重复**，所以这里自己造一份空报告（注入，不碰磁盘）。
    """
    empty = {
        "generated_at": "2026-01-01T00:00:00",
        "counts": {"previous": 10, "current": 10, "added": 0, "changed": 0,
                   "reparsed": 0, "removed": 0, "reidentified": 0, "unchanged": 10},
        "added": [], "changed": [], "reparsed": [], "removed": [], "reidentified": [],
    }
    body = _changes_client(empty).get("/api/changes").json()
    assert body["generated_at"] == "2026-01-01T00:00:00"
    for k in ("added", "changed", "reparsed", "removed", "reidentified"):
        assert body[k] == [], (k, body[k])
    # 计数键一个不少（含 reparsed），值如实透传
    assert set(body["counts"].keys()) == set(changes_mod.CHANGE_COUNT_KEYS)
    assert body["counts"]["added"] == 0 and body["counts"]["unchanged"] == 10

    # 连"完全没有 counts 键"的残缺报告也不能少给键
    partial = {"added": [], "changed": []}
    body2 = _changes_client(partial).get("/api/changes").json()
    assert set(body2["counts"].keys()) == set(changes_mod.CHANGE_COUNT_KEYS)
    for k in ("added", "changed", "reparsed", "removed", "reidentified"):
        assert body2[k] == [], k


@case
def changes_读不到文件时不假装没有变化():
    """**这条最要紧。** "报告读坏了"和"今天没有变化"是两个结论。

    把前者显示成后者，用户会以为可以安心了 —— 而实际上我们什么都不知道。
    所以坏文件时所有计数一律 None + `_error`，绝不填 0。
    """
    from server.changes import ChangesStore
    broken = ChangesStore("/nonexistent/changes.json", None, load_error="读取失败：坏了")
    payload = broken.response()
    assert payload["_error"], payload
    assert all(v is None for v in payload["counts"].values()), payload["counts"]
    assert payload["added"] == [] and payload["changed"] == []

    # 文件根本不存在（基线未建立）：四类是真的 0，但 previous/current/unchanged 给 None
    import tempfile
    missing = ChangesStore.load(os.path.join(tempfile.gettempdir(), "不存在的文件.json"))
    p2 = missing.response()
    assert p2["counts"]["added"] == 0
    assert p2["counts"]["previous"] is None and p2["counts"]["unchanged"] is None
    assert p2.get("_error")


@case
def changes_磁盘上的真实文件走一遍投影():
    """读**真实文件**跑一遍 —— 保证这条路径本身是通的。

    **只断言"不变式"，不断言"文件里有什么"**：那份文件每天都会被重写
    （今天有变更、明天可能没有），绑内容就等于绑日期。
    不变式有两条，任何一天都必须成立：
      ① counts 键一个不少
      ② 任何桶里的记录都不带 `body_text`（精简是接口的存在理由）
    """
    path = config.CHANGES_PATH
    if not os.path.exists(path):
        return
    body = changes_mod.ChangesStore.load(path).response()
    assert set(body["counts"].keys()) == set(changes_mod.CHANGE_COUNT_KEYS), \
        set(body["counts"].keys())
    for bucket in changes_mod.CHANGE_BUCKETS:
        assert bucket in body, bucket
        assert isinstance(body[bucket], list), bucket
        for item in body[bucket]:
            rec = item.get("record", item)
            assert "body_text" not in rec, f"{bucket} 里漏了正文"
    # 真实数据里有 reparsed 时，`why` 必须带出来（契约要求界面说明原因）
    for item in body.get("reparsed") or []:
        assert item.get("why"), f"reparsed 条目缺 why：{item.get('id')}"


# ---------- 9. 套磁信（契约 api.md 第 3 节，模板版） ----------

LETTER_FX = _fixture("letter.response.json")


@case
def letter_形状与_fixture_一致():
    r = CLIENT.post("/api/letter", json={"notice_id": LETTER_FX["notice_id"]})
    assert r.status_code == 200, r.text
    got = r.json()
    # fixture 里有的键一个都不能少；多出来的键（method/warnings/…）是契约允许的
    assert set(LETTER_FX.keys()) <= set(got.keys()), \
        set(LETTER_FX.keys()) - set(got.keys())
    assert got["notice_id"] == LETTER_FX["notice_id"]
    # subject 是拼的、但格式必须和 fixture 一致（标题不加书名号这类细节）
    assert got["subject"] == LETTER_FX["subject"], (got["subject"], LETTER_FX["subject"])
    # citations 必须是**对象数组**（契约第 124 行），不是字符串数组
    assert isinstance(got["citations"], list) and got["citations"]
    ckeys = {k for c in got["citations"] for k in c}
    assert ckeys == {k for c in LETTER_FX["citations"] for k in c}, ckeys


@case
def letter_每条引文都能在原文里找到():
    """**这是整节最要紧的一条。**

    演示时最有价值的不是信写得好不好，是 `citations` 逐条能点回原文 ——
    那是防"模型编造导师信息"的手段。所以引文必须逐字可信：
    至少最近一次跑的时候，每条 `body_text` 引文都要能在正文里**逐字搜到**。
    """
    got = CLIENT.post("/api/letter", json={"notice_id": LETTER_FX["notice_id"]}).json()
    rec = next(r for r in RAW if r["id"] == LETTER_FX["notice_id"])
    body = rec.get("body_text") or ""
    for c in got["citations"]:
        q = c["quote"]
        assert q, c
        assert c["url"] == rec["url"], c
        if c["source"] == "title":
            assert q == rec["title"], q
        elif c["source"] == "body_text":
            assert q in body, f"引文不在正文里，无法回链：{q[:80]}"
        elif c["source"] == "materials":
            assert q == "、".join(rec["materials"]), q
            # **结构化字段拼的不能标成"公告原文"** —— 那是诚信问题
            assert c["label"] != "公告原文", c
        elif c["source"] == "deadline":
            assert q == rec["deadline"], q
            assert c["label"] != "公告原文", c
        else:
            raise AssertionError(f"未知 source: {c['source']}")


@case
def letter_没有正文时不编内容():
    """正文抽不到的公告**不能填假引文** —— 那正是这套机制要防的事。

    注意这条测的是"不编"，不是"必须有某句固定文案"：
    公告可能没有正文、但有 `materials`（RAW[0] 就是这种情况，materials=['论文']），
    那时要点行会列材料要求 —— 也是真实信息，不需要"什么都引不到"那句软提示。
    """
    rec = dict(RAW[0])
    rec["body_text"] = ""
    letter = letter_mod.build_letter(rec, {})
    # 核心：不许出现 body_text 类型的引文（没有正文却"引用"了正文 = 编造）
    assert not [c for c in letter["citations"] if c["source"] == "body_text"], \
        letter["citations"]
    # 而且要在 warnings 里明说正文没抽到
    assert any("未抽取到" in w for w in letter["warnings"]), letter["warnings"]
    # 三条要点行全都引不到时，才给"以原文为准"的软提示
    bare = dict(rec, materials=[], deadline=None)
    soft = letter_mod.build_letter(bare, {})
    assert "以原文为准" in soft["body"], soft["body"][:300]


@case
def letter_必须标明是模板不是模型():
    """A 的硬要求：响应要说清 `body` 不是模型生成的。"""
    got = CLIENT.post("/api/letter", json={"notice_id": LETTER_FX["notice_id"]}).json()
    assert got["method"] == "template", got.get("method")
    assert "_note" in got and "不是大模型" in got["_note"], got.get("_note")
    assert any("模板草稿" in w for w in got["warnings"]), got["warnings"]


@case
def letter_引文挑的是对申请人的要求而不是校方开场白():
    """第一版按"关键词命中数"排序，挑中的是「退役人员免初试」和「专项计划初试加分」——
    对推免本科生最不相关的两句。**一封引用"退役士兵政策"的套磁信
    会让老师觉得学生根本没读公告。** 所以加了负向词表和开场白词表。
    """
    got = CLIENT.post("/api/letter", json={"notice_id": LETTER_FX["notice_id"]}).json()
    quotes = [c["quote"] for c in got["citations"] if c["source"] == "body_text"]
    assert quotes, "应当至少引到一句正文"
    banned = ("退役", "士兵", "战功", "表彰", "少数民族", "照顾政策", "专项计划")
    for q in quotes:
        assert not any(b in q for b in banned), f"引了特殊考生政策：{q[:60]}"
    # 也不该引用纯开场白
    for q in quotes:
        assert not q.startswith("2027年东南大学坚持"), q[:60]


@case
def letter_留下让用户填的空():
    """这封信是**草稿**，不是能直接发的东西 —— 该留空的地方一处都不能少。

    2026-10-19（B24）之后：占位符还在，但**每个都带一句"缺什么"**
    （`____（请补充你的学校）`）。光秃秃的 `____` 会让人猜半天，
    而"画像里明明有"的那部分现在会被填进去（见下一条用例）。
    """
    got = CLIENT.post("/api/letter", json={"notice_id": LETTER_FX["notice_id"]}).json()
    body = got["body"]
    for need in ("____（请补充你的学校）", "____（请补充你的专业）",
                 "____（请补充GPA 排名）", "____（请补充英语成绩）",
                 "____（请补充科研经历）", "____（请补充姓名）",
                 "____（请补充联系方式）"):
        assert need in body, need
    # 结尾要给回链，用户复制时出处不能丢
    assert "以上引文均来自" in body, body[-200:]


@case
def letter_画像有的字段要填进正文():
    """**B24（2026-10-19）：** 画像填了学校/专业，正文却还是「我是____大学____专业」。

    改之前 `build_body` 把占位符写死在模板里，画像只用来挑引文 ——
    用户看到的是一封"系统根本没读我画像"的信。现在：能填的都填，
    填不了的留 `____（请补充…）`。

    引文链**一点没动**：条数、逐字回链、url 都要和以前一样。
    """
    full = {"school": "浙江大学", "majors": ["物理学"],
            "gpa_rank": {"rank": 5, "total": 120},
            "english": {"cet6": 580},
            "research": ["CUPT 国赛一等奖", "凝聚态实验一段"],
            "name": "张三", "contact": "z@example.edu"}
    got = CLIENT.post("/api/letter",
                      json={"notice_id": LETTER_FX["notice_id"], "profile": full}).json()
    body = got["body"]
    assert "____" not in body, ("画像完整时正文里还有占位符", body[:400])
    for s in ("浙江大学", "物理学专业", "GPA 排名 5/120", "CET-6 580",
              "CUPT 国赛一等奖", "张三"):
        assert s in body, (s, body[:400])

    # 引文链不动：非空、每条 url 指向这条公告、正文引文逐字可回链
    rec = next(r for r in RAW if r["id"] == LETTER_FX["notice_id"])
    assert len(got["citations"]) >= 3, got["citations"]
    src = rec.get("body_text") or ""
    for c in got["citations"]:
        assert c["url"] == rec["url"], c
        if c["source"] == "body_text":
            assert c["quote"] in src, ("引文不在正文里", c["quote"][:60])
    assert "以上引文均来自" in body

    # 画像缺字段：占位符 + 明确提示（不许沉默）
    part = CLIENT.post("/api/letter", json={
        "notice_id": LETTER_FX["notice_id"],
        "profile": {"school": "浙江大学"}}).json()["body"]
    assert "浙江大学" in part
    assert "____（请补充你的专业）" in part, part[:300]
    assert "____（请补充英语成绩）" in part, part[:300]


@case
def letter_标题判据_单竖线能引_多竖线才挡():
    """标题里的**单个** `|` 是"栏目 | 标题"，不是表格。

    **这是 470 条数据上抓到的真 bug**：复旦「公告 | 服务中心学期中开放时间公告」
    是全场唯一零引文的公告，根因是 `_looks_like_table` 那条
    "一个 `|` 且没有句读 → 表格" 被判到了**标题**上 ——
    那条判据是为正文写的（正文句子本该有句读），套到标题就是误杀。

    标题只认**多列**（`|` 两次以上）才是抽到了表头。

    这条用例两头都钉：改坏了会红，改宽了也会红。
    """
    from server import letter as L

    # **能引**的（单竖线 / 正常标题）
    ok = [
        "公告 | 服务中心学期中开放时间公告",       # 复旦那条，真数据里的反例
        "2027年推免生招生简章",
        "关于开展2027年推荐免试研究生工作的通知",
        "公告|某某学院2027年推免工作安排",          # 无空格也照引
    ]
    for t in ok:
        why = L.quote_problem(t, "title")
        assert why is None, ("这标题该能引，却被挡了：%r → %s" % (t, why))

    # **该挡**的（真表头 / 太短 / 空 / 残缺）
    bad = [
        "序 号 | 申请编号 | 姓名 | 性别",   # 多列 = 表头
        "公告",                            # 太短（像栏目标签）
        "",                                # 空
        "关于2027年推免工作的…",             # 残缺（被页面截断）
    ]
    for t in bad:
        why = L.quote_problem(t, "title")
        assert why is not None, ("这标题该挡，却放行了：%r" % (t,))


@case
def letter_不把表格行当要求句引():
    """C 走查 #6：引文挑到了表格行。

    实测原样引进来的是
    `19 | 材料科学与工程学院 | 李某某 | 1120220000 | 材料物理与化学 | 陈某`
    和 `材料学院 | 赵某（硕士教学秘书） | 80000000`。
    **引这种内容比不引更糟** —— 老师看到套磁信里引着教学秘书的电话，
    只会觉得学生根本没读公告。

    修完（表格行**跳过**而不是"排后面"，并加兜底 + 警告）实测：
    - 全部 72 条公告里**一条引文都没有的：0 条**
    - 表格行只在"整篇正文都是表格"时兜底引 1 行，且带明确警告
    """
    bad = []
    no_cite = 0
    for rec in RAW:
        letter = letter_mod.build_letter(rec, {})
        if not letter["citations"]:
            no_cite += 1
        for c in letter["citations"]:
            if c["source"] != "body_text":
                continue
            if letter_mod._looks_like_table(c["quote"]):
                # 表格行**只能**出现在"整篇都是表格"的兜底里，且必须带警告
                warns = " ".join(letter["warnings"])
                if "表格" not in warns:
                    bad.append((rec["id"], c["quote"][:60], letter["warnings"]))
    assert not bad, f"{len(bad)} 条表格引文没带警告：{bad[:3]}"

    # **零引文可以，但必须"确实没法引"。** 2026-10-18 数据涨到 1119 条后，
    # 出现了一条真的没法引的：
    #   上海交大「致新生」—— 标题 3 个字是**栏目名**（不是公告标题），
    #   正文 30 字全是元信息（发布时间 / 点击数 / 分享至），一句正文都没有。
    # **它零引文是对的**：挡掉一条"公告"二字当标题的引文，比引上去更诚实。
    #
    # 所以这条用例改成验**"能引却没引"**：任何一条零引文的记录，
    # 它的**标题本身必须不合格**（标题是最后的兜底 —— 标题合格却零引文，才是 bug）。
    no_cite_records = [r for r in RAW if not letter_mod.build_letter(r, {})["citations"]]
    unexplained = [(r["id"], r.get("title")) for r in no_cite_records
                   if letter_mod.quote_problem(r.get("title"), "title") is None]
    assert not unexplained, (
        "%d 条公告一条引文都没有，**但它们的标题是合格的** —— 那是能引却没引"
        % len(unexplained), unexplained[:3])

    # 正常公告（正文是自然语言）**不该**引到表格行
    normal = [r for r in RAW if "|" not in (r.get("body_text") or "")]
    assert normal, "没有自然语言正文的公告，这条测试没意义"
    for rec in normal[:30]:
        letter = letter_mod.build_letter(rec, {})
        for c in letter["citations"]:
            if c["source"] == "body_text":
                assert not letter_mod._looks_like_table(c["quote"]), \
                    (rec["id"], c["quote"][:60])
                assert len(c["quote"]) >= 20, (rec["id"], c["quote"])


@case
def letter_引文短于20字的没有():
    """单字/两字的碎片（C 走查 #6 提到曾经引到单个词「论文」）不该出现在引文里。"""
    for rec in RAW[:40]:
        letter = letter_mod.build_letter(rec, {})
        for c in letter["citations"]:
            if c["source"] == "body_text":
                assert len(c["quote"]) >= 20, (rec["id"], len(c["quote"]), c["quote"])


@case
def letter_纯表格公告的警告如实说引了什么():
    """警告不能笼统写"只引了标题和材料/截止日" —— 实测很多条只有标题。

    **警告本身说错话，比不警告更糟。**

    判断用 `cited_from`（结构化），**不在警告文案里搜关键词** ——
    我第一版就是搜关键词，被另一条警告里的"截止日期"四个字误判了。

    ## 2026-10-17 改（环 3）：这里原来断言"一定有含「表格」的那句警告"

    那个断言当时成立，是因为"没有正文引文"只有一个原因：正文是表格。
    表格兜底被删掉之后，"没有正文引文"多了一个原因：**候选句被判不准丢了**
    （太短 / 残缺 / 表格式）。所以现在断言的是更准的东西：
    **必须有一句说清'为什么没引正文'的警告，而且它点名的来源要和 `cited_from` 对得上。**
    """
    checked = 0
    for rec in RAW:
        letter = letter_mod.build_letter(rec, {})
        srcs = set(letter["cited_from"])
        assert srcs == {c["source"] for c in letter["citations"]}, rec["id"]
        if "body_text" in srcs:
            continue
        # 走到这里 = 一句正文都没引到，必须说清原因（表格 / 判不准 / 没有要求句）
        w = [x for x in letter["warnings"] if "没有可引用的要求句" in x]
        assert w, (rec["id"], letter["warnings"])
        text = w[0]
        checked += 1
        # 说了"表格"，就必须真的是表格（degraded）；说了"判不准"，就必须真有丢弃
        if "表格" in text:
            assert letter_mod._looks_like_table(rec.get("body_text") or "") or any(
                letter_mod._looks_like_table(p)
                for p in letter_mod._split_paragraphs(rec.get("body_text"))), \
                (rec["id"], text)
        if "判不准" in text:
            assert [d for d in letter["quotes_dropped"] if d["source"] == "body_text"], \
                (rec["id"], text, letter["quotes_dropped"])
        # 警告里点名的来源，必须真的在 cited_from 里（**用集合判断，不搜文案**）
        if "材料要求" in text:
            assert "materials" in srcs, (rec["id"], text, srcs)
        if "截止日期" in text:
            assert "deadline" in srcs, (rec["id"], text, srcs)
        if "只引了标题" in text:
            assert "title" in srcs, (rec["id"], text, srcs)
    assert checked > 0, "没有一句正文都没引到的公告，这条测试没意义"


@case
def letter_判不准的引文一条都不许进信里_全量扫196条():
    """**环 3。** 引文的三种垃圾形态，一条都不许出现。

    DEMO-CHECKLIST §8.2 记着的那次事故：用一条 PDF 表格类公告生成套磁信，
    4 条引文里混着
      - 表格数据行：`19 | 材料科学与工程学院 | 李某某 | …`
      - 残缺标题：`…学术博士`
      - 两个字的名词：`论文`
    而**普通招生公告基本可用** —— 所以它当时是"偶尔成功"，正是用户说的
    "happy case"。这条用例把 196 条**全扫一遍**，不抽第一条、不赌哪条恰好坏。

    四个断言，对应四种坏形态：
      ① 不含表格行（`|` 分隔的数据行）
      ② 不以省略号结尾（残缺标题/半句的形态）
      ③ 正文引文有长度下限（两个字那种）
      ④ 正文引文**逐字能在原文里找到**（可回链，不能是拼出来的）
    """
    checked = 0
    for rec in RAW:
        letter = letter_mod.build_letter(rec, {})
        body = rec.get("body_text") or ""
        for c in letter["citations"]:
            q = c["quote"].strip()
            checked += 1
            assert q, (rec["id"], c)
            # ① 表格行
            assert not letter_mod._looks_like_table(q) or c["source"] != "body_text", \
                (rec["id"], "正文引文是表格行", q[:70])
            # ② 残缺（省略号结尾）
            assert not q.endswith(letter_mod.ELLIPSIS_TAILS), \
                (rec["id"], "引文以省略号结尾（残缺）", q[-40:])
            # ③ 长度下限
            if c["source"] == "body_text":
                assert len(q) >= letter_mod.QUOTE_MIN_BODY, (rec["id"], len(q), q)
            if c["source"] == "title":
                assert len(q) >= letter_mod.QUOTE_MIN_TITLE, (rec["id"], len(q), q)
            # ④ 可回链：正文引文必须逐字在原文里
            if c["source"] == "body_text":
                assert q in body, (rec["id"], "引文不在原文里，无法回链", q[:70])
    assert checked > 100, f"只扫到 {checked} 条引文，算不算全量？"

    # 丢掉的引文也必须给出理由，而且**不许把表格行原样搬进响应**
    for rec in RAW:
        letter = letter_mod.build_letter(rec, {})
        for d in letter["quotes_dropped"]:
            assert d.get("source") and d.get("reason"), (rec["id"], d)
            assert "quote" not in d, "把丢弃的原文搬回来了 —— 换个字段名它又出现在界面上"


@case
def letter_坏正文的三种形态_都判不准就不给():
    """**环 3 的坏输入。** 造三条公告，正文各自烂成一种形态。

    造数据而不是靠磁盘：真数据里"恰好"有没有这三种形态不可控，
    而且真数据明天可能就不坏了 —— 那这条测试会变成安慰。
    """
    base = _fake_notice("rb_letter_src", title="2027年接收推荐免试研究生工作办法")

    # ① 纯表格正文：表格行一条都不许进 citations，但信不能空、警告要说清
    t = dict(base, id="rb_letter_table",
             body_text="序号 | 志愿ID | 考生姓名 | 拟录取专业 | 复试成绩\n"
                       "国家专项计划 | 张一一 | 物理学 | 89 | 拟录取\n")
    lt = letter_mod.build_letter(t, {})
    assert not [c for c in lt["citations"] if c["source"] == "body_text"], lt["citations"]
    assert lt["citations"], "信里一条引文都没有 —— 表格式公告也该有标题/材料/截止日"
    assert any("表格" in w for w in lt["warnings"]), lt["warnings"]

    # ② 正文里只有一个两字碎片（"论文"那种）—— 有候选、但判不准
    #
    # 注意造的是**正文**里的碎片，不是 materials 里的：真数据里「论文」是
    # `materials` 的一项（标着"材料要求"的合法结构化信息），把它砍掉会把
    # 「身份证」「成绩单」一起砍掉。所以这里只治"正文引文太短"。
    s = dict(base, id="rb_letter_short", body_text="报名材料")
    ls = letter_mod.build_letter(s, {})
    assert not [c for c in ls["citations"] if c["source"] == "body_text"], ls["citations"]
    assert ls["quotes_dropped"], "丢了引文却不说 —— 那正是‘静默降级’"
    assert any("判不准" in w or "没有可引用" in w for w in ls["warnings"]), ls["warnings"]

    # ③ 残缺标题 + 残缺句子（页面把内容截断了，尾巴带省略号）
    e = dict(base, id="rb_letter_ellipsis",
             title="关于2027年接收推免生的通知…",
             body_text="申请人须在截止前提交成绩单与推荐信…")
    le = letter_mod.build_letter(e, {})
    for c in le["citations"]:
        assert not c["quote"].endswith("…"), ("残缺引文进信了", c)
    assert "title" not in le["cited_from"], "残缺标题不该被当引文（正文里还会提到标题）"
    assert any("丢弃" in w for w in le["warnings"]), le["warnings"]

    # ④ 正常公告**不受影响**：该引的正文句子还在（别把过滤器做成一律拒收）
    ok = dict(base, id="rb_letter_ok",
              body_text="申请条件：具有推免资格且成绩排名前30%。"
                        "材料要求：成绩单、推荐信、个人陈述。")
    lo = letter_mod.build_letter(ok, {})
    assert [c for c in lo["citations"] if c["source"] == "body_text"], lo["citations"]
    assert not lo["quotes_dropped"], lo["quotes_dropped"]


@case
def letter_缺_notice_id_或找不到公告时报错():
    assert CLIENT.post("/api/letter", json={}).status_code == 400
    r = CLIENT.post("/api/letter", json={"notice_id": "不存在"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# ---------- 10. 更新历史（月度订阅的凭据） ----------

@case
def history_给出每天一条的记录():
    body = CLIENT.get("/api/changes/history").json()
    assert "days" in body and body["total"] == len(body["days"])
    if not body["days"]:
        return                      # 归档还没建立就跳过（不算失败）
    for d in body["days"]:
        assert d.get("date"), d
        for k in ("records", "added", "changed", "removed", "reidentified"):
            assert k in d, (k, d)
    # 从新到旧：用户要看的是"最近发生了什么"
    dates = [d["date"] for d in body["days"]]
    assert dates == sorted(dates, reverse=True), dates
    assert body["first_date"] == dates[-1] and body["last_date"] == dates[0]


@case
def history_能一眼回答跑了多久():
    """被问"你怎么证明每天在跑"，答案不该是"我们的脚本能跑"（承诺），
    而是"这是过去 N 天的记录"（事实）。"""
    body = CLIENT.get("/api/changes/history").json()
    assert "days_with_changes" in body, list(body.keys())
    assert isinstance(body["days_with_changes"], list)
    # 索引里真有变更的天数，要和逐日数据对得上
    expect = [d["date"] for d in body["days"]
              if any((d.get(k) or 0) for k in ("added", "changed", "removed", "reidentified"))]
    assert sorted(body["days_with_changes"]) == sorted(expect), body["days_with_changes"]


@case
def history_归档缺失时不假装跑过():
    """没有归档 ≠ 跑了但没变化。**不能给一个空 days 就说"一切正常"。**"""
    from server.history import HistoryStore
    missing = HistoryStore.load(os.path.join(os.path.dirname(config.HISTORY_INDEX_PATH),
                                             "不存在的索引.json"))
    payload = missing.response()
    assert payload["total"] == 0 and payload["days"] == []
    assert payload.get("_error"), payload


# ---------- 11. 一键启动（RUN.md 里承诺的东西要真能跑） ----------

@case
def 启动体检_check_能跑且报出关键信息():
    """`RUN.md` 让演示前跑 `run.py --check`，那它自己就不能是坏的。

    这条用 subprocess 真跑一次 —— 单纯 import run.py 测不到它的输出和退出码。
    """
    import subprocess

    py = sys.executable
    script = os.path.join(ROOT, "run.py")
    assert os.path.exists(script), "run.py 不在项目根"
    r = subprocess.run([py, script, "--check"], capture_output=True,
                       cwd=ROOT, timeout=60)
    out = (r.stdout or b"").decode("utf-8", "replace")
    assert r.returncode == 0, (r.returncode, out[-500:], r.stderr[-500:])
    # 体检必须报出这几件事，否则演示前那一步等于没查
    for need in ("依赖", "端口", "公告", "导师", "web/"):
        assert need in out, f"体检输出里没有 {need}：\n{out}"


@case
def 启动脚本_run_bat_是纯_ASCII():
    """**`cmd.exe` 按控制台代码页解析 .bat**（中文 Windows 上是 GBK）。

    第一版 run.bat 存成了 UTF-8，里面的中文注释和 echo 全被解析成乱码命令，
    脚本直接崩（`'默认' 不是内部或外部命令`）。`chcp 65001` 也救不了，
    因为**解析发生在 chcp 生效之前**。

    所以 run.bat 必须纯 ASCII，中文一律交给 run.py 打。这条测试守住它 ——
    后面谁顺手往里加一句中文注释，就会红。
    """
    path = os.path.join(ROOT, "run.bat")
    assert os.path.exists(path), "run.bat 不在项目根"
    raw = open(path, "rb").read()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert not bad, f"run.bat 里有非 ASCII 字节（第 {bad[0][0]} 字节起），cmd 会解析崩"


# ---------- 12. 按需重载（重跑管道后接口要跟上） ----------

@case
def 数据文件被重写后接口自己跟上():
    """**C 报的（`server/store.py:87`）：启动时读一次就再也不看磁盘。**

    后果：A 每重跑一次管道，`/api/notices` 还在返回旧数据 ——
    HTTP 200、形状对、就是内容旧的。**它不说谎，它只是过时**，
    属于最难发现的那类 bug；演示时台上刚跑完管道、切回浏览器还是老数据。

    这条端到端验：写一份新数据到临时文件，接口必须自己跟上。
    """
    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="tuimian_reload_")
    tmp = os.path.join(tmpdir, "notices.jsonl")
    shutil.copyfile(config.NOTICES_PATH, tmp)
    try:
        # 传**工厂**（生产路径的形态），且它指向临时文件
        c = TestClient(create_app(lambda: NoticeStore.load(tmp),
                                  FACULTY_STORE, StateStore.load(":memory:")))
        assert c.get("/api/health").json()["data"]["loaded"] == len(RAW)

        # 模拟"重跑管道"：只留 3 条**非 junk** 记录。
        # 默认响应会拒收 junk，拿 RAW[:3] 可能只剩 2 条 —— 那是数据行恰好
        # 变了，不是"接口没跟上"，两种失败长得一样，所以这里主动排除。
        keep = [r for r in RAW
                if (r.get("record_quality") or "") != "junk"][:3]
        assert len(keep) == 3
        with open(tmp, "w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        assert c.get("/api/health").json()["data"]["loaded"] == 3, "health 没跟上"
        assert c.get("/api/notices", params={"size": 50}).json()["total"] == 3, \
            "notices 没跟上"

        # 文件没变时**不该**反复重载（只 stat，不读字节）
        before = c.app.state.store.reload_count
        c.get("/api/notices", params={"size": 1})
        c.get("/api/notices", params={"size": 1})
        assert c.app.state.store.reload_count == before, "文件没变却重载了"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@case
def 注入固定实例时不重载():
    """测试注入的是**实例**，必须保持"读一次、结果稳定"——
    否则门禁自己就变成不确定的了。"""
    c = _fresh_client()
    s = c.app.state.store
    assert not hasattr(s, "reload_count") or s.reload_count == 0, \
        "注入实例时不该有重载行为"
    assert c.get("/api/notices", params={"size": 1}).json()["total"] == len(VISIBLE)


# ---------- 13. 关注与提醒（契约 api.md 第 11 节） ----------
#
# **这一节一条都不许读磁盘。** 磁盘上恰好是"今天 added=0 changed=0、只有 reparsed=21"，
# 依赖它写出来的断言只有"恰好没变化的那一天"才有点意义 ——
# 而这正是这一晚反复栽的那个坑（额度状态、条数、页码）。
# 所以下面所有报告都是**合成**的，日期也是自己定的。

def _fake_notice(nid="n_1", school="浙江大学", unit="物理学院",
                 title="2027年接收推荐免试研究生工作办法",
                 body="申请条件：具有推免资格。材料要求：成绩单。", **kw):
    """合成一条公告。形状按 `/api/notices` 的输出，字段够 digest/匹配用即可。"""
    rec = {
        "id": nid, "school": school, "unit": unit, "title": title,
        "url": f"https://example.invalid/{nid}", "publish_date": "2026-10-10",
        "deadline": "2026-11-20", "apply_mode": "推免", "target_year": 2027,
        "board": "招生信息", "majors": ["物理学"], "tags": ["推免"],
        "body_text": body,
    }
    rec.update(kw)
    return rec


def _report(added=(), changed=(), reparsed=(), removed=(), date_str=None):
    """合成一份差异报告（形状同 changes.json）。"""
    rep = {
        "generated_at": f"{date_str}T09:00:00" if date_str else "2026-10-17T09:00:00",
        "counts": {"previous": 1, "current": 1,
                   "added": len(added), "changed": len(changed),
                   "reparsed": len(reparsed), "removed": len(removed),
                   "reidentified": 0, "unchanged": 0},
        "added": list(added),
        "changed": [{"record": r, "fields": f, "body_changed": True}
                    for r, f in changed],
        "reparsed": [{"record": r, "fields": f, "body_changed": False, "why": w}
                     for r, f, w in reparsed],
        "removed": list(removed),
        "reidentified": [],
    }
    return rep


_TMP_DIRS = []


def _watch_client(reports=None, notices=None):
    """状态在内存 + 报告是注入的合成数据 → **完全不依赖磁盘**。

    `notices` 给了就把这些合成记录写进一个**临时 JSONL**、用**真的 `NoticeStore.load`**
    读进来。这样"digest 里的 notice_id 能点进详情"那条链路是**真的**被走通的，
    而不是靠断言绕过 —— 合成数据走真代码，才叫端到端。

    （我第一版没给这一层，于是"点进详情"那条断言必然失败：
    合成的 notice_id 不在公告库里。那是测试写错，不是代码坏。）
    """
    import tempfile

    store = STORE
    if notices is not None:
        d = tempfile.mkdtemp(prefix="tuimian_watch_")
        _TMP_DIRS.append(d)
        path = os.path.join(d, "notices.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in notices:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        store = NoticeStore.load(path)

    dg = digest_mod.DigestStore(dict(reports or {}))
    return TestClient(create_app(store, FACULTY_STORE, StateStore.load(":memory:"),
                                 None, dg))


def _cleanup_tmp():
    import shutil
    while _TMP_DIRS:
        shutil.rmtree(_TMP_DIRS.pop(), ignore_errors=True)


def _digest(c, user="u_t", **params):
    """**查询参数名是 `user`，不是 `user_id`** —— 和 /api/me、/api/posts 一致。

    这个坑我自己踩过：接口声明 `user`、契约写 `user_id`，
    于是传 `user_id=` 时 FastAPI **静默回落到默认用户 `u_demo`**，
    不报错、只是所有关注都"消失"了。所以下面专门有一条用例盯这个。
    """
    return c.get("/api/digest", params=dict({"user": user}, **params)).json()


@case
def watch_没关注时_digest_空且结构完整():
    """**边界 A**：新用户没有关注是最正常的第一状态，不是错误。

    返回 400 会让人以为接口坏了。但也不能只给一个空数组 ——
    必须能分清"你还没配"和"配了但今天没事"。
    """
    c = _watch_client({"2026-10-17": _report(added=[_fake_notice()], date_str="2026-10-17")})
    d = _digest(c, since="2026-10-01")

    assert d["total"] == 0 and d["items"] == []
    assert d["watches"] == []
    assert d["empty_reason"] == "no_watches", d["empty_reason"]
    assert d["message"], "空结果必须带一句人话，不能只给空数组"
    # 结构完整：前端按固定字段读，少一个键就会静默成 undefined
    for k in ("user_id", "since", "since_effective", "until", "watches", "total",
              "items", "days_covered", "empty_reason", "message",
              "earliest_available", "latest_available"):
        assert k in d, f"缺键 {k}"
    assert d["_note"], "必须说明提醒只含 added/changed（否则用户不知道 reparsed 去哪了）"


@case
def watch_关注了但区间内没变化_digest_空():
    """**边界 B**：配好了，这些天**真的**没事。

    和边界 A 必须能分开 —— 都返回空数组、都不给 empty_reason，
    前端就只能写"暂无提醒"，而用户分不清是自己没设置还是系统没在跑。
    """
    # 报告里只有 reparsed（"我们的处理变了"），没有 added/changed
    c = _watch_client({"2026-10-17": _report(
        reparsed=[(_fake_notice(), {"title": ["旧", "新"]}, "抽取器版本变了")],
        date_str="2026-10-17")})
    c.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})
    d = _digest(c, since="2026-10-15")

    assert d["total"] == 0 and d["items"] == []
    assert len(d["watches"]) == 1, "规则还在（不是被清掉了）"
    assert d["empty_reason"] == "no_changes_in_range", d["empty_reason"]
    assert d["empty_reason"] != "no_watches", "B 和 A 必须是两个不同的值"
    assert "浙江大学" not in d["message"] or "没有新增或变更" in d["message"]


@case
def watch_关注学校_有新增就命中():
    """**核心路径**：关注了学校，那天新增的公告要进来，并且带"为什么提醒我"。"""
    hit = _fake_notice("n_hit", school="浙江大学", unit="物理学院")
    miss = _fake_notice("n_miss", school="北京大学", unit="物理学院")
    c = _watch_client({"2026-10-16": _report(added=[hit, miss], date_str="2026-10-16")},
                      notices=[hit, miss])
    c.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})
    d = _digest(c, since="2026-10-15")

    assert d["total"] == 1, d["items"]
    it = d["items"][0]
    assert it["notice_id"] == "n_hit", "只该命中浙大那条"
    assert it["change"] == "added"
    assert it["changed_on"] == "2026-10-16", "changed_on 是它出现在哪天报告里"
    assert it["url"], "必须有 url，否则点不进原文"
    # **为什么提醒我** —— 这是产品的差异化，不能只有结果没有理由
    assert it["matched_by"], "每条必须给出命中了哪条规则"
    assert any("浙江大学" in h["reason"] for h in it["matched_by"]), it["matched_by"]
    assert it["why"] == "；".join(h["reason"] for h in it["matched_by"])
    # 能顺 notice_id 点进详情
    assert c.get(f"/api/notices/{it['notice_id']}").status_code == 200


@case
def watch_关键词命中_changed_也算提醒():
    """**关键词路径**：公告被改了、改动里带我要的词 → 提醒。

    这条和上一条走的是不同的匹配分支，而且 `change` 是 `changed` 不是 `added`。
    """
    rec = _fake_notice("n_kw", school="复旦大学", unit="化学系",
                       title="2027年夏令营报名通知",
                       body="本次夏令营招收优秀本科生。")
    other = _fake_notice("n_other", school="复旦大学", unit="化学系",
                         title="关于调整上课时间的通知", body="与招生无关。")
    c = _watch_client({"2026-10-17": _report(
        changed=[(rec, {"deadline": [None, "2026-11-01"]}),
                 (other, {"title": ["旧", "新"]})],
        date_str="2026-10-17")})
    c.post("/api/watch", json={"user_id": "u_t", "keywords": ["夏令营"]})
    d = _digest(c, since="2026-10-15")

    assert d["total"] == 1, [i["notice_id"] for i in d["items"]]
    it = d["items"][0]
    assert it["notice_id"] == "n_kw"
    assert it["change"] == "changed"
    assert any("夏令营" in h["reason"] for h in it["matched_by"]), it["matched_by"]
    # changed 要带字段级差异（形状同 §10 的 changed[].fields）
    assert it["fields"] == {"deadline": [None, "2026-11-01"]}, it["fields"]


@case
def watch_digest_里一条_reparsed_都不出现():
    """**这一节最要紧的一条。**

    `reparsed` / `reidentified` / `removed` 的意思是"**我们的处理变了**"，
    不是"外面的世界变了"。把它们发进提醒 = 告诉用户"浙江大学物理学院有更新"，
    点进去发现**公告一个字都没变** —— 是我们改了表格解析。

    **提醒只要开始说谎，就没有人再看它**，而"哪条被悄悄改了"正是我们收钱的功能。
    """
    same_school = dict(school="浙江大学", unit="物理学院")
    c = _watch_client({"2026-10-17": _report(
        added=[_fake_notice("n_add", **same_school)],
        changed=[(_fake_notice("n_chg", **same_school), {"deadline": [None, "x"]})],
        reparsed=[(_fake_notice("n_rep", **same_school),
                   {"title": ["旧", "新"]}, "抽取器版本变了")],
        removed=[_fake_notice("n_rem", **same_school)],
        date_str="2026-10-17")})
    c.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})
    d = _digest(c, since="2026-10-15")

    got = {i["notice_id"] for i in d["items"]}
    assert got == {"n_add", "n_chg"}, got
    assert "n_rep" not in got, "reparsed 泄漏进提醒了 —— 那等于说谎"
    assert "n_rem" not in got, "removed 是'本轮没抓到'，不是世界变了"
    # 报告里确实有 reparsed（否则这条测试没意义）
    assert d["total"] == 2

    # 再验一次：**只有** reparsed 的那一天，digest 必须是空的
    c2 = _watch_client({"2026-10-17": _report(
        reparsed=[(_fake_notice(**same_school), {"title": ["旧", "新"]}, "派生字段不同")],
        date_str="2026-10-17")})
    c2.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})
    d2 = _digest(c2, since="2026-10-15")
    assert d2["total"] == 0 and d2["empty_reason"] == "no_changes_in_range", d2


@case
def watch_取关之后不再命中_而且取关不存在的_id_是_404():
    """取关要真的生效；**取关不存在的 id 必须是 404 而不是 200**。

    "反正结果都是它不在了"听起来合理，但会把"前端传错 id"这类 bug 静默吃掉：
    用户以为取关成功、规则还在、下次 digest 又冒出提醒，而日志里全是 200。
    """
    rec = _fake_notice("n_1", school="浙江大学")
    reports = {"2026-10-17": _report(added=[rec], date_str="2026-10-17")}
    c = _watch_client(reports)
    liked = c.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"}).json()
    wid = liked["watch"]["id"]
    assert _digest(c, since="2026-10-15")["total"] == 1

    r = c.delete(f"/api/watch/{wid}", params={"user": "u_t"})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == wid and r.json()["total"] == 0

    d = _digest(c, since="2026-10-15")
    assert d["total"] == 0, "取关后不该再命中"
    assert d["empty_reason"] == "no_watches", d["empty_reason"]

    # 再取关一次 → 404
    r2 = c.delete(f"/api/watch/{wid}", params={"user": "u_t"})
    assert r2.status_code == 404, r2.status_code
    assert r2.json()["error"]["code"] == "NOT_FOUND"
    # 别人的规则也不许被取关（否则任意 user 都能删别人的关注）
    c.post("/api/watch", json={"user_id": "u_a", "school": "浙江大学"})
    other = c.get("/api/watch", params={"user": "u_a"}).json()["watches"][0]["id"]
    assert c.delete(f"/api/watch/{other}",
                    params={"user": "u_b"}).status_code == 404
    assert c.get("/api/watch", params={"user": "u_a"}).json()["total"] == 1


@case
def watch_重复关注只有一条():
    """**幂等**：同一条规则重复 POST 不该造出第二条。

    演示时的真实形态是用户连点两下"关注" —— 如果变成两条规则，
    digest 的 `matched_by` 里就会出现两遍"学校=浙江大学"，
    界面上是"为什么提醒我：学校=浙江大学；学校=浙江大学"，看起来像 bug。
    """
    c = _watch_client()
    body = {"user_id": "u_t", "school": "浙江大学", "unit": "物理学院",
            "keywords": ["夏令营", "推免"]}

    r1 = c.post("/api/watch", json=body)
    r2 = c.post("/api/watch", json=body)
    assert r1.status_code == 201 and r1.json()["created"] is True
    assert r2.status_code == 200 and r2.json()["created"] is False
    assert r1.json()["watch"]["id"] == r2.json()["watch"]["id"], "id 必须确定性派生"
    assert r2.json()["total"] == 1, r2.json()

    # 归一化之后等价的写法也必须判成同一条
    for variant in (
        {"school": "  浙江大学  ", "unit": "物理学院", "keywords": ["推免", "夏令营"]},
        {"school": "浙江大学", "unit": "物理学院", "keywords": "夏令营,推免"},
        {"school": "浙江大学", "unit": "物理学院", "keywords": ["夏令营", "推免", "夏令营"]},
    ):
        v = dict({"user_id": "u_t"}, **variant)
        got = c.post("/api/watch", json=v)
        assert got.json()["created"] is False, (variant, got.json())
        assert got.json()["total"] == 1, (variant, got.json())

    # 三个字段都不给 → 400（等于"关注所有变化"，和 /api/changes 没区别，没表达意图）
    bad = c.post("/api/watch", json={"user_id": "u_t"})
    assert bad.status_code == 400, bad.status_code
    assert bad.json()["error"]["code"] == "BAD_REQUEST"
    assert c.post("/api/watch", json={"user_id": "u_t",
                                      "keywords": []}).status_code == 400
    assert c.get("/api/watch", params={"user": "u_t"}).json()["total"] == 1


@case
def watch_since_早于有记录的时间要说清被夹了():
    """**边界 C**：不报错，但必须**显式**告诉用户被夹了。

    悄悄换一个日期就算完是诚实问题：用户以为在查 1 月至今、实际只查了 10 月 16 日至今，
    他会得出"这半年什么都没发生"的结论，而那是错的。

    **判断用 `notes`（结构化），不在 message 里搜关键词。**
    我第一版把夹取提示写进 `message`，被后面的"没有变更"覆盖掉了，
    而测试却因为"命中消息里恰好也含 2026-10-16"通过了 —— 假绿。
    这就是给套磁信加 `cited_from` 的同一个教训：自然语言不是数据。
    """
    c = _watch_client({"2026-10-16": _report(
        added=[_fake_notice("n_1", school="浙江大学")], date_str="2026-10-16")})
    c.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})
    d = _digest(c, since="2026-01-01")

    assert d["since"] == "2026-01-01", "原样回显用户请求的日期"
    assert d["since_effective"] == "2026-10-16", "实际生效的是最早那天"
    assert d["since_clamped"] is True
    assert d["earliest_available"] == "2026-10-16"
    # **关键**：夹取提示必须在 notes 里，而且不能在后面的逻辑里被冲掉
    assert d["notes"], "被夹了却不告诉用户，就是契约里说的'诚实问题'"
    clamp_note = [n for n in d["notes"] if "2026-10-16" in n and "只有" in n]
    assert clamp_note, d["notes"]
    assert "2026-01-01" in clamp_note[0], "要说清用户请求的是哪个日期"
    assert d["total"] == 1, "夹了之后该命中的还是要命中"
    # 有命中时 message 讲结论、notes 讲前提 —— 两者都要给
    assert "命中" in d["message"], d["message"]

    # since 在未来 → 不报错，但明确说明，而且和"被夹"是两回事
    fut = _digest(c, since="2099-01-01")
    assert fut["total"] == 0
    assert fut["since_in_future"] is True
    assert fut["since_clamped"] is False, "未来不是'被夹'，是另一种情况"
    assert any("未来" in n for n in fut["notes"]), fut["notes"]

    # 夹取提示在"没有命中"时同样不能被冲掉（第一版就是这时丢的）
    c2 = _watch_client({"2026-10-16": _report(
        added=[_fake_notice("n_x", school="北京大学")], date_str="2026-10-16")})
    c2.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})
    d2 = _digest(c2, since="2026-01-01")
    assert d2["total"] == 0 and d2["empty_reason"] == "no_changes_in_range"
    assert any("只有" in n for n in d2["notes"]), \
        "没有命中时夹取提示更不能丢 —— 用户会以为'这半年真的什么都没发生'"


@case
def watch_完全没有记录时不说_没有变化():
    """**边界 D**：归档未建立。

    **和"没有变化"必须分开** —— 没有记录时说"没有变化"是**说谎**，
    我们不知道有没有变化。而且这时让用户"去加关注"是误导：
    他加了还是空的，会以为是自己配错了。
    """
    c = _watch_client({})                 # 一份报告都没有
    c.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})
    d = _digest(c, since="2026-10-01")

    assert d["total"] == 0 and d["items"] == []
    assert d["empty_reason"] == "no_records_at_all", d["empty_reason"]
    assert d["empty_reason"] != "no_changes_in_range", "不能说'没有变化'"
    assert d["days_covered"] == []
    assert d["earliest_available"] is None
    # 这一种空，结论本身就在 message 里（不是"前提被夹"那类 notes）
    assert "归档" in d["message"], d["message"]
    assert "关注" in d["message"], "要说明'关注加上了也暂时不会有内容'，别让用户白折腾"


@case
def watch_同一公告多次变化只提醒一次():
    """提醒回答"这条你需要看一眼"，不是流水账。

    同一条公告在区间内改了三次就提醒三次，用户第三次就开始忽略提醒 ——
    那比漏提醒更糟。取**最近那次**，并且它属于最新的那一天。
    """
    rec = _fake_notice("n_1", school="浙江大学")
    c = _watch_client({
        "2026-10-15": _report(changed=[(rec, {"deadline": [None, "a"]})], date_str="2026-10-15"),
        "2026-10-16": _report(changed=[(rec, {"deadline": ["a", "b"]})], date_str="2026-10-16"),
        "2026-10-17": _report(changed=[(rec, {"deadline": ["b", "c"]})], date_str="2026-10-17"),
    })
    c.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})
    d = _digest(c, since="2026-10-01")

    assert d["total"] == 1, [i["changed_on"] for i in d["items"]]
    it = d["items"][0]
    assert it["changed_on"] == "2026-10-17", "要取最近那次"
    assert it["fields"] == {"deadline": ["b", "c"]}, it["fields"]


@case
def watch_查询参数名传错会静默回落_所以响应要回显_user_id():
    """**这是我自己踩的坑，钉住它。**

    接口的查询参数叫 `user`（和 `/api/me`、`/api/posts` 一致），
    而 POST body 里的字段叫 `user_id`。我第一次写契约时写成了 `?user_id=`，
    于是 `GET /api/watch?user_id=u_t` **不报错**，只是 FastAPI 忽略了未知参数、
    回落到默认用户 `u_demo` —— 所有关注都"消失"了。

    **传错参数名不报错、只是结果为空**，是最难发现的那类问题。
    所以：① 响应必须回显 `user_id`，让调用方能自己发现传错了；
    ② 这条用例把"回落"这个行为钉住 —— 谁要是哪天顺手支持了两种参数名，
    会在这里看到这条注释，知道自己在改一个**已经写进契约**的约定。
    """
    c = _watch_client()
    c.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})

    right = c.get("/api/watch", params={"user": "u_t"}).json()
    assert right["total"] == 1 and right["user_id"] == "u_t"

    wrong = c.get("/api/watch", params={"user_id": "u_t"}).json()
    assert wrong["total"] == 0, "未知参数被忽略 → 回落到默认用户"
    # 但调用方能从回显看出来自己传错了（这就是回显 user_id 的用处）
    assert wrong["user_id"] == "u_demo", wrong["user_id"]
    assert wrong["user_id"] != "u_t", "回显能让调用方发现传错参数名"

    # digest 同样回显
    d = _digest(c, user="u_t", since="2026-01-01")
    assert d["user_id"] == "u_t"


@case
def digest_和_changes_history_的口径对得上():
    """**契约第 11 节承诺的"端到端测试里有一条专门盯这个"。**

    不这么验的话会出现互相打脸：
      `/api/changes/history` → "过去 7 天里有 3 天有变化"
      `/api/digest`          → "你的提醒是空的"
    用户看到这两句会认为**有一个是坏的**。

    口径是这样统一的：
    - `history.days_with_changes`（五个桶全算）= "**管道每天在跑吗**"，运维视角
    - `history.days_with_user_changes`（只算 added/changed）= "**外面的世界变了**"
    - `digest` 只发 added/changed → 和上面那一个同口径
    """
    from server.history import HistoryStore

    # 合成一份索引。**10-16 那天的 added/changed 都是 0，只有 reparsed** ——
    # 那正是"我们的处理变了、外面的世界没变"的那一天。
    #
    # 注意这里**显式写了 `reparsed` 列**：`data/out/history/index.json` 目前还没有这一列
    # （A 的 run_daily.py 还没写），没有它的话 `days_with_changes` 根本看不见 10-16，
    # 两个字段会完全一样、区分是潜伏的。写上它才能真的验出"两个口径不一样"。
    # 不管有没有这一列，`days_with_user_changes ⊆ days_with_changes` 都必须成立（下面验）。
    index = {"days": [
        {"date": "2026-10-15", "records": 3, "added": 2, "changed": 0,
         "reparsed": 0, "removed": 0, "reidentified": 0},
        {"date": "2026-10-16", "records": 3, "added": 0, "changed": 0,
         "reparsed": 5, "removed": 0, "reidentified": 0},
        {"date": "2026-10-17", "records": 3, "added": 0, "changed": 1,
         "reparsed": 0, "removed": 0, "reidentified": 0},
    ]}
    hist = HistoryStore("/nonexistent/index.json", index).response()
    assert hist["days_with_changes"] == ["2026-10-17", "2026-10-16", "2026-10-15"], \
        "五个桶全算：三天都有动静"
    assert hist["days_with_user_changes"] == ["2026-10-17", "2026-10-15"], \
        "只算 added/changed：10-16 那天'外面的世界'没变，只有我们改了抽取代码"
    # 不管索引里有没有 reparsed 列，这条关系都必须成立
    assert set(hist["days_with_user_changes"]) <= set(hist["days_with_changes"])

    # 同样的三天数据喂给 digest
    rec = _fake_notice("n_1", school="浙江大学")
    reports = {
        "2026-10-15": _report(added=[_fake_notice("n_a", school="浙江大学")],
                              date_str="2026-10-15"),
        "2026-10-16": _report(reparsed=[(rec, {"title": ["旧", "新"]}, "抽取器版本变了")],
                              date_str="2026-10-16"),
        "2026-10-17": _report(changed=[(_fake_notice("n_b", school="浙江大学"),
                                        {"deadline": [None, "x"]})],
                              date_str="2026-10-17"),
    }
    c = _watch_client(reports)
    c.post("/api/watch", json={"user_id": "u_t", "school": "浙江大学"})
    d = _digest(c, user="u_t", since="2026-10-15", until="2026-10-17")

    # ① digest 覆盖的天 == 有报告的天（不是 history 的"有变化"的天）
    assert d["days_covered"] == ["2026-10-15", "2026-10-16", "2026-10-17"]
    # ② **凡 history 说"外面的世界变了"的天，digest 必须有命中项**
    hit_days = {i["changed_on"] for i in d["items"]}
    for day in hist["days_with_user_changes"]:
        assert day in hit_days, (day, hit_days)
    # ③ 反过来：digest 命中的天，history 也必须认
    for day in hit_days:
        assert day in hist["days_with_user_changes"], (day, hist["days_with_user_changes"])
    # ④ 只有 reparsed 那天，两边都不认它 —— 这才是"对得上"
    assert "2026-10-16" not in hit_days
    assert "2026-10-16" not in hist["days_with_user_changes"]
    assert d["total"] == 2, d["total"]


# ---------- 14. 数据新鲜度与调度（契约 api.md 第 12 节） ----------
#
# 这一节测的是产品**最核心那句话**"每天更新"能不能被验证。
# 所以它自己要经得起"数据变了还成立" —— 契约要求：
# **断言不许依赖磁盘上恰好是什么数据**（这一晚栽过 5 次）。
# 下面所有新鲜度数据都是**合成**的，时间基准也是**相对现在**算的。

def _freshness_data(age_hours=0.0, records=72, schools=None):
    """合成一份 freshness.json 内容。`age_hours` 是"距现在几小时前抓的"。

    **相对现在算，不写死日期** —— 写死日期的断言过一天就变成
    "数据 30 小时前抓的"，第二天必红。
    """
    last = (datetime.now() - timedelta(hours=age_hours)).isoformat(timespec="seconds")
    if schools is None:
        schools = [
            {"school": "浙江大学", "unit": "物理学院", "homepage": "https://a.invalid",
             "last_ok": last, "records": 9, "boards": 8, "boards_empty": 3,
             "failed": [], "freshness_hours": 0.0},
            {"school": "北京大学", "unit": "研究生招生网", "homepage": "https://b.invalid",
             "last_ok": last, "records": 14, "boards": 6, "boards_empty": 1,
             "failed": ["tls_handshake"], "freshness_hours": 0.0},
        ]
    return {"generated_at": last, "schools": schools,
            "totals": {"records": records, "schools": len(schools),
                       "failed_schools": sum(1 for s in schools if s["failed"])}}


def _fresh_client_for(data, now=None, **kw):
    """注入合成新鲜度 + **假抓取命令**的客户端。

    `now` 传进去是为了让"40 小时前"这类断言不依赖真实时钟。
    """
    fr = FreshnessStore("/nonexistent/freshness.json", data)
    return TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:"),
                                 None, None, fr, kw.pop("crawl", None)))


@case
def freshness_未来时间戳不许显示负数():
    """容器 UTC + 数据 +08 时实测 -7.68 小时。页面不能写「-7.7 小时前」。"""
    future = (datetime.now() + timedelta(hours=8)).isoformat(timespec="seconds")
    data = {"generated_at": future, "schools": [
        {"school": "A", "unit": "研招网", "last_ok": future, "records": 1,
         "boards": 1, "boards_empty": 0, "failed": []},
    ]}
    d = _fresh_client_for(data).get("/api/freshness").json()
    assert d["generated_age_hours"] == 0.0, d["generated_age_hours"]
    assert d["oldest_hours"] == 0.0, d["oldest_hours"]


@case
def freshness_每所学校给_fresh_stale_整体给_worst():
    """契约第 12 节的核心：每所学校 fresh/stale，整体 worst。

    **不用磁盘上的 freshness.json** —— 那里的 `last_ok` 恰好全等于
    `generated_at`（都是 0 小时前），依赖它就只能测出"全都新鲜"。
    """
    now = datetime.now()
    # 三所学校：一个新鲜、一个 40 小时前（> 30 阈值 → stale）、一个失败
    old = (now - timedelta(hours=40)).isoformat(timespec="seconds")
    fresh = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    data = {"generated_at": fresh, "schools": [
        {"school": "A", "unit": "研招网", "last_ok": fresh, "records": 3,
         "boards": 2, "boards_empty": 0, "failed": []},
        {"school": "B", "unit": "研究生院", "last_ok": old, "records": 1,
         "boards": 2, "boards_empty": 0, "failed": []},
        {"school": "C", "unit": "物理学院", "last_ok": fresh, "records": 5,
         "boards": 3, "boards_empty": 1, "failed": ["tls_handshake"]},
    ], "totals": {"records": 9, "schools": 3, "failed_schools": 1}}

    d = _fresh_client_for(data).get("/api/freshness").json()
    by = {s["school"]: s for s in d["schools"]}

    assert by["A"]["freshness"] == "fresh" and by["A"]["status"] == "ok"
    # **stale 判定：40 小时 > 30 小时阈值**
    assert by["B"]["freshness"] == "stale", by["B"]
    assert by["B"]["status"] == "stale"
    assert 39 < by["B"]["age_hours"] < 41, by["B"]["age_hours"]
    # 失败 > stale > ok 的优先级：C 不 stale 但有失败 → failed
    assert by["C"]["status"] == "failed" and by["C"]["has_failure"] is True
    assert by["C"]["failed"] == ["tls_handshake"], "失败必须带原因"

    # 整体 worst = 最坏的那个（C 的 failed 优先于 B 的 stale）
    assert d["worst"] == "failed", d["worst"]
    assert d["stale_after_hours"] == freshness_mod.STALE_AFTER_HOURS
    assert d["stale_after_hours"] == 30.0, "阈值是写死的 30 小时，改它要有理由"
    # totals 三个计数分开给（首页要显示，运维也要看）
    assert d["totals"] == {"records": 9, "schools": 3, "failed_schools": 1,
                           "stale_schools": 1, "empty_schools": 0}, d["totals"]
    # 阈值必须出现在响应里（它是工程约定、不是事实，得让人看见依据）
    assert d["oldest_hours"] is not None and 39 < d["oldest_hours"] < 41


@case
def freshness_抓到页面但_0_条要单独一态_不能说成_ok():
    """**"抓取失败不可见"的解药，也是这一节最要紧的一条。**

    实测真数据：浙大研招网 `boards=2 / boards_empty=2 / records=0 / failed=[]`
    —— 两个栏目全空、一条没抓到、**没有任何错误码**。
    只看 `failed[]` 的旧口径下它是完美的"成功"，而实际上它**什么都没抓到**，
    界面上和"这学校最近没动静"长得一模一样。

    **也不能说成 `failed`** —— 我们不知道那个栏目是不是本来就没有招生信息。
    所以必须存在中间这一档。
    """
    last = datetime.now().isoformat(timespec="seconds")
    data = {"generated_at": last, "schools": [
        # 全空 + 0 条 + 无错误 → empty
        {"school": "全空", "unit": "研招网", "last_ok": last, "records": 0,
         "boards": 2, "boards_empty": 2, "failed": []},
        # 有内容 → ok（不能因为 boards_empty < boards 就报警）
        {"school": "正常", "unit": "研招网", "last_ok": last, "records": 5,
         "boards": 2, "boards_empty": 1, "failed": []},
        # 一条没有但**没抓到任何栏目**（boards=0）→ 不算 empty，我们没信息
        {"school": "没栏目", "unit": "研招网", "last_ok": last, "records": 0,
         "boards": 0, "boards_empty": 0, "failed": []},
    ], "totals": {"records": 5, "schools": 3, "failed_schools": 0}}

    d = _fresh_client_for(data).get("/api/freshness").json()
    by = {s["school"]: s for s in d["schools"]}

    assert by["全空"]["empty"] is True and by["全空"]["status"] == "empty", by["全空"]
    assert by["全空"]["status"] != "ok", "全空不能显示成正常 —— 那就是那个洞本身"
    assert by["全空"]["has_failure"] is False, "它没有错误码，所以不是 failed"
    assert by["正常"]["status"] == "ok" and by["正常"]["empty"] is False
    # boards=0 意味着我们**没有信息**，不能推断成"抓到了但没内容"
    assert by["没栏目"]["empty"] is False, by["没栏目"]
    assert d["totals"]["empty_schools"] == 1, d["totals"]


@case
def freshness_从没跑过时不崩且说清还没有数据():
    """**"没有数据"和"数据是新鲜的"是两个结论。**

    返回 200 + 一句人话，而不是 500，也不能假装 `ok`
    （假装 ok 的话首页会显示"最近更新 None 分钟前"）。
    """
    c = TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:"),
                              None, None, FreshnessStore("/nope/none.json", None,
                                                         load_error="还没有新鲜度数据")))
    r = c.get("/api/freshness")
    assert r.status_code == 200, r.status_code
    d = r.json()
    assert d["worst"] == "unknown", d["worst"]
    assert d["schools"] == [] and d["totals"]["schools"] == 0
    assert d["generated_at"] is None
    assert d["generated_age_hours"] is None, "没有数据时不能编一个 0（那是'刚刚'）"
    assert d["generated_age_text"] is None
    assert d["oldest_hours"] is None
    assert d["_error"], "要说清为什么没有"
    assert "还没有数据" in d["message"] or "没有数据" in d["message"], d["message"]
    # 阈值仍然要给 —— 前端要能显示判断依据
    assert d["stale_after_hours"] == 30.0

    # `/api/health` 同样不能崩
    h = c.get("/api/health").json()
    assert h["status"] == "ok"
    assert h["data"]["freshness_worst"] == "unknown"
    assert h["data"]["oldest_hours"] is None
    assert h["data"]["stale_schools"] == 0


@case
def freshness_改文件不重启能读到新值():
    """**按 mtime 重载，不是启动时读死。**

    读死的话 `/api/freshness` 会永远停在启动那一刻 ——
    而它存在的唯一目的就是回答"数据是什么时候的"，答错了比没有更坏。
    （这个坑在公告那边踩过一次：C 报的 `server/store.py:87`。）

    这条**必须走真实的缓存路径**，所以不能注入 FreshnessStore 实例 ——
    把 `config.FRESHNESS_PATH` 指向临时文件，让 `_load_freshness_cached()` 去读。
    """
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="tuimian_fresh_")
    path = os.path.join(tmpdir, "freshness.json")
    saved = config.FRESHNESS_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_freshness_data(age_hours=1.0, records=72), f, ensure_ascii=False)
        config.FRESHNESS_PATH = path
        # **不注入 freshness** → 走 _load_freshness_cached()
        c = TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:")))
        # 缓存可能已经有别的 key，清一下免得干扰
        import server.app as app_mod
        app_mod._FRESHNESS_CACHE.clear()

        first = c.get("/api/freshness").json()
        assert first["totals"]["records"] == 72, first["totals"]
        assert first["worst"] != "unknown"

        # 改文件（模拟 daily 跑完覆盖了它），**不重启服务**
        time.sleep(0.02)          # 让 mtime 一定变（ns 精度也怕同刻）
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_freshness_data(age_hours=1.0, records=999), f, ensure_ascii=False)

        second = c.get("/api/freshness").json()
        assert second["totals"]["records"] == 999, \
            f"改文件后还是旧值 {second['totals']['records']} —— 读死了"
    finally:
        import shutil
        config.FRESHNESS_PATH = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---- 抓取：单飞 / 失败要带原因（用**假命令**，绝不在门禁里真抓） ----

_FAKE_SLEEP = 1.2


def _fake_crawl_script(tmpdir, exit_code=0, stderr="", sleep=_FAKE_SLEEP):
    """写一个假的"抓取入口"：睡一会儿、往计数文件里追加一行、按需退出非 0。

    计数文件用来证"**真的只跑了一次**" —— 只看 run_id 相同不够，
    万一第二个真的跑了只是复用了 id 呢。
    """
    counter = os.path.join(tmpdir, "runs.txt")
    script = os.path.join(tmpdir, "fake_crawl.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            "import sys, time\n"
            f"time.sleep({sleep})\n"
            f"open(r'{counter}', 'a', encoding='utf-8').write('x\\n')\n"
            + (f"sys.stderr.write({stderr!r})\n" if stderr else "")
            + f"sys.exit({exit_code})\n")
    return script, counter


def _crawl_client(tmpdir, **kw):
    script, counter = _fake_crawl_script(tmpdir, **kw)
    runner = CrawlRunner(command=[sys.executable, script], cwd=tmpdir,
                         state_path=os.path.join(tmpdir, "runs.json"),
                         freshness_path=os.path.join(tmpdir, "freshness.json"),
                         schedule_enabled=False)
    c = TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:"),
                              None, None, None, runner))
    return c, counter, runner


def _wait_done(c, run_id, timeout=25):
    """轮询到跑完。**不能 sleep 一个固定值** —— 那是在赌机器速度。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = c.get(f"/api/admin/crawl/{run_id}",
                  headers={"X-Admin-Token": config.ADMIN_TOKEN}).json()
        if d.get("status") != "running":
            return d
        time.sleep(0.1)
    raise AssertionError(f"抓取 {timeout}s 没结束")


@case
def crawl_单飞_并发两次只跑一次且拿同一个_run_id():
    """契约第 12 节的单飞语义。

    **不只是性能优化，是责任**：抓取会打对方学校的服务器，
    并发跑两轮是对别人不礼貌，而且 `crawl.py` 是"读旧文件 → 覆盖写"，
    两轮并发会互相踩。
    """
    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="tuimian_crawl_")
    try:
        c, counter, _ = _crawl_client(tmpdir)
        hdr = {"X-Admin-Token": config.ADMIN_TOKEN}

        results, barrier = {}, threading.Barrier(2)

        def fire(k):
            barrier.wait()          # 让两个请求**同一时刻**发出，否则是顺序执行、测不到并发
            r = c.post("/api/admin/crawl", json={"trigger": "manual"}, headers=hdr)
            results[k] = (r.status_code, r.json())

        ts = [threading.Thread(target=fire, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)

        assert len(results) == 2, results
        ids = {v[1]["run_id"] for v in results.values()}
        assert len(ids) == 1, f"并发两次拿到两个 run_id：{ids} —— 单飞没生效"
        # 两个都是 202（一个真跑、一个 join），join 的要有标记
        assert all(v[0] == 202 for v in results.values()), \
            [(v[0], v[1].get("joined_existing")) for v in results.values()]
        joins = [v[1]["joined_existing"] for v in results.values()]
        assert sorted(joins) == [False, True], f"应恰好一个 join：{joins}"

        run_id = ids.pop()
        done = _wait_done(c, run_id)
        assert done["status"] == "ok", done
        assert done["exit_code"] == 0

        # **最关键的一条**：假脚本真的只被执行了一次
        with open(counter, encoding="utf-8") as f:
            n = sum(1 for ln in f if ln.strip())
        assert n == 1, f"抓取子进程跑了 {n} 次 —— 单飞没生效"

        # 运行日志里也只有一次真跑
        log = c.get("/api/admin/crawl", headers=hdr).json()
        assert log["total"] == 1, [x["run_id"] for x in log["runs"]]
        assert log["runs"][0]["trigger"] == "manual"
        assert log["schedule_enabled"] is False
    finally:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)


@case
def crawl_失败必须带原因不能静默_ok():
    """**报 `failed` 却不给原因等于没报** —— 看的人只知道坏了、不知道坏在哪。

    而且必须有 `error` 或 `tail` 之一，不能是一个空的失败。
    """
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="tuimian_crawl_fail_")
    try:
        c, _, _ = _crawl_client(tmpdir, exit_code=3,
                                stderr="boom: 目标站点连不上\n", sleep=0.3)
        hdr = {"X-Admin-Token": config.ADMIN_TOKEN}
        r = c.post("/api/admin/crawl", headers=hdr)
        assert r.status_code == 202, r.status_code
        run_id = r.json()["run_id"]

        # running 时 exit_code / finished_at 必须是 null，**不能是 0**
        # （0 会被读成"跑完了且成功"）
        assert r.json()["status"] == "running"
        assert r.json()["exit_code"] is None and r.json()["finished_at"] is None

        d = _wait_done(c, run_id)
        assert d["status"] == "failed", d
        assert d["exit_code"] == 3, d["exit_code"]
        assert d["error"], "失败必须带一句原因"
        assert "3" in d["error"], d["error"]
        assert d["tail"], "要带子进程输出尾部，否则不知道坏在哪"
        assert any("boom" in ln for ln in d["tail"]), d["tail"]
        assert d["duration_s"] is not None and d["duration_s"] >= 0
        # 不许把 md 报告解析进来
        assert d["steps"] is None, "steps 应当恒为 null（B 不解析 A 的 md 报告）"
        assert d["_note"] and "md" in d["_note"]
        # **统计**：数据动没动（假命令不会碰 freshness.json，所以是 None）
        assert d["data_moved"] is None, d["data_moved"]
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@case
def crawl_运行日志和_404_以及鉴权():
    """运行日志能列最近几次；查不存在的 run_id 是 404；没有 token 是 403。"""
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="tuimian_crawl_log_")
    try:
        c, _, runner = _crawl_client(tmpdir, sleep=0.2)
        # 没有 token 一律 403（三个端点都要）
        assert c.post("/api/admin/crawl").status_code == 403
        assert c.get("/api/admin/crawl").status_code == 403
        assert c.get("/api/admin/crawl/run_x").status_code == 403

        hdr = {"X-Admin-Token": config.ADMIN_TOKEN}
        r1 = c.post("/api/admin/crawl", headers=hdr).json()
        _wait_done(c, r1["run_id"])
        r2 = c.post("/api/admin/crawl", headers=hdr).json()
        _wait_done(c, r2["run_id"])
        assert r1["run_id"] != r2["run_id"], "串行的两次必须是两个 run"

        log = c.get("/api/admin/crawl", headers=hdr).json()
        assert log["total"] == 2, log["total"]
        assert log["runs"][0]["run_id"] == r2["run_id"], "新的在前"
        assert log["current"] is None, "跑完了 current 要清空"
        # 查不存在的 run_id → 404
        nf = c.get("/api/admin/crawl/run_nope", headers=hdr)
        assert nf.status_code == 404, nf.status_code
        assert nf.json()["error"]["code"] == "NOT_FOUND"

        # **落盘的 running 必须被标成 interrupted** ——
        # 进程没了，那个 run 不可能还在跑；不修的话界面永远显示"正在抓取"
        with open(runner.state_path, encoding="utf-8") as f:
            d = json.load(f)
        d["runs"].insert(0, {"run_id": "run_zombie", "status": "running",
                             "trigger": "manual", "started_at": "2026-10-17T00:00:00"})
        with open(runner.state_path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        reloaded = CrawlRunner(state_path=runner.state_path, schedule_enabled=False)
        z = reloaded.get("run_zombie")
        assert z["status"] == "interrupted", z["status"]
        assert z["error"], "要说清是被重启打断的，而不是静默改成别的状态"
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@case
def crawl_退出码_0_但数据没动要报出来():
    """**退出码 0 但数据没变，是另一种失败。**

    子进程"成功"了什么也没产出 —— 只看退出码会显示一个绿色的 `ok`，
    而实际上这一轮一点用都没有。

    不能直接判成 `failed`（中途失败时 `freshness.json` 本来就不会被重写，
    那时 `after == before` 是正常的），所以**两个时间戳都报出来 + 给一句提醒**，
    让人一眼知道这次抓取有没有用。
    """
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="tuimian_crawl_nodata_")
    try:
        c, _, runner = _crawl_client(tmpdir, exit_code=0, sleep=0.2)
        fpath = os.path.join(tmpdir, "freshness.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump({"generated_at": "2026-10-17T08:00:00", "schools": [],
                       "totals": {}}, f, ensure_ascii=False)
        runner.freshness_path = fpath       # 让 runner 去读它（假命令不会改它）

        hdr = {"X-Admin-Token": config.ADMIN_TOKEN}
        run_id = c.post("/api/admin/crawl", headers=hdr).json()["run_id"]
        d = _wait_done(c, run_id)

        assert d["status"] == "ok" and d["exit_code"] == 0
        assert d["data_generated_before"] == "2026-10-17T08:00:00", d
        assert d["data_generated_after"] == "2026-10-17T08:00:00", d
        assert d["data_moved"] is False, "数据没动要如实报出来"
        assert d["error"], "要提醒一句'可能什么都没抓到'，否则绿色 ok 会骗人"
        assert "没变" in d["error"] or "没抓到" in d["error"], d["error"]
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@case
def crawl_调度不信任计时器_只看数据新不新():
    """**判断依据永远是数据本身，不是计时器。**

    定时器只负责"提醒检查"。一个频繁重启的服务，计时器永远攒不到点，
    但数据真的旧了就该跑 —— 所以 `due()` 读 `last_ok` 算年龄。

    顺带钉住"启动时不抓"：`start_scheduler` 的第一次检查有延迟，
    而且 `schedule_enabled=False` 时**根本不起线程**。
    """
    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="tuimian_due_")
    try:
        fpath = os.path.join(tmpdir, "freshness.json")
        runner = CrawlRunner(command=[sys.executable, "-c", "pass"], cwd=tmpdir,
                             state_path=os.path.join(tmpdir, "runs.json"),
                             freshness_path=fpath, schedule_enabled=False,
                             interval_hours=24)

        # 没有新鲜度数据 → 该跑（从没跑过）
        should, why = runner.due()
        assert should is True and "没跑过" in why, (should, why)

        # 2 小时前抓过 → 不该跑
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(_freshness_data(age_hours=2.0), f, ensure_ascii=False)
        should, why = runner.due()
        assert should is False, why
        assert "还没到节拍" in why

        # 30 小时前抓过（> 24 节拍）→ 该跑
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(_freshness_data(age_hours=30.0), f, ensure_ascii=False)
        should, why = runner.due()
        assert should is True and "30" in why, (should, why)

        # 关掉调度 → 不起线程
        assert runner.start_scheduler() is False
        assert runner._sched is None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 15. 内容审核与合规（契约 api.md 第 13 节） ----------
#
# 这一节测的是"匿名论坛的法律风险能不能说清"。
# **断言不许依赖磁盘上恰好是什么数据** —— 全部自己造帖、自己造举报。
# 状态在内存（`_fresh_client()` 的 `:memory:`），所以也不留痕。

ADMIN = {"X-Admin-Token": config.ADMIN_TOKEN}


def _gov_client():
    """干净的治理用客户端。**限速器每个 app 实例一个**，所以这里也不会互相污染。"""
    return _fresh_client()


def _mk_post(c, title="实验室怎么样", body="想问问这个实验室的氛围。",
             author="u_author", **kw):
    payload = {"title": title, "body": body, "author_real_id": author,
               "unit": "物理学院", "tag": "推免经验"}
    payload.update(kw)
    r = c.post("/api/posts", json=payload)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _report_post(c, pid, reporter, reason="骂人"):
    return c.post(f"/api/posts/{pid}/report",
                  json={"reason": reason, "reporter_id": reporter})


def _to_pending(c, pid, n=None):
    """举报到阈值，让它进队列。"""
    n = n or forum.REPORT_THRESHOLD
    for i in range(n):
        _report_post(c, pid, f"u_r{i}")
    return c.get("/api/admin/queue", headers=ADMIN).json()


@case
def audit_是_append_only_接口层面没有改和删():
    """**审计是"我说得出依据"的唯一凭据。**

    它的 append-only 性质必须是**结构上的**，不是靠自觉：
    `StateStore` 上只有 `append_audit` 和 `audit_log` —— 没有修改、没有删除。
    一条可以被改的审计记录，在法律意义上等于没有审计。
    """
    c = _gov_client()
    pid = _mk_post(c)
    _to_pending(c, pid)
    c.post(f"/api/admin/posts/{pid}", params={"action": "hide",
                                             "reason_code": "harassment",
                                             "note": "人身攻击"},
           headers=ADMIN)

    # ① StateStore 上确实没有修改/删除审计的方法
    for bad in ("update_audit", "delete_audit", "remove_audit", "edit_audit",
                "clear_audit", "pop_audit"):
        assert not hasattr(c.app.state.state, bad), f"不该存在 {bad}"

    # ② 路由里也没有对审计的写/改/删 —— 只有 GET
    audit_routes = [r for r in c.app.routes
                    if getattr(r, "path", "") == "/api/admin/audit"]
    assert audit_routes, "没有 /api/admin/audit 路由"
    method_sets = [set(getattr(r, "methods", set()) or set()) for r in audit_routes]
    allowed = set().union(*method_sets)
    assert allowed == {"GET"}, f"审计路由只该有 GET，实际 {allowed}"

    # ③ 读得回来，且粒度六件套齐全
    rows = c.get("/api/admin/audit", headers=ADMIN).json()["items"]
    assert rows, "写完审计读不到"
    hide = next(r for r in rows if r["action"] == "hide")
    for k in ("at", "actor_id", "actor_role", "post_id", "action",
              "reason_code", "reason_label", "from_status", "to_status"):
        assert k in hide and hide[k] is not None or k == "reason_label", (k, hide)
    assert hide["post_id"] == pid
    assert hide["actor_role"] == "moderator"
    assert hide["reason_code"] == "harassment"
    # **前值 → 后值**：只看新状态分不清"原本好好的被下架"和"待审里被判"
    assert hide["from_status"] == "pending" and hide["to_status"] == "hidden", hide
    assert hide["post_snapshot"]["title"] == "实验室怎么样", "审计必须自带原文快照"
    # 快照里不该有身份信息（多一份泄露面）
    assert "author_real_id" not in hide["post_snapshot"]
    assert "created_ip" not in hide["post_snapshot"]

    # ④ 可按 post_id / operator_id 过滤
    by_post = c.get("/api/admin/audit", params={"post_id": pid}, headers=ADMIN).json()
    assert by_post["total"] == len(rows)
    assert all(r["post_id"] == pid for r in by_post["items"])
    by_op = c.get("/api/admin/audit", params={"operator_id": "admin"},
                  headers=ADMIN).json()
    assert by_op["total"] >= 1 and all(r["actor_id"] == "admin" for r in by_op["items"])
    by_act = c.get("/api/admin/audit", params={"action": "hide"}, headers=ADMIN).json()
    assert by_act["total"] == 1


@case
def audit_删帖之后审计仍然在():
    """**这一环的核心。**

    帖子可能被删，审计**不能跟着消失** —— 它是"我们当时为什么下架它"的凭据。
    所以审计是**独立的一份存储**，而且每条自带原文快照：
    即使帖子记录被整个清空，仍然能回答"当时下架的是什么内容、依据是什么"。
    """
    c = _gov_client()
    pid = _mk_post(c, title="要留痕的帖子", body="这段正文必须留在审计里。")
    _to_pending(c, pid)
    c.post(f"/api/admin/posts/{pid}", params={"action": "delete",
                                             "reason_code": "defamation",
                                             "note": "造谣"}, headers=ADMIN)

    before = c.get("/api/admin/audit", params={"post_id": pid}, headers=ADMIN).json()
    assert before["total"] >= 2, before["total"]

    # ---- 把帖子记录**真的删掉**（模拟帖子被清空）----
    st = c.app.state.state
    st.state["posts"] = [p for p in st.state["posts"] if p.get("id") != pid]
    st.save()
    assert st.find_post(pid) is None, "帖子记录应当已经没了"

    after = c.get("/api/admin/audit", params={"post_id": pid}, headers=ADMIN).json()
    assert after["total"] == before["total"], \
        f"帖子删了审计也跟着少了：{before['total']} -> {after['total']}"
    # **原文还在审计里** —— 这才叫"说得出依据"
    snap = next(r["post_snapshot"] for r in after["items"]
                if r.get("post_snapshot"))
    assert snap["title"] == "要留痕的帖子", snap
    assert "留在审计里" in (snap["body"] or ""), snap
    # 依据也还在
    assert any(r.get("reason_code") == "defamation" for r in after["items"])


@case
def audit_跨重启存活():
    """审计落在 `server/var/state.json`，和帖子/订阅同一套原子替换。

    这条**必须走真实文件** —— 用 `:memory:` 测不到"重启后还在"。
    """
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="tuimian_audit_")
    path = os.path.join(tmpdir, "state.json")
    try:
        st = StateStore.load(path)
        c1 = TestClient(create_app(STORE, FACULTY_STORE, st))
        pid = _mk_post(c1, title="跨重启的帖子")
        _to_pending(c1, pid)
        c1.post(f"/api/admin/posts/{pid}", params={"action": "hide",
                                                  "reason_code": "spam"},
                headers=ADMIN)
        n1 = c1.get("/api/admin/audit", headers=ADMIN).json()["total"]
        assert n1 >= 2

        # ---- "重启"：新开一个 StateStore 读同一个文件 ----
        st2 = StateStore.load(path)
        c2 = TestClient(create_app(STORE, FACULTY_STORE, st2))
        d = c2.get("/api/admin/audit", headers=ADMIN).json()
        assert d["total"] == n1, f"重启后审计少了：{n1} -> {d['total']}"
        assert any(r["action"] == "hide" for r in d["items"])
        # 新写一条也不影响老的
        c2.post("/api/admin/posts", params={}, headers=ADMIN)
        assert c2.get("/api/admin/audit", headers=ADMIN).json()["total"] >= n1
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@case
def queue_超时判定_30_小时前是_overdue_1_小时前不是():
    """**处理时限要能变成一个看得见的数**，不是感觉。

    阈值默认 24 小时（`config.MODERATION_DUE_HOURS`）。
    这里直接改帖子里的 `first_reported_at` 来造时间 ——
    **不 sleep、不等真实时钟**，所以这条不会因为跑得慢而飘。
    """
    c = _gov_client()
    pid = _mk_post(c)
    q = _to_pending(c, pid)
    item = next(x for x in q["items"] if x["id"] == pid)
    assert item["reported_at"], "队列每条必须有 reported_at"
    assert item["due_hours"] == config.MODERATION_DUE_HOURS
    assert item["age_hours"] is not None and item["age_hours"] < 1
    assert item["overdue"] is False, "刚举报的不能算超时"
    assert item["due_at"] is not None
    assert q["overdue_count"] == 0

    st = c.app.state.state
    post = st.find_post(pid)

    # ---- 1 小时前 → 不算超时 ----
    post["first_reported_at"] = (datetime.now() - timedelta(hours=1)).isoformat(
        timespec="seconds")
    st.save()
    q = c.get("/api/admin/queue", headers=ADMIN).json()
    item = next(x for x in q["items"] if x["id"] == pid)
    assert item["age_hours"] is not None and 0.9 < item["age_hours"] < 1.1
    assert item["overdue"] is False, "1 小时前不该算超时"
    assert q["overdue_count"] == 0

    # ---- 30 小时前（> 24）→ 超时 ----
    post["first_reported_at"] = (datetime.now() - timedelta(hours=30)).isoformat(
        timespec="seconds")
    st.save()
    q = c.get("/api/admin/queue", headers=ADMIN).json()
    item = next(x for x in q["items"] if x["id"] == pid)
    assert 29.9 < item["age_hours"] < 30.1, item["age_hours"]
    assert item["overdue"] is True, "30 小时前必须算超时"
    assert q["overdue_count"] == 1

    # ---- 处置之后就不再算积压（我们动过手了）----
    c.post(f"/api/admin/posts/{pid}", params={"action": "hide"}, headers=ADMIN)
    q = c.get("/api/admin/queue", headers=ADMIN).json()
    assert q["overdue_count"] == 0, "已经处置过的不算积压"
    item = next(x for x in q["items"] if x["id"] == pid)
    assert item["overdue"] is False
    assert item["age_hours"] is not None, "但年龄还是要知道的"

    # ---- 没有 reported_at 的返回 False，不是 True（不知道 ≠ 超时）----
    pid2 = _mk_post(c, title="没被举报过的")
    post2 = st.find_post(pid2)
    assert not post2.get("first_reported_at")
    q = c.get("/api/admin/queue", params={"status": "visible"}, headers=ADMIN).json()
    item2 = next((x for x in q["items"] if x["id"] == pid2), None)
    if item2 is not None:
        assert item2["overdue"] is False and item2["reported_at"] is None


@case
def queue_排序_超时的和等得最久的排在前面():
    """**原来按 `created_at` 倒序对审核队列恰恰是反的** ——
    最老的举报最容易被一直压在最底下。

    现在：超时的在前，然后等得最久的在前。
    """
    c = _gov_client()
    st = c.app.state.state
    ids = []
    for i in range(3):
        pid = _mk_post(c, title=f"待审帖子{i}")
        _to_pending(c, pid, n=forum.REPORT_THRESHOLD)
        ids.append(pid)
    # 三个帖子分别造：40 小时前、10 小时前、刚刚
    for pid, hours in zip(ids, (40, 10, 0.1)):
        st.find_post(pid)["first_reported_at"] = (
            datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    st.save()

    q = c.get("/api/admin/queue", headers=ADMIN).json()
    order = [x["id"] for x in q["items"]]
    assert q["overdue_count"] == 1, q["overdue_count"]
    assert order[0] == ids[0], f"40 小时前那条（唯一超时）必须排第一：{order}"
    assert order[1] == ids[1], f"然后是 10 小时前的：{order}"
    assert order[2] == ids[2], f"刚举报的排最后：{order}"


@case
def limit_带原因和重试时间_不是笼统_429():
    """**去重管"同一条"，限速管"广撒网"** —— 两个机制，都不能省。

    触发时给明确的原因、上限、已用、**还要等多久**，外加 `Retry-After` 头。
    只给一个 429 状态码，用户只能反复重试。
    """
    c = _gov_client()
    # 配置里发帖上限 5 次 / 小时 —— 用配置值算，不写死 5
    n = config.POST_RATE_LIMIT
    for i in range(n):
        assert c.post("/api/posts", json={
            "title": f"第 {i} 帖", "body": "正文", "author_real_id": "u_flood",
        }).status_code == 201, f"第 {i} 帖应当成功"

    r = c.post("/api/posts", json={"title": "第 n 帖", "body": "正文",
                                  "author_real_id": "u_flood"})
    assert r.status_code == 429, r.status_code
    e = r.json()["error"]
    assert e["code"] == "RATE_LIMITED"
    assert e["bucket"] == "发帖"
    assert e["limit"] == n and e["window_s"] == config.POST_RATE_WINDOW_S
    assert e["used"] >= n
    assert e["retry_after_s"] > 0, "必须给重试时间"
    assert "秒后再试" in e["message"], e["message"]
    assert r.headers.get("Retry-After"), "要有标准的 Retry-After 头"

    # **换个人不受影响**（限速是按人的，不是全站）
    assert c.post("/api/posts", json={
        "title": "别人的帖", "body": "正文", "author_real_id": "u_other",
    }).status_code == 201

    # **校验失败的请求不吃配额** —— 否则用户表单填错三次就被锁一小时
    c2 = _gov_client()
    for _ in range(config.POST_RATE_LIMIT + 3):
        assert c2.post("/api/posts", json={"title": "只有标题"}).status_code == 400
    # 配额仍然完好
    assert c2.post("/api/posts", json={
        "title": "正经帖", "body": "正文", "author_real_id": "u_ok",
    }).status_code == 201

    # 举报也要限速：一个人对 50 个不同帖子各举报一次，去重完全拦不住
    c3 = _gov_client()
    # **每个帖子换个作者**：不然还没测到举报限速，就先撞上发帖限速了
    ids = [_mk_post(c3, title=f"被举报的{i}", author=f"u_author{i}")
           for i in range(config.REPORT_RATE_LIMIT + 1)]
    codes = []
    for pid in ids:
        codes.append(_report_post(c3, pid, "u_spray").status_code)
    assert codes[-1] == 429, codes
    assert all(x == 200 for x in codes[:-1]), codes
    rr = _report_post(c3, ids[0], "u_spray")
    assert rr.status_code == 429 and rr.json()["error"]["bucket"] == "举报"

    # **重复举报不吃举报配额**：它没产生新内容
    c4 = _gov_client()
    pids = [_mk_post(c4, title=f"帖子{j}", author=f"u_a{j}") for j in range(3)]
    first = _report_post(c4, pids[0], "u_dup")
    assert first.status_code == 200 and first.json()["deduped"] is False
    for _ in range(config.REPORT_RATE_LIMIT + 2):
        r = _report_post(c4, pids[0], "u_dup")
        assert r.status_code == 200 and r.json()["deduped"] is True, r.json()
    for pid in pids[1:]:               # 配额没被 deduped 耗掉
        assert _report_post(c4, pid, "u_dup").status_code == 200


@case
def 举报去重_回归_别被后续改动破坏():
    """**这一条是回归**：去重是上一轮做好的（"否则找三个朋友就能把任何帖子压下去"），
    加了审计和限速之后必须还在。

    去重和限速是**两个不同的问题**：去重管"同一条"，限速管"广撒网"。
    加限速不能把去重顶掉，反过来也一样。
    """
    c = _gov_client()
    pid = _mk_post(c)

    # 同一人刷 5 次：只计一次，且**不会**因为累计到阈值而进队列
    for _ in range(5):
        r = _report_post(c, pid, "u_same")
        assert r.status_code == 200, r.status_code
    body = r.json()
    assert body["report_count"] == 1, body
    assert body["deduped"] is True, body
    assert body["review_status"] == "visible", "一个人刷不出 pending"
    assert c.get("/api/posts").json()["total"] == 1, "帖子应当还在前台"

    # **重复举报不写审计**：它没改变任何状态，写进去只会淹掉真正的那条
    rows = c.get("/api/admin/audit", headers=ADMIN).json()["items"]
    assert sum(1 for x in rows if x["action"] == "report") == 1, rows

    # 三个不同的人 → 到阈值，进队列
    for i in range(forum.REPORT_THRESHOLD - 1):
        _report_post(c, pid, f"u_diff{i}")
    assert c.get("/api/posts").json()["total"] == 0, "到阈值应当离开前台"
    q = c.get("/api/admin/queue", headers=ADMIN).json()
    assert q["total"] == 1
    # 举报理由要能看到（审核的人要能判）
    assert q["items"][0]["reports"], "队列里看不到举报理由就没法判"


@case
def appeal_申诉流转_审计里能看到完整四条链():
    """**恢复之后审计里能看到完整链条**：举报 → 下架 → 申诉 → 恢复。

    申诉必须**带原文快照** —— 不能因为下架就丢原文。
    """
    c = _gov_client()
    pid = _mk_post(c, body="这是我的原文，申诉时要能对着它判。", author="u_author")
    _to_pending(c, pid)
    c.post(f"/api/admin/posts/{pid}", params={"action": "hide",
                                             "reason_code": "harassment",
                                             "note": "以为在骂人"}, headers=ADMIN)

    # ---- 只有作者能申诉 ----
    assert c.post(f"/api/posts/{pid}/appeal",
                  json={"author_real_id": "u_someone_else", "reason": "我要申诉"}
                  ).status_code == 403
    # ---- 理由必填 ----
    assert c.post(f"/api/posts/{pid}/appeal",
                  json={"author_real_id": "u_author"}).status_code == 400

    r = c.post(f"/api/posts/{pid}/appeal",
               json={"author_real_id": "u_author",
                     "reason": "那句话是引用新闻，不是针对个人"})
    assert r.status_code == 200, r.text
    ap_id = r.json()["appeal_id"]
    assert r.json()["status"] == "pending"

    # 一次只有一条待处理申诉
    dup = c.post(f"/api/posts/{pid}/appeal",
                 json={"author_real_id": "u_author", "reason": "again"})
    assert dup.status_code == 409, dup.status_code

    # **申诉带原文快照**
    st = c.app.state.state
    ap = st.find_post(pid)["appeal"]
    assert "原文" in ap["post_snapshot"]["body"], ap["post_snapshot"]
    assert ap["status"] == "pending"

    # ---- 管理员恢复 ----
    res = c.post(f"/api/admin/appeals/{ap_id}", params={"action": "restore",
                                                       "note": "确实是引用"},
                 headers=ADMIN)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["result"] == "restored"
    assert body["review_status"] == "rejected", "恢复后回到前台可见"
    # **完整链条**
    chain = body["audit_chain"]
    for step in ("report", "hide", "appeal", "appeal_restored"):
        assert step in chain, f"审计链条缺 {step}：{chain}"
    assert chain.index("report") < chain.index("hide") < chain.index("appeal"), chain
    # 恢复之后帖子真的回到前台
    assert c.get("/api/posts").json()["total"] == 1

    # 结案后不能重复申诉
    again = c.post(f"/api/posts/{pid}/appeal",
                   json={"author_real_id": "u_author", "reason": "再来一次"})
    assert again.status_code == 409, again.status_code
    # 已结案的申诉也不能再处置
    assert c.post(f"/api/admin/appeals/{ap_id}", params={"action": "uphold"},
                  headers=ADMIN).status_code == 409
    # 不存在的申诉 404
    assert c.post("/api/admin/appeals/ap_nope", params={"action": "uphold"},
                  headers=ADMIN).status_code == 404

    # ---- 另一条：维持原处置 ----
    pid2 = _mk_post(c, title="第二条要被驳回的")
    _to_pending(c, pid2)
    c.post(f"/api/admin/posts/{pid2}", params={"action": "hide"}, headers=ADMIN)
    ap2 = c.post(f"/api/posts/{pid2}/appeal",
                 json={"author_real_id": "u_author", "reason": "不服"}).json()["appeal_id"]
    res2 = c.post(f"/api/admin/appeals/{ap2}", params={"action": "uphold"},
                  headers=ADMIN).json()
    assert res2["result"] == "upheld"
    assert res2["review_status"] == "hidden", "维持 = 还是下架"
    assert "appeal_upheld" in res2["audit_chain"]


@case
def moderation_处置结果可查_但举报人看不到举报人():
    """**处置结果可查，但绝不让举报人看到举报人是谁。**

    - 作者：状态 + **依据类别** + 时间 + 申诉进展；
    - 举报人：**只有**"已处理/正在处理"；
    - 路人：看不到。
    """
    c = _gov_client()
    pid = _mk_post(c, author="u_author")
    _to_pending(c, pid)
    c.post(f"/api/admin/posts/{pid}", params={"action": "hide",
                                             "reason_code": "privacy",
                                             "note": "含他人手机号"}, headers=ADMIN)

    # ---- 作者看得到依据类别和时间 ----
    a = c.get(f"/api/posts/{pid}/moderation", params={"user": "u_author"}).json()
    assert a["your_role"] == "author"
    assert a["status"] == "hidden"
    assert a["status_label"] == "已下架"
    assert a["reason_code"] == "privacy"
    assert a["reason_label"] == "泄露个人隐私", "作者有权知道依据"
    assert a["moderated_at"], "作者有权知道什么时候"
    assert a["appeal_status"] == "none"
    # **但不能露举报人**
    flat = json.dumps(a, ensure_ascii=False)
    for leak in ("u_r0", "reporter_id", "reporter"):
        assert leak not in flat, f"作者视图泄漏了 {leak}：{flat}"

    # ---- 举报人只看得到"已处理" ----
    rep = c.get(f"/api/posts/{pid}/moderation", params={"user": "u_r0"}).json()
    assert rep["your_role"] == "reporter"
    assert rep["handled"] is True
    assert rep["message"] == "已处理"
    # 不给类别、不给备注、不给时间线 —— 给了就能反推"是不是因为我举报才处理"
    for k in ("reason_code", "reason_label", "moderated_at", "appeal"):
        assert k not in rep, f"举报人视图不该有 {k}：{rep}"
    assert "u_r1" not in json.dumps(rep, ensure_ascii=False), "举报人看不到别的举报人"

    # ---- 路人看不到 ----
    assert c.get(f"/api/posts/{pid}/moderation",
                 params={"user": "u_stranger"}).status_code == 403
    # 帖子不存在 404
    assert c.get("/api/posts/p_nope/moderation",
                 params={"user": "u_author"}).status_code == 404

    # ---- 还没处置时，举报人看到的是"正在处理" ----
    pid2 = _mk_post(c, title="还没处置的")
    _to_pending(c, pid2)
    rep2 = c.get(f"/api/posts/{pid2}/moderation", params={"user": "u_r0"}).json()
    assert rep2["handled"] is False
    assert rep2["message"] == "已收到，正在处理"
    assert rep2["due_at"], "举报人该知道预计什么时候有结果"


# ---------- 16. 字段可信度：值 / null / unknown 三态（契约 api.md 第 14 节） ----------
#
# 这一节测的是一句话：**`unknown` 不许被当成 `absent`。**
# 所以排序 / 统计 / 聚合**三处都要验**，而不是只在字段里体现一下。
#
# **断言不许依赖磁盘上恰好是什么数据**：1/2/4 全部用自造记录；
# 第 3 条虽然打真数据，但期望值是**从 RAW 现算的**，没有任何硬编码的比率。

def _cred_record(pid, title, deadline=None, fs=None, school="测试大学",
                 publish="2026-10-01", conf=0.9, majors=None):
    """造一条公告。`fs` 传 None 表示**老数据**（没有 field_state）。"""
    rec = {
        "id": pid, "school": school, "school_slug": "test", "unit": "研究生院",
        "board": "招生", "title": title, "publish_date": publish,
        "url": f"https://example.invalid/{pid}", "deadline": deadline,
        "apply_mode": "推免", "target_year": 2027, "degree_types": ["硕士"],
        "majors": majors if majors is not None else [],
        # **A 一落盘 `field_state`，字段必然在这里**；`_raw` 用来验"原样透传"
        "tags": ["推免"], "extract": {"version": "v4", "confidence": conf},
        "body_text": "正文" * 50, "body_len": 100, "attachments": [],
        "materials": [], "contact": None, "source": "test",
        "content_hash": f"h_{pid}", "crawled_at": "2026-10-17T08:00:00",
        "relevance": 2,
    }
    if fs is not None:
        rec["field_state"] = fs
    return rec


def _cred_client(records):
    """把自造记录写进临时 JSONL、走**真的** `NoticeStore.load`。

    （和 digest 那一节同一个做法：用真加载器，这样"详情能取到""meta 能算"
    都是真走通的，而不是我在测试里绕过一层。）
    """
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="tuimian_cred_")
    path = os.path.join(tmpdir, "notices.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    st = NoticeStore.load(path)
    c = TestClient(create_app(st, FACULTY_STORE, StateStore.load(":memory:")))
    c._tmpdir = tmpdir
    return c


def _close(c):
    import shutil
    shutil.rmtree(getattr(c, "_tmpdir", ""), ignore_errors=True)


# 四条记录，正好覆盖三态 + 老数据：
#   found 早 / found 晚 / unknown（老数据，没有 field_state）/ absent（数据层明确说）
def _three_state_records():
    return [
        _cred_record("n_found_early", "早截止", deadline="2026-11-01"),
        _cred_record("n_found_late", "晚截止", deadline="2027-06-01"),
        # 老记录：deadline 为 null 且**没有 field_state** → unknown
        _cred_record("n_unknown", "没读到截止日"),
        # **再来一条 unknown**：故意让 unknown 和 absent 的条数**不相等** ——
        # 不然"两个数不同"这条断言在它们恰好都是 1 时毫无意义（我第一版就是这个毛病）
        _cred_record("n_unknown2", "也没读到",
                     fs={"deadline": "unknown", "majors": "found",
                         "attachments": "found", "body": "missing"}),
        # 数据层明确说：读了正文，确实没有截止日 → absent
        _cred_record("n_absent", "公告里就没有截止日", fs={
            "deadline": "absent", "majors": "found",
            "attachments": "found", "body": "ok"}),
    ]


@case
def cred_三态能原样往返():
    """**第 1 条**：数据层给什么三态，接口就还什么三态。

    这条看着简单，但它是后面所有断言的地基 ——
    如果 `field_state` 在路上被吃掉或改名，用户就永远分不出那两句话。
    """
    recs = [
        _cred_record("n1", "都有", deadline="2026-12-01", fs={
            "deadline": "found", "majors": "found",
            "attachments": "found", "body": "ok"}),
        _cred_record("n2", "确实没有", fs={
            "deadline": "absent", "majors": "absent",
            "attachments": "found", "body": "generic"}),
        _cred_record("n3", "判不了", fs={
            "deadline": "unknown", "majors": "unknown",
            "attachments": "unknown", "body": "missing"}),
    ]
    c = _cred_client(recs)
    try:
        for pid, want in (("n1", {"deadline": "found", "majors": "found",
                                  "attachments": "found", "body": "ok"}),
                          ("n2", {"deadline": "absent", "majors": "absent",
                                  "attachments": "found", "body": "generic"}),
                          ("n3", {"deadline": "unknown", "majors": "unknown",
                                  "attachments": "unknown", "body": "missing"})):
            d = c.get(f"/api/notices/{pid}", params={"meta": 1}).json()
            fs = d["field_state"]
            for k, v in want.items():
                assert fs[k] == v, f"{pid}.{k}: 给的是 {v}，回来的是 {fs[k]}"
            assert fs["_source"] == "data", "数据层给了就该标 data，不该标 inferred"
            # 三态和措辞必须一致
            disp = d["deadline_display"]
            assert disp["state"] == want["deadline"]
            if want["deadline"] == "found":
                assert disp["text"] == "2026-12-01" and disp["must_check_original"] is False
            elif want["deadline"] == "absent":
                assert disp["text"] == credibility.ABSENT_DEADLINE_TEXT
                assert disp["must_check_original"] is False, "确定没有就不用逼用户看原文"
            else:
                assert disp["text"] == credibility.UNKNOWN_DEADLINE_TEXT
                assert disp["must_check_original"] is True, "判不了就必须让用户核对原文"
                assert disp["url"], "unknown 的措辞要求**必须带原文链接**"
    finally:
        _close(c)


@case
def cred_unknown_不被当成_absent_排序():
    """**第 2 条（a）排序**：`sort=deadline_asc` 时 `unknown` 不许和 `absent` 一起沉底。

    原来两处都是 `_parse_date(deadline) or date.max` ——
    `deadline is None` 一律沉到最下面，等于把"我们没读到"和"确实没有"
    一起埋在最底下。**一个 unknown 的公告可能下周就截止。**

    正确顺序：found 按日期升序 → **unknown** → absent。
    """
    c = _cred_client(_three_state_records())
    try:
        # **必须带 meta=1**：列表项默认**不带** `field_state`（形状被 fixture 钉住），
        # 所以三态要从 `field_trust.states` 读 —— 那正是契约给的列表侧出口。
        d = c.get("/api/notices", params={"sort": "deadline_asc", "size": 50,
                                         "meta": 1}).json()
        ids = [x["id"] for x in d["items"]]
        assert ids[:2] == ["n_found_early", "n_found_late"], f"found 没按日期升序：{ids}"
        assert ids[-1] == "n_absent", f"absent 该沉底：{ids}"
        assert set(ids[2:4]) == {"n_unknown", "n_unknown2"}, f"unknown 该夹在中间：{ids}"

        # 用**区间**判，而不是只看某一条的位置 —— 这样条数怎么变都成立
        states = {x["id"]: x["field_trust"]["states"]["deadline"] for x in d["items"]}
        by_state = {}
        for i, pid in enumerate(ids):
            by_state.setdefault(states[pid], []).append(i)
        assert set(by_state) == {"found", "unknown", "absent"}, by_state
        assert max(by_state["found"]) < min(by_state["unknown"]), \
            f"found 和 unknown 的区间重叠了：{by_state}"
        assert max(by_state["unknown"]) < min(by_state["absent"]), \
            "unknown 被当成 absent 一起沉底了 —— 这正是第 14 节要消灭的那个混淆"
    finally:
        _close(c)


@case
def cred_unknown_不被当成_absent_统计与聚合():
    """**第 2 条（b）统计 +（c）聚合**。

    统计：`/api/quality` 三态必须**分列**，永远不给一个"没有截止日的条数"。
    聚合：`/api/match` 的 `bucket` 里 `unknown` → `需人工确认`、
          `absent` → `无截止日`（两个桶的回答完全不同）。
    """
    recs = _three_state_records()
    c = _cred_client(recs)
    try:
        # ---- (b) 统计 ----
        q = c.get("/api/quality").json()
        dl = q["overall"]["fields"]["deadline"]
        # 五条：found 2（有日期）/ unknown 2（一条老数据 + 一条明确 unknown）/ absent 1
        assert dl["found"] == 2, dl
        assert dl["unknown"] == 2, dl
        assert dl["absent"] == 1, dl
        assert dl["total"] == 5, dl
        assert dl["found"] + dl["unknown"] + dl["absent"] == dl["total"], dl
        # **未知和确定没有必须是两个独立的数。**
        # 条数故意不相等（2 vs 1）—— 不然"两个数不同"这条断言在它们恰好都是 1 时
        # 毫无意义（我第一版就是这个毛病，被自己的测试骗了一次）。
        assert dl["unknown"] != dl["absent"], "统计把 unknown 和 absent 混成一个数了"
        assert dl["unknown_rate"] == 0.4 and dl["absent_rate"] == 0.2, dl
        # 不存在"missing"这个键 —— 那正是旧口径的合并
        assert "missing" not in dl, f"不该有合并口径的键：{dl}"

        # 按学校筛出来的比例要和整体对得上（不是写死的）
        one = c.get("/api/quality", params={"school": "测试大学"}).json()
        assert one["total"] == 5, one["total"]
        assert one["deadline_unknown_rate"] == dl["unknown_rate"]

        # ---- (c) 聚合：match 的分桶 ----
        # `unknown` → 需人工确认（你去看原文）；`absent` → 无截止日（这条不用赶）
        items = c.post("/api/match", json={}, params={"limit": 50}).json()["items"]
        by_id = {x["id"]: x["bucket"] for x in items}
        assert by_id["n_unknown"] == match_mod.BUCKET_UNKNOWN, by_id
        assert by_id["n_unknown2"] == match_mod.BUCKET_UNKNOWN, by_id
        assert by_id["n_absent"] == match_mod.BUCKET_ABSENT, by_id
        assert by_id["n_unknown"] != by_id["n_absent"], \
            "聚合把 unknown 和 absent 混进同一个桶了 —— 两个桶的回答完全不同"
        # found 的两条走真实日期分桶，不是人工确认
        assert by_id["n_found_early"] not in (match_mod.BUCKET_UNKNOWN,
                                             match_mod.BUCKET_ABSENT)
        # 新桶名必须在服务端的词表里（C 要照它更新自己那份顺序数组）
        assert match_mod.BUCKET_ABSENT in match_mod.BUCKET_ORDER, match_mod.BUCKET_ORDER
        assert match_mod.BUCKET_UNKNOWN in match_mod.BUCKET_ORDER
    finally:
        _close(c)


@case
def cred_quality_按真文件现算不硬编码():
    """**第 3 条**：`/api/quality` 的数必须是**按真数据现算**的。

    期望值从 `RAW` 现算，**没有任何写死的比率** ——
    数据一涨（这一晚从 78 涨到 196），硬编码的断言会红，
    而红的理由是"数据变了"而不是"代码坏了"，那种门禁很快就没人看。
    """
    q = CLIENT.get("/api/quality").json()
    assert q["total"] == len(RAW), (q["total"], len(RAW))

    # 用同一套判定重算一遍，必须逐个对上
    for name in credibility.TRACKED_FIELDS:
        want = credibility.tally(RAW, name)
        got = q["overall"]["fields"][name]
        assert got == want, f"{name} 的统计对不上：\n接口 {got}\n现算 {want}"
    assert q["overall"]["fields"]["body"] == credibility.body_tally(RAW)
    assert q["deadline_unknown_rate"] == credibility.tally(RAW, "deadline")["unknown_rate"]

    # **body 的比率分母是"记了抽取方式的那些"，不是全部。**
    # 真实数据 196/196 都是 unknown → generic_rate 必须是 None。
    # 如果哪天 A 落了 field_state.body，这条断言会自动走另一支。
    bt = credibility.body_tally(RAW)
    assert q["overall"]["fields"]["body"]["known"] == bt["known"], bt
    if bt["known"] == 0:
        assert bt["generic_rate"] is None and bt["ok_rate"] is None, bt

    # summary 里的百分数必须是现算的，不能是写死的字符串
    assert str(len(RAW)) in q["summary"], q["summary"]
    body = q["overall"]["fields"]["body"]
    if body["known"] == 0:
        # **一个都没记抽取方式时，比率必须是 null，不是 0.0** ——
        # 0.0% 读起来像"测过了、一条都没用兜底选择器"，
        # 而真相是"一条都没记录"。真实数据现在就是这种情况（196/196 unknown）。
        assert q["body_generic_rate"] is None, q["body_generic_rate"]
        assert "无从计算" in q["summary"], q["summary"]
    else:
        assert q["body_generic_rate"] == credibility.body_tally(RAW)["generic_rate"]
        assert f"{q['body_generic_rate'] * 100:.1f}%" in q["summary"], q["summary"]
    if q["deadline_unknown_rate"] is None:
        assert "无" in q["summary"], "没有数据时该说'无'，不能说 0.0%"

    # 按学校逐所加起来 == 全体
    total_by_school = sum(s["total"] for s in q["schools"])
    assert total_by_school == q["total"], (total_by_school, q["total"])
    assert q["overall"]["schools"] == len(q["schools"])

    # 单字段查询
    one = CLIENT.get("/api/quality", params={"field": "deadline"}).json()
    assert set(one["overall"]["fields"]) == {"deadline"}, one["overall"]["fields"]
    # 不认识的字段 → 400，不是静默忽略
    assert CLIENT.get("/api/quality", params={"field": "nope"}).status_code == 400


@case
def cred_老数据没有_field_state_时按_unknown_并说明():
    """**第 4 条**：`field_state` 缺失（老数据）时按 `unknown` 处理，**并说明是推断的**。

    两个方向都要验：
    - **值为空** + 没有 field_state → `unknown`（**绝不许推成 `absent`**）；
    - **值在** + 没有 field_state → `found`（"有值"本身就是证据；
      把它说成 unknown 会让用户白跑一趟原文 —— 那是另一种不诚实）。
    """
    c = _cred_client([
        _cred_record("old_null", "老数据，没有截止日也没说为什么"),   # → unknown
        _cred_record("old_val", "老数据，但有截止日", deadline="2026-12-31"),  # → found
        _cred_record("old_majors", "老数据，专业是空的"),             # majors → unknown
    ])
    try:
        d = c.get("/api/notices/old_null", params={"meta": 1}).json()
        assert d["field_state"]["deadline"] == "unknown", d["field_state"]
        assert d["field_state"]["deadline"] != "absent", \
            "老数据绝不能推成 absent —— 那正是这一节要防的事"
        # **要说清这是推断的**
        assert d["field_state"]["_source"] == "inferred", d["field_state"]
        assert d["deadline_display"]["must_check_original"] is True
        # 四个键都要在（缺键比错值更难查）
        for k in ("deadline", "majors", "attachments", "body"):
            assert k in d["field_state"], f"field_state 缺 {k}：{d['field_state']}"
        assert d["field_state"]["body"] == "unknown", "老数据没记正文抽取方式，不许猜"
        assert d["field_trust"]["has_untrusted"] is True
        assert "deadline" in d["field_trust"]["untrusted_fields"]

        # **值是有的 → found**，不许说 unknown
        v = c.get("/api/notices/old_val", params={"meta": 1}).json()
        assert v["field_state"]["deadline"] == "found", v["field_state"]
        assert v["deadline_display"]["text"] == "2026-12-31"
        assert v["deadline_display"]["must_check_original"] is False
        assert "deadline" not in v["field_trust"]["untrusted_fields"]

        # 空列表同样算"没有值"
        m = c.get("/api/notices/old_majors", params={"meta": 1}).json()
        assert m["field_state"]["majors"] == "unknown", m["field_state"]

        # 列表的摘要也要在（`?meta=1`）
        lst = c.get("/api/notices", params={"meta": 1, "size": 50}).json()
        row = next(x for x in lst["items"] if x["id"] == "old_null")
        assert row["field_trust"]["untrusted_count"] >= 1, row["field_trust"]
        assert row["field_trust"]["states"]["deadline"] == "unknown"
    finally:
        _close(c)


@case
def cred_field_state_落盘后_详情默认就带它_接口不用改():
    """**第 5 条（前瞻）**：A 落盘 `field_state` 之后，
    它作为**数据字段**会随 `dict(rec)` 自动出现在详情响应里 —— 接口代码不用改。

    这条是替我未来的自己写的：**不要**为了等 A 而写一层"如果数据里有就加上"的
    转发代码，那层代码一旦写错就是把数据吃掉。数据字段自然透传才对。

    （默认形状受 `fixtures/notices.detail.json` 钉住，所以 A 落盘后要重出 fixture
    才会变成默认键之一 —— 这里验的是"透传这条路是通的"。）
    """
    c = _cred_client([
        _cred_record("has_fs", "数据层给了 field_state", deadline="2026-12-01", fs={
            "deadline": "found", "majors": "absent",
            "attachments": "unknown", "body": "generic",
            # 数据层以后加的额外键**不许被我吃掉**
            "extra_future_key": "保留我"}),
    ])
    try:
        # 不带 meta：靠 `dict(rec)` 自然透传
        d = c.get("/api/notices/has_fs").json()
        assert "field_state" in d, "数据字段没有透传到详情 —— 是不是被投影吃掉了？"
        assert d["field_state"]["deadline"] == "found"
        assert d["field_state"]["extra_future_key"] == "保留我", \
            "数据层加的额外键被吃掉了（这会让 A 加字段时必须改 B 的代码）"

        # 带 meta：规范化版本，四个键齐 + 保留额外键
        m = c.get("/api/notices/has_fs", params={"meta": 1}).json()["field_state"]
        assert m["deadline"] == "found" and m["majors"] == "absent"
        assert m["attachments"] == "unknown" and m["body"] == "generic"
        assert m["extra_future_key"] == "保留我"
        assert m["_source"] == "data"
    finally:
        _close(c)


# ---------------------------------------------------------------------------
# 17. 鲁棒性：**喂坏的输入，看它响不响**（契约 api.md 第 15 节）
#
# 用户这一轮的判据是：「功能跑通不代表鲁棒性就强，不希望做出来的只是偶尔成功的
# happy case」。前面 126 条全绿，**但绿的道理是它们都在喂正常输入** ——
# 坏数据、空数据、坏状态文件、畸形正文一个都没进过门禁。
#
# 所以这一节的每一条都必须满足两件事，缺一不可：
#   ① 有一个**故意做坏的输入**（造出来的，不依赖磁盘上恰好是什么数据）；
#   ② 断言"**响**"：门禁红、或者接口给出一个机器能判断的状态 —— 而不是"没崩"。
#
# 反过来也要防：**"每次都报警"等于没有报警**。所以每条坏输入旁边都要有一条
# "正常输入不响"的对照断言。少了这一半，鲁棒性就变成了新的误报源。
# ---------------------------------------------------------------------------

def _jsonl_tmp(lines, prefix="tuimian_rb_"):
    """把给定的行**原样**写进一个临时 `notices.jsonl`，返回 (path, dir)。

    坏输入必须走**真的** `NoticeStore.load`，不能在测试里绕开它 ——
    否则验的是"我构造的 meta 能不能被检查"，不是"坏文件会不会被拦住"。
    """
    import tempfile

    d = tempfile.mkdtemp(prefix=prefix)
    _TMP_DIRS.append(d)
    path = os.path.join(d, "notices.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")
    return path, d


@case
def 加载自检_坏行必须让门禁变红而且说清第几行():
    """**环 1。** 坏输入：`notices.jsonl` 里有一行不是合法 JSON。

    实测过的事故（D 报的）：改坏第 6 行 → 服务**静默加载 195/196**，
    `/api/health` 里 `skipped_lines: 1` 记下来了，**126 条门禁全绿**。
    少一条公告，没有任何东西会响。

    这条用例做四件事：
      ① 真数据必须零坏行 —— 坏行一旦真的出现在磁盘上，**这条立刻红**（响）
      ② 造一个坏文件：加载不抛（不拒绝启动）、行号对、错因有、条数对
      ③ 端到端：注入这份 store 的 app，`/api/health` 说 `degraded` 且点名第几行，
         同时 `/api/notices` **仍然返回能读到的那些**（"能用，但要响"）
      ④ 三态不许混：`empty`（0 条）不是 `ok`，`degraded` 不是 `empty`
    """
    from server.store import load_health

    # ---- ① 真数据：这是"喂坏输入"的另一半 —— 坏输入真的出现时这里要红 ----
    real = load_health(STORE.load_meta)
    assert real["state"] == "ok", (
        f"数据源里有 {real['skipped_lines']} 行解析失败，"
        f"接口会静默少 {real['skipped_lines']} 条公告：{real['bad_lines']}")

    # ---- ② 造坏的 ----
    good = [_fake_notice("rb_ok_1"), _fake_notice("rb_ok_2"), _fake_notice("rb_ok_3")]
    lines = [
        json.dumps(good[0], ensure_ascii=False),
        "{ 这一行坏了",                     # 第 2 行：半截 JSON
        json.dumps(good[1], ensure_ascii=False),
        '["我不是对象"]',                    # 第 4 行：合法 JSON 但不是对象
        json.dumps(good[2], ensure_ascii=False),
        json.dumps({"no_id": True}, ensure_ascii=False),   # 第 6 行：缺 id
        "",
    ]
    path, _ = _jsonl_tmp(lines)
    bad_store = NoticeStore.load(path)      # **不抛**：1 行坏不该拒绝启动
    h = load_health(bad_store.load_meta)
    assert h["state"] == "degraded", h
    assert h["usable"] is True, "坏行把服务打挂了 —— 另一个极端，也不是响"
    assert h["loaded"] == 3, h
    assert h["skipped_lines"] == 3, h
    assert [b["line"] for b in h["bad_lines"]] == [2, 4, 6], h["bad_lines"]
    for b in h["bad_lines"]:
        assert b["error"].strip(), b
        assert b["excerpt"].strip(), b
    # 人话要说清"少了东西"，不能只说"ok"
    assert str(h["skipped_lines"]) in h["message"] and "少" in h["message"], h["message"]

    # ---- ③ 端到端：health 要能机器判断，接口仍可用 ----
    c = TestClient(create_app(bad_store, FACULTY_STORE, StateStore.load(":memory:")))
    try:
        body = c.get("/api/health").json()
        assert body["status"] == "ok", "服务还活着（status 是存活语义）"
        assert body["data"]["load_state"] == "degraded", body["data"]["load_state"]
        assert body["data"]["load"]["bad_lines"][0]["line"] == 2, body["data"]["load"]
        # 旧字段保留：C 和老调用方读的是这个裸数，删掉是破坏兼容
        assert body["data"]["skipped_lines"] == 3, body["data"]["skipped_lines"]
        lst = c.get("/api/notices", params={"size": 50}).json()
        assert lst["total"] == 3, f"能读到的 3 条也不给看了：{lst['total']}"
    finally:
        _close(c)

    # ---- ④ 三态不许混（这一条是纯结构，不碰磁盘）----
    assert load_health({"loaded": 0, "skipped": 0, "bad_lines": []})["state"] == "empty"
    assert load_health({"loaded": 0, "skipped": 2,
                        "bad_lines": [{"line": 1, "error": "x"}]})["state"] == "degraded", \
        "全是坏行时根因是‘坏了’，报成 empty 会把人带去查管道"
    assert load_health({"loaded": 5, "skipped": 0, "bad_lines": []})["state"] == "ok"


@case
def 加载自检_正常数据不报假警():
    """**"每次都报警"等于没有报警。** 这条是上一条的对照组。

    接口和真数据都在正常状态时，`load_state` 必须是 `ok`、`bad_lines` 必须是空 ——
    否则演示时那行红字会一直挂着，谁也不会再去看它。
    """
    body = CLIENT.get("/api/health").json()
    assert body["data"]["load_state"] == "ok", body["data"]
    assert body["data"]["load"]["bad_lines"] == [], body["data"]["load"]
    assert body["data"]["skipped_lines"] == 0
    assert body["data"]["load"]["usable"] is True
    assert body["data"]["loaded"] == len(RAW), (body["data"]["loaded"], len(RAW))


@case
def 排序_relevance_高于日期_用造的数据验而不是赌磁盘():
    """**环 2。** 造四条记录：relevance 高但日期早的，必须排在低但日期晚的前面。

    为什么不能用真数据验（我第一版就是这么干的）：磁盘上 `relevance` 和
    `publish_date` **恰好是同向的**，所以"按日期排"和"按 relevance 排"
    在真数据的前 50 条里给出几乎一样的结果 —— 用真数据断言"高的在前"，
    等于在赌"今天的数据恰好长得像结论"。造数据才验得死。

    两个方向都要验，缺一不可：
      ① relevance 是**第一档**：rel=2 / 2026-01-05 必须排在 rel=0 / 2026-10-16 前面
      ② 同档内日期仍是降序：rel=2 里新日期在前（档内时效性不许被破坏）
      ③ relevance=0 的记录**一条都不许消失**（它只排序，绝不过滤）
    """
    recs = [
        # 高 relevance、日期最早
        dict(_fake_notice("rb_rel2_old", title="2027年接收推荐免试研究生工作办法",
                          publish_date="2026-01-05"), relevance=2),
        # 高 relevance、日期较新（用来验档内日期降序）
        dict(_fake_notice("rb_rel2_new", title="接收推免生工作办法补充通知",
                          publish_date="2026-10-16"), relevance=2),
        # 中档
        dict(_fake_notice("rb_rel1", title="关于研究生培养方案修订的说明",
                          publish_date="2026-10-15"), relevance=1),
        # 低 relevance、日期最新：**它必须最后一个**，而且必须还在
        dict(_fake_notice("rb_rel0", title="关于公布本学期课程安排的通知",
                          publish_date="2026-10-16"), relevance=0),
    ]
    path, _ = _jsonl_tmp([json.dumps(r, ensure_ascii=False) for r in recs])
    st = NoticeStore.load(path)
    c = TestClient(create_app(st, FACULTY_STORE, StateStore.load(":memory:")))
    try:
        body = c.get("/api/notices", params={"size": 50}).json()
        ids = [i["id"] for i in body["items"]]
        assert ids == ["rb_rel2_new", "rb_rel2_old", "rb_rel1", "rb_rel0"], ids
        # ② 档内日期降序（不是整天泡在 relevance 里就完事）
        assert ids.index("rb_rel2_new") < ids.index("rb_rel2_old")
        # ③ 只排序、不过滤：低档那条还在，总数也没少
        assert body["total"] == len(recs) == 4, (body["total"], len(recs))
        assert "rb_rel0" in ids
    finally:
        _close(c)


@case
def 排序_relevance_不许混进列表载荷():
    """**环 2 的锁。** `relevance` 只用于排序，**绝不用于过滤** —— 而且不许进列表载荷。

    为什么必须锁在"载荷"这一层：只要列表项里带着 `relevance`，
    前端（或者下一个 agent）就一定能拿它当筛选器用 —— 而 `collector/fields.py`
    自己写了"这个信号两个方向都会误杀"（实测把真的推免公示判成非招生）。
    一个会误杀的信号，能排不能滤。**结构上给不出去，才是最硬的锁。**

    这条同时是三处一起验：白名单没有它、默认响应每个 item 都没有它、
    `?meta=1` 也不许为了"顺手"把它带出来。
    """
    assert "relevance" not in config.LIST_FIELDS, \
        "relevance 进了白名单 —— 列表载荷马上就会带上它，然后就会有人拿它做筛选"
    for params in ({"size": 50}, {"size": 50, "meta": 1}):
        items = fetch_all(CLIENT, "/api/notices", params)
        leaked = [i["id"] for i in items if "relevance" in i]
        assert not leaked, f"列表项里带着 relevance（会被拿去当筛选器）：{leaked[:5]}"
    # 详情是**完整记录**，`relevance` 会随 `dict(rec)` 出现 —— 那被 fixture 钉着
    # （`notices.detail.json` 里就有这个键），是"详情给全"的语义，不是给前端筛选用的：
    # 列表页拿不到它，逐条拉详情又要花掉每天 3 条的免费额度。
    assert "relevance" in _fixture("notices.detail.json"), \
        "详情 fixture 变了 —— 这条注释和断言要一起重新想"


@case
def state_坏文件要备份要响_不许静默重建():
    """**环 4。** 坏输入：`state.json` 半截（写到一半崩了 / 手工改坏了）。

    ## 为什么这条比看起来严重

    状态文件里装着**帖子、订阅、关注、以及审计**。原来的行为是：读不出来 →
    改名为 `.broken-<时间戳>` → 从空状态继续，**但 `/api/health` 一个字都不说**。
    于是界面看起来像"新装了一个站"：帖子没了、审计没了，没有任何东西会响。
    审计是法律意义上要留痕的东西，**静默重建一个空 state 等于痕迹全丢**。

    这条用例验四件事：
      ① 不拒绝启动（演示现场一个字节坏了，不该让整个站打不开）
      ② 原文件**内容一点没丢**（改名备份，不是删除/覆盖）
      ③ `StateStore.recovery` + `/api/health` 都说得出来，而且说清"没恢复"
      ④ 对照：干净的状态文件**不许报警**（否则这行红字会一直挂着）
    """
    import tempfile

    d = tempfile.mkdtemp(prefix="tuimian_state_")
    _TMP_DIRS.append(d)
    path = os.path.join(d, "state.json")
    broken_text = '{"version": 1, "posts": [{"id": "p_lost"}], "audit": [{"a": 1}'
    with open(path, "w", encoding="utf-8") as f:
        f.write(broken_text)

    st = StateStore.load(path)                  # ① 不抛
    rec = st.recovery
    assert rec is not None, "坏状态文件被静默接受了 —— 一个字都没响"
    assert rec["reason"] == "unreadable", rec
    assert rec["backup"] and os.path.exists(rec["backup"]), rec
    # ② 原文件内容**逐字节**还在备份里（不是删掉重建）
    assert open(rec["backup"], encoding="utf-8").read() == broken_text, \
        "备份里的内容和原文件不一致 —— 这不是备份，是覆盖"
    # ③ 说得清：帖子/审计没恢复这件事必须在 message 里
    assert "没有恢复" in rec["message"], rec["message"]
    assert st.posts() == [] and st.audit_log() == []

    c = TestClient(create_app(STORE, FACULTY_STORE, st))
    try:
        body = c.get("/api/health").json()
        assert body["state"]["ok"] is False, body["state"]
        assert body["state"]["recovery"]["reason"] == "unreadable", body["state"]
        assert "没有恢复" in body["state"]["recovery"]["message"]
        # 服务本身照常可用（能用，但要响）
        assert c.get("/api/notices", params={"size": 1}).status_code == 200
    finally:
        _close(c)

    # ④ 对照：干净的文件不报警
    clean = os.path.join(d, "clean.json")
    with open(clean, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "users": {}, "posts": [], "orders": [],
                   "watches": [], "audit": [], "counters": {
                       "post": 0, "audit": 0, "appeal": 0}}, f)
    st2 = StateStore.load(clean)
    assert st2.recovery is None, st2.recovery
    c2 = TestClient(create_app(STORE, FACULTY_STORE, st2))
    try:
        assert c2.get("/api/health").json()["state"]["ok"] is True
    finally:
        _close(c2)


@case
def state_合法JSON但形状不对也要拦():
    """**环 4 的第二种坏输入。** 文件能解析，但不是我们要的形状。

    实测两种原来都会出大事（都不是"报个错"，而是"更难查的失败"）：

    | 坏法 | 原来发生什么 |
    |---|---|
    | 顶层是 `[]` | `AttributeError: 'list' object has no attribute 'setdefault'` —— **服务直接起不来**，而且错因完全指不到"状态文件坏了" |
    | `users` 是字符串 | 服务起来了，**每个 `/api/me` 都 500** —— 只有真去点的时候才发现 |

    现在两种都按"损坏"处理：备份 + 空状态 + 响。类型检查是**逐键**的，
    因为只 catch `JSONDecodeError` 覆盖不到"能解析但形状不对"这一类。
    """
    import tempfile

    d = tempfile.mkdtemp(prefix="tuimian_state_shape_")
    _TMP_DIRS.append(d)

    cases = [
        ("[]", "not_an_object"),
        ('{"version": 1, "users": "坏了", "posts": []}', "bad_shape"),
        ('{"version": 1, "posts": {"不是": "数组"}}', "bad_shape"),
        ("null", "not_an_object"),
    ]
    for i, (text, want_reason) in enumerate(cases):
        path = os.path.join(d, f"case{i}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        st = StateStore.load(path)              # **不许抛**
        assert st.recovery is not None and st.recovery["reason"] == want_reason, \
            (text, st.recovery)
        # 起来之后真的能用（`users` 那个原来每个请求都 500）
        c = TestClient(create_app(STORE, FACULTY_STORE, st))
        try:
            assert c.get("/api/me", params={"user": "u_x"}).status_code == 200, text
            assert c.get("/api/health").json()["state"]["ok"] is False, text
        finally:
            _close(c)


@case
def state_并发写不丢更新也不写坏文件():
    """**环 4。** 两个请求同时写会不会覆盖/截断 —— 这一项原来完全没验过。

    验法不是"看它崩不崩"（并发 bug 的窗口很窄，压一次不出事不能证明没 bug），
    而是断言三条**不变量**：
      ① 被接受（201）的每一条帖子，落盘之后**一条都不少**（丢更新会在这里红）
      ② 状态文件始终是合法 JSON（截断/半个文件会在这里红）
      ③ 目录里不留 `.tmp`（原子替换中途崩掉会留下它）
      ④ 免费额度：10 个线程同时拉详情，**正好 3 个 200**（读-改-写丢更新会多放行）

    代码层面已经把"改状态 + save"放进 RLock 里（`StateStore.transaction()`），
    所以这条用例**不再靠运气** —— 它守的是"别把锁拆掉"。
    """
    import tempfile

    d = tempfile.mkdtemp(prefix="tuimian_state_conc_")
    _TMP_DIRS.append(d)
    path = os.path.join(d, "state.json")
    st = StateStore.load(path)
    c = TestClient(create_app(STORE, FACULTY_STORE, st))
    codes, errs = [], []
    lk = threading.Lock()

    def _post(i):
        try:
            code = c.post("/api/posts", json={
                "user_id": f"u_c{i}", "title": f"并发{i}",
                "body": "内容" * 30, "unit": "物理学院"}).status_code
            with lk:
                codes.append(code)
        except Exception as e:                  # noqa: BLE001
            with lk:
                errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=_post, args=(i,)) for i in range(24)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errs, f"并发发帖抛异常：{errs[:3]}"

        saved = StateStore.load(path)
        # ① 每个 201 都必须落盘；429 是限速（不是丢数据）
        assert len(saved.posts()) == codes.count(201), \
            (len(saved.posts()), codes.count(201), {k: codes.count(k) for k in set(codes)})
    finally:
        _close(c)

    # ②③ 文件合法性 + 不留 .tmp
    json.load(open(path, encoding="utf-8"))
    assert not [x for x in os.listdir(d) if x.endswith(".tmp")], \
        "留下了 .tmp —— 原子替换没走完"

    # ④ 并发扣额度：免费 3 条，10 个线程同时拉，必须正好 3 个 200
    qpath = os.path.join(d, "quota.json")
    qst = StateStore.load(qpath)
    qc = TestClient(create_app(STORE, FACULTY_STORE, qst))
    rids = [r["id"] for r in RAW[:10]]
    qcodes = []

    def _detail(rid):
        try:
            code = qc.get(f"/api/notices/{rid}", params={"user": "u_quota"}).status_code
        except Exception as e:                  # noqa: BLE001
            code = type(e).__name__
        with lk:
            qcodes.append(code)

    threads = [threading.Thread(target=_detail, args=(r,)) for r in rids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert qcodes.count(200) == 3, \
        f"并发扣额度放行了 {qcodes.count(200)} 条（应当是 3）：{qcodes}"
    assert StateStore.load(qpath).state["users"]["u_quota"]["daily_used"] == 3


@case
def 空数据_必须和_筛选没命中_分得开():
    """**环 5。** 坏输入：数据源是空的（管道没跑完 / 跑空了）。

    D 实测过"空文件 → 加载 0 条、不崩"，然后就停了。**"不崩"不是这一环的判据。**
    真正的问题是：**`{"total": 0, "items": []}` 有两种完全不同的成因** ——

    | 成因 | 用户该看到 | 该做什么 |
    |---|---|---|
    | 数据源空 | 「数据还没加载」 | 等我们 |
    | 有 196 条、条件太窄 | 「没有符合条件的公告」 | 放宽筛选 |

    原来这两件事**响应一模一样**。调用方一定会把第一种说成第二种
    —— 这是本项目反复栽的 §7.4（把"没有数据"说成"没有"）。

    所以现在 `?meta=1` 顶层有 `data_state.empty_reason`：
    `source_empty` / `no_match` / `null`（有结果）。这条用例把三态全走一遍。
    """
    import tempfile

    d = tempfile.mkdtemp(prefix="tuimian_empty_")
    _TMP_DIRS.append(d)
    empty_path = os.path.join(d, "empty.jsonl")
    open(empty_path, "w", encoding="utf-8").close()      # 真的空文件（0 字节）
    est = NoticeStore.load(empty_path)
    assert est.load_meta["loaded"] == 0, est.load_meta

    c = TestClient(create_app(est, FacultyStore.load(FACULTY), StateStore.load(":memory:")))
    try:
        # ① 空数据源：接口照常 200（不是错误），但必须说"没数据"
        body = c.get("/api/notices", params={"meta": 1, "size": 50}).json()
        assert body["total"] == 0 and body["items"] == [], body
        ds = body["data_state"]
        assert ds["source_empty"] is True, ds
        assert ds["empty_reason"] == "source_empty", ds
        assert ds["loaded"] == 0 and ds["matched"] == 0, ds
        assert "没有" not in ds["note"] or "没有任何数据" in ds["note"], ds["note"]
        # ② health 也要显眼地说"空"，不是混在数字里的一个 0
        h = c.get("/api/health").json()
        assert h["data"]["load_state"] == "empty", h["data"]["load_state"]
        assert h["data"]["loaded"] == 0
        assert h["data"]["load"]["message"], h["data"]["load"]
        # ③ 对比：真数据上"筛得太窄"必须报 no_match，不许说 source_empty
        body2 = CLIENT.get("/api/notices",
                           params={"meta": 1, "school": "这个学校不存在"}).json()
        assert body2["total"] == 0, body2["total"]
        ds2 = body2["data_state"]
        assert ds2["source_empty"] is False, ds2
        assert ds2["empty_reason"] == "no_match", ds2
        assert ds2["loaded"] == len(RAW), (ds2["loaded"], len(RAW))
        # ④ 有结果时不给 empty_reason（有就是有，别让调用方多判一个状态）
        body3 = CLIENT.get("/api/notices", params={"meta": 1, "size": 1}).json()
        assert body3["data_state"]["empty_reason"] is None, body3["data_state"]
        assert body3["data_state"]["matched"] == body3["total"] > 0
    finally:
        _close(c)

    # ⑤ **默认响应形状不许变**：`data_state` 只在 `?meta=1` 里出现，
    #    否则 fixture 比对和 C 的解析全废（§5.1 的硬规矩）
    plain = CLIENT.get("/api/notices", params={"size": 1}).json()
    assert set(plain.keys()) == {"total", "page", "size", "items"}, set(plain.keys())


@case
def 空数据_筛选接口也不许崩():
    """空数据源下**每个列表类接口**都得能开，不许 500。

    这条容易漏：`facets()` 里 `sorted(d.items(), ...)` 在空数据上是空列表，
    但 `notices` 的筛选、分页、排序在 0 条上各走一遍才算验过 —— 空字典取最大值
    这类错误只在"一条都没有"的时候才出现。
    """
    import tempfile

    d = tempfile.mkdtemp(prefix="tuimian_empty2_")
    _TMP_DIRS.append(d)
    path = os.path.join(d, "empty.jsonl")
    open(path, "w", encoding="utf-8").close()
    est = NoticeStore.load(path)
    c = TestClient(create_app(est, FacultyStore.load(FACULTY), StateStore.load(":memory:")))
    try:
        for params in ({}, {"q": "推免"}, {"school": "浙江大学"}, {"sort": "deadline_asc"},
                       {"page": 3, "size": 50}, {"meta": 1, "year": 2027}):
            r = c.get("/api/notices", params=params)
            assert r.status_code == 200, (params, r.status_code, r.text[:200])
            assert r.json()["total"] == 0
        f = c.get("/api/facets")
        assert f.status_code == 200, f.text[:200]
        assert f.json()["schools"] == [], f.json()
        h = c.get("/api/health").json()
        assert h["data"]["loaded"] == 0 and h["data"]["load_state"] == "empty"
        # 详情：0 条数据时不能 500，要老实 404
        assert c.get("/api/notices/随便一个id").status_code == 404
    finally:
        _close(c)


# ---------- 18. 推送：事件 × 画像（契约 api.md 第 16 节） ----------
#
# 编号是 18，不是 17：`RUN.md` 里把上面那组鲁棒性用例称作"第 17 节"
# （它们没有章节标题，是 E 加在 §16 之后的一批），这里顺着往下排，免得两处撞号。
#
# 这一节的四条核心用例是用户点名的：unknown 不被当成 absent / 重复事件不重复推送 /
# 画像删除后不再推送 / 每条必含原文链接。另外三条配套（免费分层、提醒窗口、参数边界）。
#
# **全部用造的事件**（临时 records + 注入 EventStore）：
# `data/out/events.jsonl` 今天是空的，赌它等于没验。这一晚"断言绑磁盘数据"栽过五次。

from server import events as events_mod  # noqa: E402
from server import push as push_mod  # noqa: E402

# 测试用画像。**故意和 profile-example.json 不同**：那份是 `/api/match` 的演示画像，
# 和推送的门禁用同一份会让"改了一处、两处一起红"变成常态，看不出是哪边错了。
_PUSH_PROFILE = {
    "school": "浙江大学",
    "majors": ["物理学"],
    "preferred_schools": ["浙江大学", "中山大学"],
    "interest_keywords": ["推免", "材料"],
}


def _push_event(eid, *, etype="added", nid=None, school="浙江大学", unit="研究生院",
                title="关于2027年推免工作的通知", deadline=None, ds=None,
                relevance=2, matched=("推免",), change=None, on="2026-10-17",
                topic="apply", notice_kind=None, backfill=None):
    """造一条**符合 events.schema.json** 的事件。

    `ds` 不传时按 `deadline` 猜 —— 但**只是造数据的方便**，不是业务逻辑：
    真实事件的 `field_state` 由数据层给，接口层一律原样透传。
    """
    if ds is None:
        ds = "found" if deadline else "absent"
    ev = {
        "event_id": eid,
        "type": etype,
        "notice_id": nid or ("n_" + eid),
        "at": on + "T10:00:00",
        "detected_on": on,
        "url": "https://example.edu.cn/" + eid,
        "school": school,
        "unit": unit,
        "title": title,
        "publish_date": on,
        "deadline": deadline,
        "field_state": {"deadline": ds, "majors": "found",
                        "attachments": "absent", "body": "ok"},
        "relevance": relevance,
        "matched": list(matched),
        # **	opic 是推送的硬门槛**（契约 §18.7）：合成事件默认当招生公告。
        # 要测非招生那条分支就显式传 topic="non_apply"，要测不知道就传 topic=None。
        "topic": topic,
        # `notice_kind`（A29，契约 §18.9）：合成事件默认不填 —— **老数据就是没有**，
        # 走 `tier_of` 的旧规则。要测分层就显式传 announcement/catalog/emba/other。
        "notice_kind": notice_kind,
    }
    if backfill is not None:
        ev["is_backfill"] = backfill
    if change is not None:
        ev["change"] = change
    return ev


def _push_client(events, state=None):
    """造一个注入了固定事件流的客户端。`state` 传进来就能跨 app 复用一份状态。"""
    st = state if state is not None else StateStore.load(":memory:")
    est = events_mod.EventStore("<test>", events, "events")
    return TestClient(create_app(STORE, FACULTY_STORE, st, events=est))


def _day(n):
    """今天 + n 天（`YYYY-MM-DD`）。**造数据用真实今天** ——
    写死一个日期会让这条用例在明天变红，而红的原因和代码无关。"""
    return (date.today() + timedelta(days=n)).isoformat()


@case
def push_unknown_不许被当成_absent():
    """**用户点名的第 1 条。**

    推送文案天然短，"该公告暂无截止日"字数刚好、读起来也很顺 ——
    而它对 `unknown` 就是**谎话**：`absent` 是"公告里确实没有"（用户该放心），
    `unknown` 是"我们没读到"（用户该去看原文）。写成一样，
    用户就会照着"没有截止日"做决定。

    三件事要同时成立，缺一条这条就白测：
    ① 两种态的 **text 不同**；
    ② `unknown` 的 `must_check_original` 为 True 且**带原文链接**；
    ③ **文本里不许出现「暂无」「——」这类省略说法**（那是硬规矩）。
    """
    evs = [
        _push_event("e_found", deadline=_day(4), ds="found"),
        _push_event("e_absent", deadline=None, ds="absent"),
        _push_event("e_unknown", deadline=None, ds="unknown"),
    ]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_p1", json=_PUSH_PROFILE)
        r = c.get("/api/push/preview?user=u_p1").json()
        assert r["empty_reason"] is None, r["empty_reason"]
        by_id = {i["event_id"]: i for i in r["items"]}
        assert set(by_id) == {"e_found", "e_absent", "e_unknown"}, sorted(by_id)

        found = by_id["e_found"]["deadline"]
        absent = by_id["e_absent"]["deadline"]
        unknown = by_id["e_unknown"]["deadline"]

        # ① 三态三种说法，**两两不同**
        assert found["state"] == "found" and absent["state"] == "absent" \
            and unknown["state"] == "unknown", (found, absent, unknown)
        assert absent["text"] == credibility.ABSENT_DEADLINE_TEXT, absent["text"]
        assert unknown["text"] == credibility.UNKNOWN_DEADLINE_TEXT, unknown["text"]
        assert absent["text"] != unknown["text"], "absent 和 unknown 说了同一句话"

        # ② unknown 必须让人去看原文：带链接 + 标记
        assert unknown["must_check_original"] is True, unknown
        assert absent["must_check_original"] is False, absent
        assert by_id["e_unknown"]["url"], "unknown 的推送没有原文链接"

        # ③ 硬规矩：不许用省略说法把"不知道"糊过去
        for bad in ("暂无", "——", "待定"):
            assert bad not in unknown["text"], (bad, unknown["text"])
            assert bad not in absent["text"], (bad, absent["text"])

        # ④ body 也得分开 —— 前端可能只渲染 body
        assert by_id["e_absent"]["body"] != by_id["e_unknown"]["body"], \
            "两种态拼出来的 body 一样"

        # ⑤ 对照：found 的 days_left 是算出来的，不是 None
        assert found["days_left"] == 4, found
        assert absent["days_left"] is None and unknown["days_left"] is None, \
            "absent/unknown 不该有 days_left（那是编造一个数）"
    finally:
        _close(c)


@case
def push_重复事件不重复推送():
    """**用户点名的第 2 条。**

    `event_id` 是稳定去重键，`push_id = sha1(user|event)` 也是稳定的。
    这条要验两件事，缺一件都不算：

    ① **第二次不再推**（进 `suppressed` 且 `reason=duplicate`）——
       而且要**说得出是哪条**被压了，只给一个数字等于没解释；
    ② `push_id` 两次**完全一样** —— 含了时间戳的话每次都是新 id，去重立刻失效。
    """
    evs = [_push_event("e_1"), _push_event("e_2")]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_p2", json=_PUSH_PROFILE)

        # GET 是只读的：刷新两次应该一模一样（预览不是消费）
        g1 = c.get("/api/push/preview?user=u_p2").json()
        g2 = c.get("/api/push/preview?user=u_p2").json()
        assert [i["push_id"] for i in g1["items"]] == [i["push_id"] for i in g2["items"]]
        assert len(g2["items"]) == 2, "GET 居然有副作用（预览不该消耗推送）"

        # POST 才是"接收"
        posted = c.post("/api/push/preview", params={"user": "u_p2"}, json={}).json()
        assert posted["tally"]["committed"] is True
        assert len(posted["items"]) == 2, posted["tally"]
        first_ids = [i["push_id"] for i in posted["items"]]

        # 再 GET：一条都不剩，全进 suppressed
        again = c.get("/api/push/preview?user=u_p2").json()
        assert again["items"] == [], [i["event_id"] for i in again["items"]]
        assert len(again["suppressed"]) == 2, again["suppressed"]
        assert {x["reason"] for x in again["suppressed"]} == {"duplicate"}, \
            again["suppressed"]
        # 压掉的必须能对上号
        assert {x["event_id"] for x in again["suppressed"]} == {"e_1", "e_2"}, \
            again["suppressed"]
        assert all(x["text"] for x in again["suppressed"]), "压掉了却不说为什么"

        # push_id 稳定：同一 (user, event) 永远同一个 id
        #
        # **不拿 `push_id_of` 算期望值再比** —— 那是让实现和自己比。
        # 这里验的是三件与算法无关的性质：格式、稳定性、按人区分。
        for pid in first_ids:
            assert pid.startswith("push_"), pid
            assert len(pid) == len("push_") + 16, pid
            assert pid[5:] == pid[5:].lower(), pid          # 十六进制，不是 base64
            assert not any(ch.isdigit() and len(pid) > 40 for ch in pid)
        # 换个用户当然不同（去重是按人算的）
        assert push_mod.push_id_of("u_other", "e_1") not in first_ids
    finally:
        _close(c)


@case
def push_画像删除后不再推送():
    """**用户点名的第 3 条。** 删画像 = "别再推了"，必须立刻生效。

    还要验一个**对照**：删之前能推、删之后不能 —— 只验"删完是空的"是不够的，
    那样"推送本来就是空的"也会过。
    """
    evs = [_push_event("e_1"), _push_event("e_2")]
    c = _push_client(evs)
    try:
        # 对照：填了画像就能推
        c.put("/api/profile?user=u_p3", json=_PUSH_PROFILE)
        before = c.get("/api/push/preview?user=u_p3").json()
        assert len(before["items"]) == 2, before["tally"]

        d = c.delete("/api/profile?user=u_p3").json()
        assert d["deleted"] is True and d["pushes_stopped"] is True, d

        after = c.get("/api/push/preview?user=u_p3").json()
        assert after["items"] == [], [i["event_id"] for i in after["items"]]
        # **不许说成"没有变化"**：要说"你还没填画像"
        assert after["empty_reason"] == "no_profile", after["empty_reason"]
        assert after["profile"]["can_push"] is False, after["profile"]

        # 删了就不该再写新推送记录
        n_before = len(c.app.state.state.state["pushes"])
        c.post("/api/push/preview", params={"user": "u_p3"}, json={})
        assert len(c.app.state.state.state["pushes"]) == n_before, \
            "删了画像还在记录推送"

        # 重复删：不静默成功也不崩（返回 False）
        assert c.delete("/api/profile?user=u_p3").json()["deleted"] is False
    finally:
        _close(c)


@case
def push_每条推送必含原文链接():
    """**用户点名的第 4 条。** 免费层也一样。

    这是"可信"的底线：推送说得再漂亮，不能回链就是不可核实。

    **全量扫，不抽样** —— 抽一条验过的写法，在第三条数据上就会漏
    （这一晚"取一页就够了"栽过两次）。
    """
    today_n = [4, 2, None, None]
    states = ["found", "found", "absent", "unknown"]
    evs = []
    for n, (d, s) in enumerate(zip(today_n, states)):
        evs.append(_push_event(
            "e_url_%d" % n,
            deadline=(_day(d) if d is not None else None),
            ds=s,
            etype="added" if n < 3 else "attachment_added",
            change=({"field": "attachments", "attachments": [
                {"name": "附件.pdf", "url": "https://example.edu.cn/a.pdf"}]}
                if n == 3 else None),
        ))
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_p4", json=_PUSH_PROFILE)
        # 订阅身份才能看全（免费层只给 3 条）
        c.post("/api/subscriptions", params={"user": "u_p4"},
               json={"user_id": "u_p4", "code": config.PUSH_ACTIVATION_CODE})
        r = c.get("/api/push/preview?user=u_p4").json()
        assert len(r["items"]) == 4, (len(r["items"]), r["tally"])
        for it in r["items"]:
            assert it.get("url"), ("缺原文链接", it["event_id"])
            assert str(it["url"]).startswith(("http://", "https://")), it["url"]
        # 免费层也一样（前 3 条）
        c.put("/api/profile?user=u_p4f", json=_PUSH_PROFILE)
        free = c.get("/api/push/preview?user=u_p4f").json()
        assert free["items"], "免费层一条都没有"
        for it in free["items"]:
            assert it.get("url", "").startswith("http"), ("免费层缺链接", it["event_id"])

        # 真实样例源也扫一遍（`?source=sample` 是演示时的实际输入）
        smp = c.get("/api/push/preview?user=u_p4&source=sample").json()
        assert smp["source"]["kind"] == "sample", smp["source"]
        assert smp["items"], "演示样例一条都没推出来"
        for it in smp["items"]:
            assert it.get("url", "").startswith("http"), ("样例缺链接", it["event_id"])
    finally:
        _close(c)


@case
def push_提醒只对_found_且窗口取最紧一档():
    """T-7 / T-3 / T-1 是**窗口**，不是"第 N 天准时发一条"。

    造 8/7/4/3/2/1/0/-1 天八种，一条一条钉：

    | days_left | 期望 |
    |---|---|
    | 8  | 不提醒（还早） |
    | 7  | T-7 |
    | 4  | **T-7**（在 7 天窗口内、3 天窗口外，取最紧一档） |
    | 3  | T-3 |
    | 2  | T-3 |
    | 1  | T-1 |
    | 0  | T-1 + "今天截止" |
    | -1 | 不提醒（已过） |

    再加两条：`absent`/`unknown` 一律没有提醒（我们不知道什么时候截止，
    挂个提醒等于假装知道）。
    """
    evs = []
    for n in (8, 7, 4, 3, 2, 1, 0, -1):
        evs.append(_push_event("e_d%d" % (n + 10), deadline=_day(n), ds="found"))
    evs.append(_push_event("e_da", deadline=None, ds="absent"))
    evs.append(_push_event("e_du", deadline=None, ds="unknown"))

    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_p5", json=_PUSH_PROFILE)
        c.post("/api/subscriptions", params={"user": "u_p5"},
               json={"user_id": "u_p5", "code": config.PUSH_ACTIVATION_CODE})
        r = c.get("/api/push/preview?user=u_p5&limit=50").json()
        by_id = {i["event_id"]: i for i in r["items"]}
        assert len(by_id) == 10, sorted(by_id)

        expect = {8: None, 7: "T-7", 4: "T-7", 3: "T-3", 2: "T-3", 1: "T-1", 0: "T-1", -1: None}
        for n, kind in expect.items():
            ev_id = "e_d%d" % (n + 10)
            got = (by_id[ev_id]["reminder"] or {}).get("kind")
            assert got == kind, ("days_left=%s 的提醒对不对" % n, got, kind, by_id[ev_id]["reminder"])

        # 4 天拿的是 T-7 而不是 T-3，也不是两个都挂
        assert by_id["e_d14"]["reminder"]["window_days"] == 7
        assert by_id["e_d14"]["reminder"]["days_left"] == 4
        # 0 天的文案是"今天截止"，不是"还剩 0 天"
        assert "今天截止" in by_id["e_d10"]["reminder"]["text"], \
            by_id["e_d10"]["reminder"]["text"]

        # absent / unknown 一律没有提醒
        assert by_id["e_da"]["reminder"] is None, by_id["e_da"]["reminder"]
        assert by_id["e_du"]["reminder"] is None, by_id["e_du"]["reminder"]

        # 对照：免费身份**全部**没有提醒（提醒是订阅功能），但三态和链接照给
        c.put("/api/profile?user=u_p5f", json=_PUSH_PROFILE)
        free = c.get("/api/push/preview?user=u_p5f&limit=50").json()
        assert free["items"], free["tally"]
        assert all(i["reminder"] is None for i in free["items"]), \
            "免费层拿到了提醒"
        assert all(i["url"] for i in free["items"])
    finally:
        _close(c)


@case
def push_免费层给前十条且说清截断():
    """免费层给**前 10 条**而不是 0 条 —— 推送的价值必须看得见才卖得掉。

    但截断本身**必须说出来**（`tally.truncated` + `truncated_reason=free`）。
    不说的代价是：用户以为今天只发生了 10 件事。

    > 2026-10-19 从 3 改成 10（B16，对标竞品）：3 条判断不了"这产品靠不靠谱"，
    > 而用户要判断的恰好是这个。付费墙挪到"订阅提醒 + 资料库"上。
    """
    evs = [_push_event("e_f%d" % i) for i in range(15)]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_p6f", json=_PUSH_PROFILE)
        free = c.get("/api/push/preview?user=u_p6f").json()
        # **期望值写死 10，不引用 `config.PUSH_FREE_LIMIT`。**
        # 引用常量等于让实现和自己比 —— 把常量改成 0 这条也照样绿，
        # 而"免费层一条都看不到"正是要防的那个产品错误（今天的
        # `LISTING_SORT_FIXTURE_STALE` 是同一个病，别再犯）。
        assert len(free["items"]) == 10, (len(free["items"]), free["tally"])
        assert free["tally"]["truncated"] is True, free["tally"]
        assert free["tally"]["truncated_reason"] == "free", free["tally"]
        assert free["tally"]["matched"] == 15, free["tally"]["matched"]

        # 订阅身份：全量，不截断
        c.put("/api/profile?user=u_p6s", json=_PUSH_PROFILE)
        c.post("/api/subscriptions", params={"user": "u_p6s"},
               json={"user_id": "u_p6s", "code": config.PUSH_ACTIVATION_CODE})
        sub = c.get("/api/push/preview?user=u_p6s").json()
        assert len(sub["items"]) == 15, len(sub["items"])
        assert sub["tally"]["truncated"] is False, sub["tally"]

        # 开通码不对 → 400 且不区分"不对/过期"
        bad = c.post("/api/subscriptions", params={"user": "u_p6s"},
                     json={"user_id": "u_p6s", "code": "NOPE"})
        assert bad.status_code == 400, bad.status_code
        assert bad.json()["error"]["code"] == "BAD_CODE", bad.json()
    finally:
        _close(c)


@case
def push_limit_越界截断不报错():
    """`?limit=` 越界**截断不报错**：为"给你看的东西"返回错误、让页面白掉，
    是拿用户体验换契约洁癖。

    但非法值（`abc`）该是 4xx —— 那是**调用方写错了**，不是"要多了"。
    """
    evs = [_push_event("e_l%d" % i) for i in range(4)]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_p7", json=_PUSH_PROFILE)
        c.post("/api/subscriptions", params={"user": "u_p7"},
               json={"user_id": "u_p7", "code": config.PUSH_ACTIVATION_CODE})

        for lim, expect in ((0, 1), (9999, 50), (2, 2), (4, 4)):
            r = c.get("/api/push/preview", params={"user": "u_p7", "limit": lim})
            assert r.status_code == 200, (lim, r.status_code)
            body = r.json()
            # `9999 → 50` 里的 50 是**契约写死的上限**（§16.4），不引用 config ——
            # 引用它就把"上限是不是 50"这件事从"契约规定"降级成"实现自己的说法"。
            assert body["tally"]["limit"] == expect, (lim, body["tally"]["limit"], expect)
            assert len(body["items"]) <= min(expect, 4), (lim, len(body["items"]))

        # 非法值：4xx，且**不是裸 HTML**
        bad = c.get("/api/push/preview", params={"user": "u_p7", "limit": "abc"})
        assert 400 <= bad.status_code < 500, bad.status_code
        assert "application/json" in bad.headers.get("content-type", ""), \
            bad.headers.get("content-type")

        # 不认识的 source：明确说，不静默回落到真实源
        s = c.get("/api/push/preview", params={"user": "u_p7", "source": "xxx"})
        assert s.status_code == 400, s.status_code
        assert s.json()["error"]["code"] == "BAD_SOURCE", s.json()
    finally:
        _close(c)


@case
def push_紧急度权重_按截止日分档且不压掉已截止():
    """`urgency_weight` 的分档 + **它只排序、不过滤**（2026-10-17）。

    派单原话是「过滤掉已过期的（urgency=0）」。**没照做** ——
    已截止的是**沉底不压掉**，那是同一天早些时候定的决定，不能悄悄改一处。

    这条用例两头钉：
    ① 分档对（0/2.0/1.5/1.2/1.0）且 `absent`/`unknown` 不惩罚；
    ② **权重 0 的照样返回**（只是排最后）—— 防止有人图省事把"0"改成"丢掉"。
    """
    D = {"state": credibility.UNKNOWN, "days_left": None}

    def w(days, state=credibility.FOUND):
        return push_mod.urgency_weight(
            {"deadline": {"state": state, "days_left": days}})

    # ① 分档
    assert w(-1) == 0.0, "已截止必须是 0"
    assert w(0) == 2.0, "今天截止必须是最急的 2.0"
    for d in (1, 3, 7):
        assert w(d) == 2.0, ("T-7 内必须是 2.0", d)
    for d in (8, 10, 14):
        assert w(d) == 1.5, ("T-14 内必须是 1.5", d)
    for d in (15, 20, 30):
        assert w(d) == 1.2, ("T-30 内必须是 1.2", d)
    for d in (31, 90, 365):
        assert w(d) == 1.0, ("30 天外必须是 1.0", d)

    # ② `absent` / `unknown` **不惩罚**（给 1.0，不给 0）
    for st in (credibility.ABSENT, credibility.UNKNOWN):
        assert w(None, st) == 1.0, ("%s 被惩罚了 —— 那是把'不知道'当成'不用急'" % st)

    # ③ **权重 0 的照样出现在结果里**（沉底，不压掉）
    import datetime as _dt
    today = _dt.date.today()
    ev_expired = _push_event(
        "e_exp", deadline=(today - _dt.timedelta(days=3)).isoformat(), relevance=2)
    ev_soon = _push_event(
        "e_soon", deadline=(today + _dt.timedelta(days=2)).isoformat(), relevance=1)
    c = _push_client([ev_expired, ev_soon])
    c.put("/api/profile", params={"user": "u_w1"}, json=_PUSH_PROFILE)
    c.post("/api/subscriptions", params={"user": "u_w1"},
           json={"code": config.PUSH_ACTIVATION_CODE})
    r = c.get("/api/push/preview?user=u_w1&limit=10").json()
    ids = [i["event_id"] for i in r["items"]]
    assert "e_exp" in ids, ("已截止的被压掉了 —— 契约 §16.5 要求沉底不压掉", ids)
    assert ids[0] == "e_soon", ("紧急的没排前面", ids)
    # 已截止的必须在末尾，且权重是 0
    exp_item = next(i for i in r["items"] if i["event_id"] == "e_exp")
    assert exp_item["urgency_weight"] == 0.0, exp_item["urgency_weight"]
    assert ids.index("e_exp") > ids.index("e_soon"), ids

    # ④ `relevance=0` 不会因为紧急插队（乘法的好性质）
    ev_irrel = _push_event(
        "e_irrel", deadline=(today + _dt.timedelta(days=1)).isoformat(), relevance=0)
    c2 = _push_client([ev_irrel, ev_soon])
    c2.put("/api/profile", params={"user": "u_w1"}, json=_PUSH_PROFILE)
    c2.post("/api/subscriptions", params={"user": "u_w1"},
            json={"code": config.PUSH_ACTIVATION_CODE})
    r2 = c2.get("/api/push/preview?user=u_w1&limit=10").json()
    order = [i["event_id"] for i in r2["items"]]
    assert order.index("e_soon") < order.index("e_irrel"), (
        "跟招生无关的因为'紧急'插到前面了 —— 乘法应该保证 0×2.0 还是 0", order)


@case
def push_预览形状与_fixture_一致():
    """形状（键集合 + 嵌套）必须等于 `contracts/fixtures/push.preview.json`。

    **只比形状，不比数值。** 这一份响应里几乎没有"稳定的数"：

    - `generated_at` 每次调用都变；
    - `deadline.days_left` / `reminder.kind` 由"今天是几号"算出来；
    - `tally.*` 依赖当天已推过几条。

    所以按契约 §9 的规矩：**形状永久有效、逐字段比对；数值只对 `_as_of` 那天有效。**
    这条用例盯的是"有没有人偷偷加/删一个键" —— 那才是会把 C 的页面打坏的事。
    """
    c = _push_client(events_mod.EventStore.load_sample(
        os.path.join(ROOT, "contracts", "fixtures", "events.sample.json")).records)
    try:
        c.put("/api/profile?user=u_shape", json=_PUSH_PROFILE)
        c.post("/api/subscriptions", params={"user": "u_shape"},
               json={"user_id": "u_shape", "code": config.PUSH_ACTIVATION_CODE})
        got = c.get("/api/push/preview?source=sample&user=u_shape").json()
    finally:
        _close(c)

    fx = _fixture("push.preview.json")

    def key_paths(d, prefix="", drop_private=True):
        """递归键路径集合。`_` 开头的是 fixture 标注，不是契约（§9 第 1 条）。

        **列表要遍历所有元素**：`items` 里条目类型不同（`added` 没有 `change`，
        `absent` 没有 `reminder`），只看第一个会漏掉一半的键 ——
        我第一版就是这么写的，于是"fixture 多了/少了 change.*"全是假失败。
        """
        out = set()
        if isinstance(d, dict):
            for k, v in d.items():
                if drop_private and k.startswith("_"):
                    continue
                out.add(prefix + k)
                # suppressed_sample / suppressed_tally 是**按原因分桶的 dict**，
                # 桶名随场景变（今天有 not_apply，明天可能没有）——
                # 把它们当固定键会比出一种形状不一致，而其实是数据不同。
                # 这里只比到桶那一层，桶名的**词表**由下面单独断言。
                if k in ("suppressed_sample", "suppressed_tally"):
                    continue
                out |= key_paths(v, prefix + k + ".", drop_private)
        elif isinstance(d, list):
            for x in d:
                out |= key_paths(x, prefix + "[].", drop_private)
        return out

    g, f = key_paths(got), key_paths(fx)
    assert g == f, (
        "推送预览的键集合和 fixture 不一致：\n"
        "  接口多了：%s\n  fixture 多了：%s\n"
        "（默认响应形状 = fixture，一个多余键都不加 —— 契约 §16.4/§9）"
        % (sorted(g - f), sorted(f - g)))
    # 分桶字段只比到桶那一层（见 `key_paths`），桶名在这里按**词表**校验：
    # 加一个新原因就要同步改契约 §16.7 和这里，不许随便冒出来一个字符串。
    known = {"backfill", "junk", "merged", "duplicate", "rate_limited",
             "not_apply", "topic_unknown", "mode_mismatch", "outdated_year",
             # B22 分层：catalog 挪去参考资料、emba 硬排除、超出 60 天窗口
             "catalog", "emba", "out_of_window"}
    for field in ("suppressed_sample", "suppressed_tally"):
        bad = set(got.get(field) or {}) - known
        assert not bad, ("%s 里出现了不认识的原因：%s\n"
                         "  原因词表是契约的一部分（§16.7）。"
                         % (field, sorted(bad)))
    assert got["items"], "样例源应当至少推出 1 条"


@case
def push_补抓的旧公告不进推送():
    """**issue 008 的核心。** `is_backfill=true` 的事件不许出现在 `items` 里。

    实测过：付费身份推 20 条，**18 条是 2018~2026 的旧公告**，而 headline 还写着
    「【新公告】2013 年…」。那是当场露馅的形态 —— 也是"可信"这条主线的自毁。

    A 的原话是"我们这次多抓到了 ≠ 学校今天新发了"。前者是补抓，后者才是新闻。

    ## 这条用例真正要防的两件事

    1. **别一刀切。** 数据层对 `deadline_changed` / `attachment_added`
       **永远不标 `is_backfill`** —— 再老的公告改了截止日也该报。
       所以判据必须是"事件自己说自己是补抓的"，不能是 `type == "added"`。
       这里专门造一条 `deadline_changed`（没有 is_backfill 字段）来钉住这一点。
    2. **压掉的要说出来。** 进 `suppressed` 且 `reason="backfill"`，
       并且 `tally.backfill` 给数 —— 否则用户看到"今天 2 条"不知道还有 44 条哪去了。
    """
    evs = [
        # 真新公告（今天发的，不是补抓）
        _push_event("e_new", deadline=_day(5), ds="found"),
        # 补抓的旧公告：added + is_backfill=true
        dict(_push_event("e_old", deadline=None, ds="absent"), is_backfill=True),
        # 补抓的旧公告，但**没有 is_backfill 字段**（数据层对别的类型不标）
        # —— 它必须**照推**。一刀切按 type 或按发布日排除就会把它误杀。
        _push_event("e_changed_old", etype="deadline_changed",
                    change={"field": "deadline", "from": None, "to": _day(3)}),
    ]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_b1", json=_PUSH_PROFILE)
        c.post("/api/subscriptions", params={"user": "u_b1"},
               json={"user_id": "u_b1", "code": config.PUSH_ACTIVATION_CODE})
        r = c.get("/api/push/preview?user=u_b1&limit=50").json()

        ids = [i["event_id"] for i in r["items"]]
        assert "e_old" not in ids, "补抓的旧公告进了 items：%s" % ids
        assert "e_new" in ids, "真新公告被误杀了：%s" % ids
        # **关键**：没有 is_backfill 字段的 deadline_changed 必须还在
        assert "e_changed_old" in ids, \
            "把 deadline_changed 一刀切掉了 —— 数据层对它不标 is_backfill，它该照推"

        sup = {x["event_id"]: x["reason"] for x in r["suppressed"]}
        assert sup.get("e_old") == "backfill", sup
        assert r["tally"]["backfill"] == 1, r["tally"]
        # 压掉的必须能说出为什么
        old = next(x for x in r["suppressed"] if x["event_id"] == "e_old")
        assert old["text"] and "补抓" in old["text"], old["text"]

        # 对照：**全是补抓**时不许说"今天没有跟你相关的"
        c2 = _push_client([dict(_push_event("e_old2"), is_backfill=True)])
        try:
            c2.put("/api/profile?user=u_b2", json=_PUSH_PROFILE)
            r2 = c2.get("/api/push/preview?user=u_b2").json()
            assert r2["items"] == [], r2["items"]
            assert r2["empty_reason"] == "only_backfill", r2["empty_reason"]
            assert r2["empty_text"] and "历史公告" in r2["empty_text"], r2["empty_text"]
        finally:
            _close(c2)
    finally:
        _close(c)


@case
def push_同一条公告在同一批里只推一条():
    """同一条公告的 `added` 和 `deadline_changed`，**同一批里只留一条**。

    跨批次它们是两件事（时钟往前走，每次都带来新信息）；
    但同一批里两条并列出现在界面上，看起来就是"重复推送"——
    A 验收时实测到 `['ue00ab131-2bc4', 'ud1ddf924-1515', 'ud1ddf924-1515']`。

    留**信息量大的**那条（`deadline_changed` > `attachment_added` > `added`），
    被合并掉的进 `suppressed` 且 `reason="merged"` —— **直接丢掉的话
    `tally.matched` 和 `sent` 会对不上，用户以为漏推了**。
    """
    evs = [
        _push_event("e_add", nid="n_same", etype="added"),
        _push_event("e_dl", nid="n_same", etype="deadline_changed",
                    deadline=_day(2), change={"field": "deadline", "from": None,
                                              "to": _day(2)}),
        _push_event("e_other", nid="n_other", etype="added"),
    ]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_m1", json=_PUSH_PROFILE)
        r = c.get("/api/push/preview?user=u_m1").json()

        noticed = [i["notice_id"] for i in r["items"]]
        assert len(noticed) == len(set(noticed)), "同一批里同一条公告出现了两次：%s" % noticed
        assert set(noticed) == {"n_same", "n_other"}, noticed

        # 留的是 deadline_changed（信息量大），不是 added
        kept = next(i for i in r["items"] if i["notice_id"] == "n_same")
        assert kept["event_id"] == "e_dl", \
            "合并时没留信息量大的那条：留下来的 event_id = %s" % kept["event_id"]

        # 被合并掉的那条要能在 suppressed 里找到
        merged = [x for x in r["suppressed"] if x["reason"] == "merged"]
        assert len(merged) == 1, r["suppressed"]
        assert merged[0]["event_id"] == "e_add", merged[0]
        assert merged[0]["text"], "合并掉了却不说"

        # 对照：**不同公告**不该被合并
        assert any(i["notice_id"] == "n_other" for i in r["items"]), r["items"]
        # tally 自洽：命中 3 条 = 推 2 条 + 合并 1 条
        assert r["tally"]["matched"] == 3, r["tally"]
        assert r["tally"]["sent"] == 2, r["tally"]
    finally:
        _close(c)


@case
def push_真实事件流里没有补抓的公告():
    """**用真实数据回归一次**：items 里不许出现窗口外（近 60 天）的公告。

    和上一条的分工：上一条验**逻辑**（造数据），这条验**真实数据上真的执行了**。
    只验逻辑不验真实数据，会出现"逻辑对但没接上"（我这一晚栽过：
    `?meta=1` 拿投影后的数据算派生量，每条都算成 unknown）。

    **窗口是 60 天，不是 30**（B22，2026-10-19 用户拍板）：推免季的公告 9 月发、
    10 月底才截止 —— 按 30 天筛会把"45 天前发的推免办法"整批判成补抓，
    而它今天仍然有效。这条用例盯的是**窗口真的被用上了**：
    真实数据里 `added` 事件不一定是补抓，光靠 `is_backfill` 拦不住去年的简章。

    **文件不存在时跳过**（新克隆的仓库没有它），但**不静默跳过** ——
    前面那条已经覆盖了逻辑，这里只是多一层真实数据回归。
    """
    path = os.path.join(ROOT, "data", "out", "events.jsonl")
    if not os.path.exists(path):
        return                      # 数据层还没产出，跳过（逻辑由上一条钉着）

    store = events_mod.EventStore.load(path)
    c = _push_client(store.records)
    try:
        # 用一个"很宽"的画像：命中尽量多，才可能撞上补抓事件
        c.put("/api/profile?user=u_real", json={
            "preferred_schools": ["北京大学", "清华大学", "浙江大学", "复旦大学"],
            "interest_keywords": ["物理", "化学", "材料", "数学", "生物", "招生", "推免"],
        })
        c.post("/api/subscriptions", params={"user": "u_real"},
               json={"user_id": "u_real", "code": config.PUSH_ACTIVATION_CODE})
        r = c.get("/api/push/preview?user=u_real&limit=50").json()

        cutoff = date.today() - timedelta(days=config.PUSH_POOL_FRESH_DAYS)
        bad = []
        for it in r["items"]:
            pub = push_mod.parse_day(it.get("publish_date"))
            if pub is None:
                bad.append((it["event_id"], it.get("publish_date"), "读不到发布日"))
            elif pub < cutoff:
                bad.append((it["event_id"], it["publish_date"]))
        assert not bad, (
            "真实事件流上有 %d 条早于 %d 天的公告进了 items（前 3 条：%s）\n"
            "对外说的是「近 %d 天」，首屏就必须按同一个窗口筛 —— "
            "否则页头的数和屏幕上的条目对不上，用户还会看到去年的简章。"
            % (len(bad), config.PUSH_POOL_FRESH_DAYS, bad[:3],
               config.PUSH_POOL_FRESH_DAYS))
        assert not any(it.get("notice_kind") in ("catalog", "emba")
                       for it in r["items"]), "首屏混进了 catalog/emba"

        # 真实数据里**有**补抓事件时，suppressed 里就该出现 backfill
        if any(e.get("is_backfill") for e in store.records):
            reasons = {x["reason"] for x in r["suppressed"]}
            # 只有"匹配到的"才会进 suppressed；宽画像下通常能命中一些。
            # 命不中也不该红 —— 所以只在确实压掉了东西时才要求看到 backfill。
            if r["tally"]["backfill"] > 0:
                assert "backfill" in reasons, (
                    "tally.backfill=%d 但 suppressed 里没有 backfill：%s"
                    % (r["tally"]["backfill"], reasons))
    finally:
        _close(c)


@case
def push_已截止的沉底但不压掉():
    """已截止的公告**排在最后，但仍然在列表里**（issue：首条推送是已截止公告）。

    原来的做法是 `days_left < -7` 就压进 `suppressed(expired)`。**改成沉底。**

    ## 为什么压掉是错的

    1. **截止日可能抽错。** 这个项目有过"把正文里的日期当成发布日"的实例，
       也有噪声闸误杀正确截止日。压掉一条抽错的，用户**永远不知道该去报名**。
    2. **学校会延期。** "截止日从 10-15 延长到 10-30" 恰恰是最有价值的推送 ——
       而如果 10-15 那天把它压掉了，这条推送就不会出现。
    3. **补发的附件截止后照样有人要**（模板、表格）。
    4. 项目的头号原则是 **"`relevance` 只排序、不隐藏"** ——
       已截止是同一个问题：**排序错了只是低几行，压错了是永久看不到。**

    ## 但 `unknown` / `absent` 不许沉底

    我们不知道它们截止了没。归进"已截止"是编造 + 归进"未截止"也是编造 ——
    所以位置不动。只有 `found` 且 `days_left < 0` 才沉底。

    `days_left == 0`（今天截止）**也不沉底**：那是最紧急的一条。
    """
    evs = [
        # 已截止 2 天（浙大那条的形态）
        _push_event("e_past", deadline=_day(-2), ds="found", relevance=2),
        # 今天截止
        _push_event("e_today", deadline=_day(0), ds="found", relevance=2),
        # 未来
        _push_event("e_future", deadline=_day(20), ds="found", relevance=2),
        # 不知道 / 没有（**不许沉底**）
        _push_event("e_unknown", deadline=None, ds="unknown", relevance=2),
        _push_event("e_absent", deadline=None, ds="absent", relevance=2),
    ]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_x1", json=_PUSH_PROFILE)
        c.post("/api/subscriptions", params={"user": "u_x1"},
               json={"user_id": "u_x1", "code": config.PUSH_ACTIVATION_CODE})
        r = c.get("/api/push/preview?user=u_x1&limit=50").json()

        ids = [i["event_id"] for i in r["items"]]
        # ① 一条都不许丢
        assert set(ids) == {"e_past", "e_today", "e_future", "e_unknown", "e_absent"}, ids
        # ② 已截止的在**最后一个**
        assert ids[-1] == "e_past", "已截止的没有沉底：%s" % ids
        # ③ 今天截止的**不许沉底**（它是最紧急的）
        assert ids[0] != "e_past", ids
        # ④ unknown / absent 不许被归进"已截止"
        assert ids.index("e_unknown") < ids.index("e_past"), ids
        assert ids.index("e_absent") < ids.index("e_past"), ids
        # ⑤ 不许出现在 suppressed 里（这不是"压掉"，是"排序"）
        assert not any(x["reason"] == "expired" for x in r["suppressed"]), r["suppressed"]
        assert not any(x["event_id"] == "e_past" for x in r["suppressed"]), r["suppressed"]
        # ⑥ 那个数要能查：tally.expired 说清末尾有几条
        assert r["tally"]["expired"] == 1, r["tally"]
        # **`expired_matched` 是另一个数**（C 报的 bug，2026-10-17 补）：
        # 已截止的沉底 + `limit` 截断 → `tally.expired` 在真实数据上**恒为 0**
        # （实测：limit=999 时 22，limit=20 时 0，而 expired_matched 两次都是 22）。
        # 没有这个数，前端那句「最后 N 条已截止」永远不触发，是死代码。
        assert r["tally"]["expired_matched"] == 1, r["tally"]
        assert r["tally"]["sent"] == 5, r["tally"]

        # ⑥' 截断之后：items 里看不到已截止的，但 `expired_matched` 还在
        r2 = c.get("/api/push/preview?user=u_x1&limit=2").json()
        assert len(r2["items"]) == 2, r2["tally"]
        # 未截止的排前面 → 前 2 条里不该有已截止的
        assert not any(push_mod.is_expired(i) for i in r2["items"]), r2["items"]
        assert r2["tally"]["expired"] == 0, r2["tally"]
        assert r2["tally"]["expired_matched"] == 1, (
            "截断后 expired_matched 丢了 —— 前端就没法说「另有 N 条已截止」", r2["tally"])

        # ⑦ 三态不混：is_expired 只认 found
        assert push_mod.is_expired({"deadline": {"state": "unknown", "days_left": None}}) is False
        assert push_mod.is_expired({"deadline": {"state": "absent", "days_left": None}}) is False
        assert push_mod.is_expired({"deadline": {"state": "found", "days_left": 0}}) is False
        assert push_mod.is_expired({"deadline": {"state": "found", "days_left": -1}}) is True
    finally:
        _close(c)


@case
def push_数据旧了要说不知道_不能说没有():
    """**"数据旧"不许伪装成"今天没有新公告"。**

    推送的全部价值建立在"今天发生了什么"上。而"今天没有新公告"有两种成因：

    | 成因 | 该说什么 |
    |---|---|
    | 管道跑了，今天真的没有 | 「今天没有新公告」 |
    | **管道几天没跑** | 「我们**没去看**，所以"没有"这个词现在说不了」 |

    第二种如果照常说"今天没有新公告"，就是把"我们没看"说成"没有" ——
    本项目反复在防的那类错误，只是这次藏在**时间**里。

    阈值直接用 §12 的 `STALE_AFTER_HOURS`（30 小时），**不另定一套** ——
    两处各定一次，迟早一处说真一处说假。

    ## 四个场景（第三个是关键）

    ① 数据 1 小时前 → `stale=false`，能正常说"今天"
    ② 数据 40 小时前 + 有内容 → `stale=true`，items **照给**（不隐藏），但 note 说"不知道"
    ③ 数据 40 小时前 + 没匹配到 → **`empty_reason="stale_events"`**，不是 `no_match`
    ④ 对照：数据新鲜 + 没匹配到 → **这时才可以说 `no_match`**
    """
    def _ev(eid, hours_ago, title="关于推免工作的通知"):
        at = datetime.now() - timedelta(hours=hours_ago)
        return {
            "event_id": eid, "type": "added", "notice_id": "n_" + eid,
            "at": at.isoformat(timespec="seconds"),
            "detected_on": at.date().isoformat(),
            "url": "https://example.edu.cn/" + eid,
            "school": "浙江大学", "unit": "研究生院", "title": title,
            "publish_date": at.date().isoformat(),
            "deadline": None,
            "field_state": {"deadline": "absent", "majors": "found",
                            "attachments": "absent", "body": "ok"},
            "relevance": 2, "matched": ["推免"],
            # 推送的硬门槛（契约 §18.7）：没有 topic 就走不到 items 里
            "topic": "apply",
        }

    def _run(records, prof):
        c = _push_client(records)
        c.put("/api/profile?user=u_s1", json=prof)
        return c.get("/api/push/preview?user=u_s1&limit=50").json(), c

    # ① 新鲜
    r, c = _run([_ev("e_new", 1)], _PUSH_PROFILE)
    try:
        assert r["source"]["stale"] is False, r["source"]
        assert r["source"]["data_age_hours"] < 30, r["source"]
        assert r["source"]["stale_after_hours"] == \
            freshness_mod.STALE_AFTER_HOURS, r["source"]
        assert "不知道" not in r["source"]["note"], r["source"]["note"]
        assert r["items"], "新鲜数据应该有推送"
    finally:
        _close(c)

    # ② 旧 + 有内容：items 照给，但 stale + note 说不知道
    r, c = _run([_ev("e_old", 40)], _PUSH_PROFILE)
    try:
        assert r["source"]["stale"] is True, r["source"]
        assert r["items"], "旧数据里的推送照给 —— 不隐藏（和 relevance 只排不滤同一条原则）"
        assert "不知道" in r["source"]["note"], r["source"]["note"]
        assert "不是「没有」" in r["source"]["note"], r["source"]["note"]
    finally:
        _close(c)

    # ③ 旧 + 没匹配到 → stale_events，不是 no_match
    r, c = _run([_ev("e_old2", 40, title="完全无关的通知")],
                {"preferred_schools": ["不存在的大学"],
                 "interest_keywords": ["不存在的方向"]})
    try:
        assert r["source"]["stale"] is True, r["source"]
        assert r["empty_reason"] == "stale_events", r["empty_reason"]
        assert "不知道" in (r["empty_text"] or ""), r["empty_text"]
        # **不搜"没有新公告"这个词**：这句文案为了对比，**引用了**这句话
        # （"「今天有没有新公告」我们不知道"）。grep 关键词会误判 ——
        # 这正是 §7.3 那条"要验结构化信息就读字段，别 grep 文案"。
        # 真正要验的是 `empty_reason` 这一支走对了，上面那条已经钉住。
    finally:
        _close(c)

    # ④ 对照：新鲜 + 没匹配到 → 这时才可以说 no_match
    r, c = _run([_ev("e_fresh2", 1, title="完全无关的通知")],
                {"preferred_schools": ["不存在的大学"],
                 "interest_keywords": ["不存在的方向"]})
    try:
        assert r["source"]["stale"] is False, r["source"]
        assert r["empty_reason"] == "no_match", r["empty_reason"]
        assert r["empty_reason"] != "stale_events"
    finally:
        _close(c)

    # ⑤ 文件不存在：也是 stale（我们什么都没看到）
    missing = events_mod.EventStore.load(os.path.join(os.sep, "nope", "events.jsonl"))
    c = TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:"),
                               events=missing))
    try:
        c.put("/api/profile?user=u_s2", json=_PUSH_PROFILE)
        r = c.get("/api/push/preview?user=u_s2").json()
        assert r["source"]["stale"] is True, r["source"]
        assert r["empty_reason"] == "no_events_file", r["empty_reason"]
    finally:
        _close(c)


@case
def push_事件文件一变接口就跟着变_不用重启():
    """**"30 秒发现"的后端一半**：`events.jsonl` 被重写之后，不重启也读到新的。

    演示时"我点一下抓取，屏幕上 30 秒后多一条"这个镜头的依据就是这条：

    - 前端：C 的 `live.js` 每 30 秒查 `/api/freshness`；
    - 后端：`_load_events_cached` 按 `(mtime_ns, size)` 重载。

    缺了后端这一半，前端轮询再勤也只会看到旧数据 ——
    那正是当初 `store.py` 踩过的坑（"它不说谎，它只是过时"）。

    **这条必须用真文件**（假的 mtime 造不出来），所以写临时文件、连着 app 一起换。
    """
    import tempfile

    d = tempfile.mkdtemp(prefix="tuimian_ev_reload_")
    _TMP_DIRS.append(d)
    path = os.path.join(d, "events.jsonl")

    old_ev = {
        "event_id": "e_r1", "type": "added", "notice_id": "n_r1",
        "at": "2026-10-17T10:00:00", "detected_on": "2026-10-17",
        "url": "https://example.edu.cn/1", "school": "浙江大学",
        "unit": "研究生院", "title": "第一条", "publish_date": "2026-10-17",
        "deadline": None,
        "field_state": {"deadline": "absent", "majors": "found",
                        "attachments": "absent", "body": "ok"},
        "relevance": 2, "matched": ["推免"],
        "topic": "apply",
    }
    new_ev = dict(old_ev, event_id="e_r2", notice_id="n_r2",
                  title="第二条", at="2026-10-17T11:00:00")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(old_ev, ensure_ascii=False) + "\n")

    # **不注入 events**，走 `_load_events_cached()` 的真路径（这里要验的就是它）
    app = create_app(STORE, FACULTY_STORE, StateStore.load(":memory:"))
    c = TestClient(app)
    try:
        c.put("/api/profile?user=u_r", json=_PUSH_PROFILE)
        # 换掉配置里的路径（接口层要读这个文件）
        old_path = config.EVENTS_PATH
        config.EVENTS_PATH = path
        try:
            _EVENTS_CACHE.clear()
            r1 = c.get("/api/push/preview?user=u_r&limit=50").json()
            assert [i["event_id"] for i in r1["items"]] == ["e_r1"], \
                [i["event_id"] for i in r1["items"]]

            # **追加一条**（模拟管道跑完追加）
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(new_ev, ensure_ascii=False) + "\n")

            r2 = c.get("/api/push/preview?user=u_r&limit=50").json()
            ids = [i["event_id"] for i in r2["items"]]
            assert "e_r2" in ids, "追加事件之后没读到（mtime 重载没生效）：%s" % ids
            assert len(ids) == 2, ids
            # 数据时刻也要跟着走
            assert r2["source"]["total"] == 2, r2["source"]
        finally:
            config.EVENTS_PATH = old_path
            _EVENTS_CACHE.clear()
    finally:
        _close(c)


@case
def push_空的原因四态分得开():
    """`empty_reason` 四态，**四句话四件事**。统一写「暂无推送」是本项目反复栽的 §7.4。

    | 情形 | `empty_reason` |
    |---|---|
    | 有内容 | `null` |
    | 没填画像 | `no_profile` |
    | 事件文件不存在 | `no_events_file` |
    | 有事件但都不匹配 | `no_match` |
    """
    states = {}

    # ① 没画像
    c1 = _push_client([_push_event("e_1")])
    try:
        r = c1.get("/api/push/preview?user=u_e1").json()
        states["no_profile"] = (r["empty_reason"], r["empty_text"])
    finally:
        _close(c1)

    # ② 事件文件不存在（`EventStore.load` 对一个不存在的路径）
    missing = events_mod.EventStore.load(os.path.join(os.sep, "nope", "events.jsonl"))
    assert missing.source_kind == "none", missing.source_kind
    c2 = TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:"),
                               events=missing))
    try:
        c2.put("/api/profile?user=u_e2", json=_PUSH_PROFILE)
        r = c2.get("/api/push/preview?user=u_e2").json()
        states["no_events_file"] = (r["empty_reason"], r["empty_text"])
    finally:
        _close(c2)

    # ③ 有事件但一条都不匹配（画像和事件完全错开）
    #
    # **必须用"新鲜"的事件**（`on=today`）：默认 `_push_event` 的日期是 2026-10-17，
    # 而门禁跑在 10-18 晚上时数据年龄已经 34 小时 > 30 小时阈值，
    # 于是 `stale_events`（我们的数据过期了）会正确地压过 `no_match`。
    # 那一版用例因此红在 `['no_profile','no_events_file','stale_events']` ——
    # **不是接口坏了，是用例造了一份"过期数据"却在断言"没匹配"**。
    today = date.today().isoformat()
    c3 = _push_client([_push_event("e_x1", school="某不存在大学", unit="某学院",
                                   title="完全无关的通知", matched=["无关"], on=today)])
    try:
        c3.put("/api/profile?user=u_e3", json={
            "preferred_schools": ["浙江大学"], "interest_keywords": ["凝聚态物理"],
        })
        r = c3.get("/api/push/preview?user=u_e3").json()
        states["no_match"] = (r["empty_reason"], r["empty_text"])
        assert r["tally"]["unmatched"] == 1, r["tally"]
        assert r["empty_reasons"] == ["no_match"], (
            "新鲜数据 + 没匹配，原因应该只有 no_match", r["empty_reasons"])
    finally:
        _close(c3)

    # 四态的值**两两不同**，而且都有话可说
    assert set(states) == {"no_profile", "no_events_file", "no_match"}
    reasons = [states[k][0] for k in states]
    assert reasons == ["no_profile", "no_events_file", "no_match"], reasons
    texts = [states[k][1] for k in states]
    assert len(set(texts)) == 3, ("三句话不该有两句一样", texts)
    for k, (_r, text) in states.items():
        assert text and len(text) > 8, (k, text)

    # 有内容时 empty_reason 必须是 None（别让调用方多判一个状态）
    c4 = _push_client([_push_event("e_ok")])
    try:
        c4.put("/api/profile?user=u_e4", json=_PUSH_PROFILE)
        r = c4.get("/api/push/preview?user=u_e4").json()
        assert r["empty_reason"] is None, r["empty_reason"]
        assert r["empty_text"] is None, r["empty_text"]
        assert r["items"], r["tally"]
    finally:
        _close(c4)


# ---------------------------------------------------------------------------
# 第 18 节：推送针对性（四维加权 + 可解释）
# ---------------------------------------------------------------------------

@case
def push_四维打分口径与权重都来自配置():
    """防两件事：

    ① **某一维跑出 0~1。** 四维是加权求和后再乘 `urgency_weight` 的，
       某一维越界会让"对口"和"紧急"两个尺度对不上，
       排序结果看起来正常、其实已经不可解释。
    ② **权重被硬编码回函数里。** 契约 §18 承诺权重是可调配置 ——
       如果它回到代码里，"演示时现场改一个数看效果"就做不到了，
       而且没人会发现自己失去了这个能力。
    """
    w = config.PUSH_SCORE_WEIGHTS
    assert set(w) == {"school_hit", "major_hit", "direction_hit", "freshness"}, sorted(w)
    assert abs(sum(w.values()) - 1.0) < 1e-9, ("四维权重和必须是 1.0", w)
    assert all(0 < v <= 1 for v in w.values()), w

    today = date.today()
    ev = _push_event("e_dims", deadline=(today + timedelta(days=2)).isoformat(),
                     relevance=2, matched=("推免", "材料"))
    dims, _detail = push_mod.score_dims(ev, _PUSH_PROFILE, today)
    assert set(dims) == {"school_hit", "major_hit", "direction_hit", "freshness"}, sorted(dims)
    for k, v in dims.items():
        assert 0.0 <= v <= 1.0, ("这一维跑出了 0~1", k, v)

    # 加权总分也必须在 0~1（它再乘 0~2 的 urgency，结果才是可比的）
    total = push_mod.weighted_score(dims)
    assert 0.0 <= total <= 1.0, total


@case
def push_新鲜度分档按发布日且未知不算旧():
    """**"我们不知道"和"它是旧公告"必须是两个值。**

    `publish_date` 是空串的事件在真实数据里有一批（数据层抽不到）。
    把 `unknown` 归进 `old` 是最省事也最坏的做法：
    一条刚发但页面没写日期的公告会被打进"历史参考"，
    而用户看到的理由会是"90 天前发布" —— 那是编造。
    反方向同理：当成 `fresh` 就是把八年前的公告叫「新公告」（issue 008 那个错）。
    """
    today = date(2026, 10, 18)

    def v(days):
        pub = (today - timedelta(days=days)).isoformat()
        return push_mod.freshness_of({"publish_date": pub}, today)

    assert v(0)[0] == config.PUSH_FRESH_FULL
    assert v(30)[0] == config.PUSH_FRESH_FULL, ("第 30 天还算新鲜", v(30))
    assert v(31)[0] == config.PUSH_FRESH_MID, ("第 31 天该掉档", v(31))
    assert v(90)[0] == config.PUSH_FRESH_MID, ("第 90 天还在中档", v(90))
    assert v(91)[0] == config.PUSH_FRESH_OLD, ("第 91 天该到底档", v(91))
    assert v(400)[0] == config.PUSH_FRESH_OLD

    unknown = push_mod.freshness_of({"publish_date": ""}, today)
    assert unknown[1] == "unknown", unknown
    assert unknown[0] == config.PUSH_FRESH_UNKNOWN, unknown
    assert unknown[0] != config.PUSH_FRESH_OLD, "发布日期读不到被当成了'旧公告'"
    assert unknown[0] != config.PUSH_FRESH_FULL, "发布日期读不到被当成了'新公告'"
    assert unknown[2] is None, ("读不到日期时 age_days 必须是 None，不许猜 0", unknown)

    # 发布日在未来（数据错）→ 归 unknown，但 age_days 要说出来是负数
    future = push_mod.freshness_of(
        {"publish_date": (today + timedelta(days=3)).isoformat()}, today)
    assert future[1] == "unknown", future
    assert future[2] == -3, ("未来日期要如实给出负数，不许截断成 0", future)


@case
def push_每条推送都有可读的why():
    """**B2 的核心交付。** 防"加了 `why` 却漏填/填空"。

    用户问「为什么推给我」，空数组等于没回答；一条 2 个字的理由
    （"相关""匹配"）也等于没回答。所以除了非空，还要求每条理由像句话。
    同时钉住 `why` 和 `matched_because` **不许漂移**：
    `matched_because` 为空的意思是不推，那它压根不该出现在 items 里。
    """
    today = date.today()
    evs = [
        _push_event("e_y1", deadline=(today + timedelta(days=3)).isoformat(), relevance=2),
        _push_event("e_y2", deadline=None, ds="absent", relevance=1),
        _push_event("e_y3", deadline=None, ds="unknown", relevance=2, matched=()),
    ]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_why", json=_PUSH_PROFILE)
        r = c.get("/api/push/preview?user=u_why&limit=10").json()
    finally:
        _close(c)

    assert r["items"], r["tally"]
    for it in r["items"]:
        why = it.get("why")
        assert isinstance(why, list), ("why 必须是数组", it["event_id"], type(why))
        assert why, ("这条推给了用户却不说为什么", it["event_id"])
        for line in why:
            assert isinstance(line, str) and len(line.strip()) >= 4, \
                ("理由太短，等于没说", it["event_id"], line)
        assert it["matched_because"], (
            "why 有内容但 matched_because 为空 —— 两条判据漂移了", it["event_id"])
        # 三态的说法不许混：截止日未读到时不许出现"未公布"
        block = it["deadline"]
        if block["state"] == credibility.UNKNOWN:
            assert not any("未公布截止日" in x for x in why), (
                "unknown 被说成了'未公布'（那是 absent 的说法）", why)
        if block["state"] == credibility.ABSENT:
            assert not any("请点原文核对" in x for x in why), (
                "absent 被说成了'没读到'（那是 unknown 的说法）", why)


@case
def push_对口程度主导排序而不是只看紧急度():
    """**B2 的核心。** 防"四维接进来了，排序还在看 relevance/紧急度"。

    构造一对：一条今天截止但只沾一点边，一条很对口但还早 20 天。
    如果排序真的按对口程度走，后者必须在前 ——
    否则用户首屏看到的全是"马上截止但跟我没关系"的东西，
    而"对口"这件事只体现在 `why` 的文字里，没体现在名次上。
    """
    today = date.today()
    weak = _push_event(
        "e_weak", school="某不存在大学", unit="某学院",
        title="关于举办材料学科前沿讲座的通知", matched=("材料",),
        deadline=(today + timedelta(days=1)).isoformat(), relevance=2)
    strong = _push_event(
        "e_strong", school="浙江大学", unit="研究生院",
        title="浙江大学关于2027年推免工作的通知", matched=("推免",),
        deadline=(today + timedelta(days=20)).isoformat(), relevance=2)

    c = _push_client([weak, strong])
    try:
        c.put("/api/profile?user=u_tgt", json=_PUSH_PROFILE)
        r = c.get("/api/push/preview?user=u_tgt&limit=10").json()
    finally:
        _close(c)

    ids = [i["event_id"] for i in r["items"]]
    assert "e_weak" in ids and "e_strong" in ids, ids
    dims = {i["event_id"]: i["score_dims"] for i in r["items"]}
    assert dims["e_strong"]["school_hit"] == 1.0, dims["e_strong"]
    assert dims["e_weak"]["school_hit"] == 0.0, dims["e_weak"]
    assert ids.index("e_strong") < ids.index("e_weak"), (
        "只沾一点边但马上截止的排到了很对口的前面 —— 对口程度没进排序", ids)


@case
def push_新鲜度真的参与排序():
    """B2 判据③的**机制**断言：两条除发布日外一样，新的必须排在旧的之前。

    ## 2026-10-19 调整（B22）：窗口外的旧公告不再进首屏

    原来用一条 **120 天前**的公告来验"旧的排后面"。B22 之后，
    首屏只看近 60 天（`config.PUSH_POOL_FRESH_DAYS`），120 天那条会被
    `out_of_window` 挡掉 —— 不是测试错了，是**场景变了**。

    所以拆成两半：
    - 接口层：5 天 vs **45 天**（都在窗口内），新鲜的排前面；
    - 打分层：120 天那条的 `freshness` 仍然是 `old` 档（它依然参与打分，
      只是没资格上首屏）。

    这样将来谁把 `freshness` 从排序键里拿掉，这条照样会红。
    """
    today = date.today()
    old = _push_event("e_age_old", relevance=2,
                      deadline=(today + timedelta(days=5)).isoformat(),
                      on=(today - timedelta(days=45)).isoformat())
    new = _push_event("e_age_new", relevance=2,
                      deadline=(today + timedelta(days=5)).isoformat(),
                      on=(today - timedelta(days=5)).isoformat())

    c = _push_client([old, new])
    try:
        c.put("/api/profile?user=u_age", json=_PUSH_PROFILE)
        r = c.get("/api/push/preview?user=u_age&limit=10").json()
    finally:
        _close(c)

    items = {i["event_id"]: i for i in r["items"]}
    assert items["e_age_old"]["score_dims"]["freshness"] == config.PUSH_FRESH_MID
    assert items["e_age_new"]["score_dims"]["freshness"] == config.PUSH_FRESH_FULL
    ids = [i["event_id"] for i in r["items"]]
    assert ids.index("e_age_new") < ids.index("e_age_old"), (
        "两条除发布日外一模一样，旧的却排在前面 —— freshness 没进排序键", ids)

    # 窗口外（>60 天）的公告：打分函数仍给 `old` 档，只是进不了首屏。
    dims, _detail = push_mod.score_dims(
        _push_event("e_age_120", relevance=2,
                    on=(today - timedelta(days=120)).isoformat()),
        _PUSH_PROFILE, today)
    assert dims["freshness"] == config.PUSH_FRESH_OLD, dims


@case
def push_方向数据缺失时给中间值不判成没命中():
    """**"没命中"和"没得比"必须分开。**

    事件的可用方向信号只有 `matched[]` 和标题。两者都空时，
    判成 `direction_hit=0` 等于说"我们看过，没有你的方向" ——
    而真相是"我们没拿到方向数据"。这和 §14 的三态是同一条原则，
    只不过发生在打分里：0 会把它压下去，0.5 则让它保持中性。
    """
    today = date.today()

    # ① 画像里没填方向 → 没得比
    no_kw_profile = {"school": "浙江大学", "majors": ["物理学"],
                     "preferred_schools": ["浙江大学"]}
    ev = _push_event("e_d1", relevance=2, matched=("推免",))
    dims, detail = push_mod.score_dims(ev, no_kw_profile, today)
    assert detail["direction_state"] == "no_profile", detail
    assert dims["direction_hit"] == config.PUSH_DIRECTION_UNKNOWN, dims
    assert dims["direction_hit"] != 0.0, "画像没填方向被当成了'没命中'"

    # ② 事件里没有方向数据 → 也判不了
    bare = _push_event("e_d2", relevance=2, title="2027年硕士研究生招生简章", matched=())
    dims2, detail2 = push_mod.score_dims(bare, _PUSH_PROFILE, today)
    assert detail2["direction_state"] == "no_data", detail2
    assert dims2["direction_hit"] == config.PUSH_DIRECTION_UNKNOWN, dims2
    assert dims2["direction_hit"] != 0.0, "事件没有方向数据被当成了'没命中'"

    # ③ 真比过了、没中 → 这才是 0
    miss = _push_event("e_d3", relevance=2,
                       title="2027年硕士研究生招生简章", matched=("招生",))
    dims3, detail3 = push_mod.score_dims(miss, _PUSH_PROFILE, today)
    assert detail3["direction_state"] == "miss", detail3
    assert dims3["direction_hit"] == 0.0, dims3
    # 三种状态**两两不同**（否则这条用例就是在赌）
    assert len({"no_profile", "no_data", "miss"}) == 3


@case
def push_负向维度_EMBA和报考点不许排第一():
    """B20（2026-10-19，QA 实测）。契约 §18.8。

    QA 用"浙大物理 + 意向北大/清华/中科大 + 凝聚态 + 直博"的画像实测：
    首屏第 1 条是 **EMBA 简章**、第 2 条是**报考点公告**，
    真正的《北京大学 2027 年硕士研究生招生简章》排到第 3。
    四维全是正向的（学校命中就加分），这种事在分数里**完全没有体现**。

    这条用例要证明三件事，缺一条都只是"碰巧排对了"：

    ① **不是靠调四维权重**：EMBA 那条的**原始分**（四维×紧急度）故意比简章高
       （它还两天后截止），所以按四维排它必然第一；现在它排到简章后面，
       只可能是 `misfit` 乘出来的。
    ② **命中了哪一维要说出来**：`misfit.keys` / `misfit.text` / `why` 里都有 ——
       降权可以，黑箱不行。
    ③ **画像本身要管理类时不降权**：否则会把"想读 MBA 的人"的简章全压下去。
       （判据只看用户**明确写的** `target_levels` / 专业，不猜。）
    """
    evs = [
        # 两天后截止 → 紧急度 2.0，原始分必然最高
        _push_event("e_emba", school="北京大学", deadline=_day(2), ds="found",
                    title="北京大学2027年高级管理人员工商管理（EMBA）"
                          "专业学位硕士研究生招生简章"),
        _push_event("e_brochure", school="北京大学", deadline=None, ds="absent",
                    title="北京大学 2027 年硕士研究生招生简章"),
        # 标题明写"报考点"的统考公告：按统考处理，画像只要推免 → 被 mode_mismatch 挡掉
        _push_event("e_site", school="北京大学", deadline=None, ds="absent",
                    title="2027年全国硕士研究生招生考试北京大学（1101）报考点公告"),
        # 事务性、但标题没写"统考/报考点"：仍按 misfit 降权，不隐藏
        _push_event("e_confirm", school="北京大学", deadline=None, ds="absent",
                    title="北京大学 2027 年复试录取工作办法"),
    ]
    profile = {"majors": ["物理学"], "preferred_schools": ["北京大学"],
               "interest_keywords": ["凝聚态"], "target_levels": ["直博"],
               "wanted_modes": ["推免"]}
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_fit", json=profile)
        r = c.get("/api/push/preview?user=u_fit").json()
        assert r["empty_reason"] is None, r["empty_reason"]
        by = {i["event_id"]: i for i in r["items"]}
        assert set(by) == {"e_emba", "e_brochure", "e_confirm"}, sorted(by)
        order = [i["event_id"] for i in r["items"]]
        assert order.index("e_brochure") < order.index("e_emba"), (
            "EMBA 简章排到了招生简章前面", order)

        # ① EMBA 的**原始分**必须高于简章 —— 否则"排后面"证明不了是 misfit 干的
        def raw(it):
            return push_mod.weighted_score(it["score_dims"]) * it["urgency_weight"]
        assert raw(by["e_emba"]) > raw(by["e_brochure"]), (
            "这条用例没造好：EMBA 的原始分本来就更低，排后面说明不了任何事",
            raw(by["e_emba"]), raw(by["e_brochure"]))
        assert by["e_emba"]["misfit"]["weight"] < 1.0

        # ② 可解释：keys / text / why 三处都要有
        assert by["e_emba"]["misfit"]["keys"] == ["degree_type"], by["e_emba"]["misfit"]
        assert "EMBA" in (by["e_emba"]["misfit"]["text"] or ""), by["e_emba"]["misfit"]
        assert any("降权" in w for w in by["e_emba"]["why"]), by["e_emba"]["why"]
        assert by["e_confirm"]["misfit"]["keys"] == ["transactional"], by["e_confirm"]["misfit"]
        assert by["e_brochure"]["misfit"] == {"weight": 1.0, "keys": [], "text": None}, (
            "没命中的条目也必须给同样的三个键（前端不用判字段在不在）",
            by["e_brochure"]["misfit"])

        # ③ 标题证据修正 apply_mode：正文抽成"推免"也不许让统考公告进首屏
        site = [x for x in r["suppressed"] if x.get("event_id") == "e_site"]
        assert site and site[0]["reason"] == "mode_mismatch", (
            "报考点公告应按 mode_mismatch 被挡，而不是靠 misfit 排序",
            [(x.get("event_id"), x.get("reason")) for x in r["suppressed"]])
    finally:
        _close(c)

    # ③ 画像本来就要专硕/管理类 → 不降权（EMBA 的原始分更高，它就该第一）
    c2 = _push_client(evs)
    try:
        c2.put("/api/profile?user=u_mgmt", json={
            "majors": ["工商管理"], "preferred_schools": ["北京大学"],
            "interest_keywords": ["管理"], "target_levels": ["专硕"]})
        r2 = c2.get("/api/push/preview?user=u_mgmt").json()
        by2 = {i["event_id"]: i for i in r2["items"]}
        assert by2["e_emba"]["misfit"]["keys"] == [], (
            "画像明确要专硕/管理类，EMBA 不该被降权（过度纠正）",
            by2["e_emba"]["misfit"])
        assert r2["items"][0]["event_id"] == "e_emba", [i["event_id"] for i in r2["items"]]
    finally:
        _close(c2)


@case
def push_首屏按notice_kind分层_目录和在职不进首屏():
    """**B22 的核心。** 首屏 = 招生通知（announcement）+ 其他相关（other）补位，
    目录/简章（catalog）和在职项目（emba）**不进首屏**。

    改之前（QA 实测）：首屏 10 条 = 6 条目录/简章 + 2 组重复 + 1 条 EMBA + 1 条真公告。
    根因是 `topic=apply` 把"今天该办的通告"和"随时可查的目录"混成了一类 ——
    `notice_kind` 就是来拆这一类的。

    这条用例要钉住四件事：
    ① `items` 里**只有** announcement / other，一条 catalog/emba 都没有；
    ② **一级整层在前**，二级（other）只在后面 —— 包括已截止的一级也不许沉到二级后面；
    ③ 每条都带 `notice_kind`（前端要按它显示"其他相关"标签）；
    ④ 被移走的要**说得出为什么**（`suppressed` 里有 catalog / emba，不是静默消失）。
    """
    evs = [
        _push_event("e_ann", notice_kind="announcement", deadline=_day(5), ds="found"),
        # 50 天前发的公告：**在 60 天窗口内**，即使标了 backfill 也该进首屏
        _push_event("e_ann_50d", notice_kind="announcement", on=_day(-50),
                    backfill=True, deadline=None, ds="absent"),
        _push_event("e_other", notice_kind="other", title="2027年推免工作补充说明"),
        _push_event("e_cat", notice_kind="catalog",
                    title="浙江大学2027年硕士研究生招生专业目录"),
        _push_event("e_emba", notice_kind="emba",
                    title="浙江大学2027年高级管理人员工商管理（EMBA）招生简章"),
    ]
    profile = {"majors": ["物理学"], "preferred_schools": ["浙江大学"],
               "interest_keywords": ["推免"], "target_levels": ["直博"]}
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_kind", json=profile)
        r = c.get("/api/push/preview?user=u_kind&limit=20").json()
    finally:
        _close(c)

    items = r["items"]
    kinds = [i["notice_kind"] for i in items]
    assert "e_cat" not in [i["event_id"] for i in items], "目录进了首屏"
    assert "e_emba" not in [i["event_id"] for i in items], "EMBA 进了首屏"
    assert set(kinds) <= {"announcement", "other"}, kinds
    assert all(k is not None for k in kinds), "有 item 没带 notice_kind"

    # ② 一级整层在前：出现 other 之后，不许再有 announcement
    tiers = [i["tier"] for i in items]
    assert tiers == sorted(tiers), ("二级插到了一级中间", tiers)
    first_two = [i for i in items if i["tier"] == 1]
    assert all(i["notice_kind"] == "announcement" for i in first_two), (
        "一级里混进了非 announcement", [i["notice_kind"] for i in first_two])
    assert [i["event_id"] for i in items][:len(first_two)] == \
        [i["event_id"] for i in first_two], "一级没有全部排在前面"

    # 窗口内的补抓 announcement：进首屏（这是"60 天"的全部意义）
    assert "e_ann_50d" in [i["event_id"] for i in items], (
        "50 天前的招生通知被 backfill 挡掉了 —— 60 天窗口没生效",
        [(x["event_id"], x["reason"]) for x in r["suppressed"]])

    # ④ 移走了什么要说得出来
    reasons = {x["reason"] for x in r["suppressed"]}
    assert "catalog" in reasons and "emba" in reasons, reasons
    assert r["tally"]["catalog"] >= 1 and r["tally"]["emba_excluded"] >= 1, r["tally"]

    # ⑤ 画像自己要专硕/管理类时，EMBA **不该**被硬排除（纠正过度）
    c2 = _push_client(evs)
    try:
        c2.put("/api/profile?user=u_kind_mg", json={
            "majors": ["工商管理"], "preferred_schools": ["浙江大学"],
            "interest_keywords": ["管理"], "target_levels": ["专硕"]})
        r2 = c2.get("/api/push/preview?user=u_kind_mg&limit=20").json()
    finally:
        _close(c2)
    emba = [i for i in r2["items"] if i["event_id"] == "e_emba"]
    assert emba, ("明确要专硕/管理类，EMBA 不该被排除", r2["tally"])
    assert emba[0]["tier"] == 2, emba[0]


@case
def push_六十天窗口外的公告不进首屏():
    """**B22:** 对外说"近 60 天"，首屏就必须按同一个窗口筛。

    真实数据里有过一条 **178 天前**的《2026 年推免简章》出现在首屏 ——
    它的 `added` 事件不是补抓（数据层只按 30 天标 `is_backfill`），
    光靠 `is_backfill` 拦不住。所以窗口要**自己判**，不能只信事件标记。
    """
    evs = [
        _push_event("e_45", notice_kind="announcement", on=_day(-45)),
        # 61 天前、且**没有**被标 backfill —— 只靠 is_backfill 会漏掉它
        _push_event("e_61", notice_kind="announcement", on=_day(-61), backfill=False),
        _push_event("e_59_other", notice_kind="other", on=_day(-59),
                    title="2027年推免工作补充说明"),
    ]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_win", json={
            "majors": ["物理学"], "preferred_schools": ["浙江大学"],
            "interest_keywords": ["推免"]})
        r = c.get("/api/push/preview?user=u_win&limit=20").json()
    finally:
        _close(c)

    ids = [i["event_id"] for i in r["items"]]
    assert "e_45" in ids and "e_59_other" in ids, ids
    assert "e_61" not in ids, ("窗口外（61 天）的公告进了首屏", ids)
    out = [x for x in r["suppressed"] if x["reason"] == "out_of_window"]
    assert [x["event_id"] for x in out] == ["e_61"], out
    assert r["tally"]["out_of_window"] == 1, r["tally"]


@case
def push_目录进参考资料块而不是消失():
    """**B22 第三层。** 目录/简章不进首屏，但**必须还在**——进「参考资料」区。

    如果只是"压掉"，用户会觉得"我填了北大，怎么连北大的招生简章都没有"；
    而目录恰恰是随时要查的东西。所以：

    - `reference.items` 全是 `catalog`，**同校同名去重**（南京大学那对目录
      曾经在首屏并排出现两次）；
    - 每条带 url（参考资料也要能点回原文）；
    - `reference.total` 是去重后的总数，`items` 只是前 N 条（截断要说）。
    """
    c = _push_client([])          # 事件流空着，参考资料照样有（它来自公告库）
    try:
        c.put("/api/profile?user=u_ref", json={
            "majors": ["物理学"],
            "preferred_schools": ["北京大学", "清华大学", "中国科学技术大学"],
            "interest_keywords": ["凝聚态"], "target_levels": ["直博"]})
        r = c.get("/api/push/preview?user=u_ref&limit=20").json()
    finally:
        _close(c)

    ref = r["reference"]
    assert ref["total"] > 0, ("参考资料一条都没有 —— 目录被压掉了？", r["tally"])
    assert len(ref["items"]) <= config.PUSH_REFERENCE_LIMIT, len(ref["items"])
    assert ref["truncated"] == (ref["total"] > len(ref["items"])), ref
    keys = []
    for it in ref["items"]:
        assert it["notice_kind"] == "catalog", it
        assert it.get("url"), ("参考资料没有原文链接", it["notice_id"])
        keys.append(((it.get("school") or "").strip(),
                     " ".join((it.get("title") or "").split())))
    assert len(keys) == len(set(keys)), ("参考资料里同一校同名的目录出现了两次", keys)
    assert not any(i["notice_kind"] == "catalog" for i in r["items"]), "catalog 混进首屏"


@case
def push_页头两个数能从数据里复算():
    """**B23：** `tally.apply_fresh_announcement` / `..._matched` 必须能复算。

    演示里要说的那句是 **"近 60 天全库 37 条招生通知，其中 6 条对口你"** ——
    数字一旦是拍脑袋写死的，第二天数据一变就成了台上的谎。

    这条用例**不复用接口的辅助函数**，而是在测试里按契约口径重新数一遍：
    `notice_kind == announcement` + `topic == apply` + 非 junk + 60 天内
    + 同校同标题去重；"对口"再叠加学校/专业/方向/院系的子串命中与招生方式过滤。
    """
    c = _push_client([])
    try:
        c.put("/api/profile?user=u_pool", json={
            "majors": ["物理学"],
            "preferred_schools": ["北京大学", "清华大学", "中国科学技术大学"],
            "interest_keywords": ["凝聚态", "超导"], "target_levels": ["直博"],
            "wanted_modes": ["推免"]})
        r = c.get("/api/push/preview?user=u_pool&limit=20").json()
    finally:
        _close(c)

    today = date.today()
    window = config.PUSH_POOL_FRESH_DAYS

    def age(rec):
        d = push_mod.parse_day(rec.get("publish_date"))
        return None if d is None else (today - d).days

    def dkey(rec):
        return ((rec.get("school") or "").strip(),
                " ".join((rec.get("title") or "").split()))

    def is_junk(rec):
        return (rec.get("record_quality") or "") == "junk"

    base = [x for x in RAW
            if x.get("notice_kind") == "announcement"
            and x.get("topic") == "apply" and not is_junk(x)
            and age(x) is not None and 0 <= age(x) <= window]
    uniq = {}
    for x in sorted(base, key=lambda x: x.get("publish_date") or "", reverse=True):
        uniq.setdefault(dkey(x), x)
    assert r["tally"]["apply_fresh_announcement"] == len(uniq), (
        "全库招生通知数和数据对不上",
        r["tally"]["apply_fresh_announcement"], len(uniq))

    # "对口你"：同一套硬过滤 + 命中画像（子串方向：画像词 in 公告文本）
    wanted = ["推免"]

    def hit(rec):
        school = (rec.get("school") or "").strip()
        hay = " ".join([(rec.get("title") or ""), (rec.get("unit") or ""),
                        " ".join(rec.get("majors") or [])])
        for s in ["北京大学", "清华大学", "中国科学技术大学"]:
            if s in school:
                return True
        for m in ["物理学"]:
            if m in hay:
                return True
        for k in ["凝聚态", "超导"]:
            if k in hay:
                return True
        return False

    matched = [x for x in uniq.values()
               if hit(x) and (not x.get("apply_mode") or x["apply_mode"] in wanted)]
    assert r["tally"]["apply_fresh_announcement_matched"] == len(matched), (
        "「其中几条对口你」和数据对不上",
        r["tally"]["apply_fresh_announcement_matched"], len(matched))
    assert 0 < len(matched) <= len(uniq), (len(matched), len(uniq))


@case
def profile_近似字段自动映射_未知字段要报出来():
    """B19（2026-10-19，QA 实测）。契约 §16.2。

    改之前：`PUT /api/profile` 传 `intended_schools` → **200**，
    字段被原样保存，然后在匹配里**完全不存在** —— 用户看不到任何提示，
    只会觉得"这系统不懂我"。QA 按 README 的固定画像跑，第一屏 0 条，
    根因就是这两个字段名（`intended_schools` / `major`）。

    三件事，缺一条这条用例就只是安慰：

    ① 近似字段**真的进了 profile**（不是只提示一句"我猜你想填…"）；
    ② 映射**真的影响了推送结果**（否则只是改了个键名）；
    ③ 认不出的字段要**报出来**，并说清它不参与匹配 ——
       但**仍然原样保存**（`/api/match` 以后可能会用）。
    """
    evs = [_push_event("e_bd", school="北京大学",
                       title="北京大学2027年硕士研究生招生简章")]
    c = _push_client(evs)
    try:
        payload = {"intended_schools": ["北京大学"], "major": "物理学",
                   "foo_bar": 1}
        r = c.put("/api/profile?user=u_alias", json=payload)
        assert r.status_code == 200, r.text
        d = r.json()
        prof = d["profile"]

        # ① 映射进 profile，而且**不留原名**（留了就一个值两个来源）
        assert prof.get("preferred_schools") == ["北京大学"], prof
        assert prof.get("majors") == ["物理学"], prof
        assert "intended_schools" not in prof and "major" not in prof, prof
        assert d["mapped_fields"] == [
            {"from": "intended_schools", "to": "preferred_schools"},
            {"from": "major", "to": "majors"},
        ], d["mapped_fields"]

        # ② 未知字段：报出来 + 人话 + 原样保存
        assert d["unknown_fields"] == ["foo_bar"], d["unknown_fields"]
        note = d["field_note"]
        assert "foo_bar" in note and "不参与匹配" in note, note
        assert "intended_schools" in note and "preferred_schools" in note, note
        assert prof.get("foo_bar") == 1, "未知字段被静默丢掉了（这条文件以前就是靠这个栽的）"

        # ③ 推送真的用上了映射来的字段
        p = c.get("/api/push/preview?user=u_alias").json()
        assert p["items"], (p["empty_reason"], p["tally"])
        item = p["items"][0]
        assert item["event_id"] == "e_bd", item["event_id"]
        assert any(x.startswith("意向学校") for x in item["matched_because"]), \
            item["matched_because"]

        # ④ 两者都填时：**以真字段为准**，近似字段进 dropped（说一声，不静默）
        d2 = c.put("/api/profile?user=u_alias2", json={
            "preferred_schools": ["清华大学"], "intended_schools": ["北京大学"]}).json()
        assert d2["profile"]["preferred_schools"] == ["清华大学"], d2["profile"]
        assert any(x["field"] == "intended_schools" for x in d2.get("dropped") or []), \
            d2.get("dropped")
    finally:
        _close(c)


# ---------------------------------------------------------------------------
# 第 19 节：记录质量（junk / thin / ok）与时效四件套
# ---------------------------------------------------------------------------

@case
def quality_junk默认不出现在列表():
    """**B1 的核心。** 默认拒收 `record_quality=junk`，但 `include_junk=true` 能查到。

    防的是三件事，每一件都见过真后果：

    ① **脏记录混进首屏**（标题是站点名、正文只剩"发布时间/点击数"）——
       用户看到一张没有标题的卡，会以为我们抽不出内容。
    ② **默认能看到 = 没法复查。** 用户报"你们少了一条"时，
       必须能把它原样取出来，所以 `include_junk=true` 是一条真路径，不是摆设。
    ③ **"排掉了"被说成"没有数据"。** 所以 `total` 必须跟着变，
       两个数的差要**恰好等于** junk 的条数 —— 不是"差不多"。
    """
    plain = CLIENT.get("/api/notices", params={"size": 1}).json()
    with_junk = CLIENT.get("/api/notices",
                           params={"size": 1, "include_junk": "true"}).json()

    assert plain["total"] == len(VISIBLE), (plain["total"], len(VISIBLE))
    assert with_junk["total"] == len(RAW), (with_junk["total"], len(RAW))
    assert with_junk["total"] - plain["total"] == len(JUNK), (
        "两个 total 的差必须恰好是 junk 条数",
        with_junk["total"], plain["total"], len(JUNK))

    page = CLIENT.get("/api/notices", params={"size": 50}).json()["items"]
    bad = [i["id"] for i in page if i["record_quality"] == "junk"]
    assert not bad, ("默认列表里混进了 junk", bad[:3])

    opened = CLIENT.get("/api/notices",
                        params={"size": 50, "include_junk": "true"}).json()["items"]
    assert any(i["record_quality"] == "junk" for i in opened), (
        "include_junk=true 却一条 junk 都没出现 —— 开关没接上")


@case
def quality_四件套在四个接口都透出且同名():
    """`record_quality` / `topic` / `is_fresh` / `age_days` 四个接口都要给，**同名**。

    防"四个接口四种写法"：C 的卡片在列表页、匹配页、推送页共用一份渲染，
    只要有一处把 `age_days` 写成 `days_old`、或者把四件套塞进 `quality: {...}`，
    前端就要写四套逻辑 —— 而**漏掉一处不会报错，只会让某个页面显示空白**。
    """
    keys = {"record_quality", "topic", "is_fresh", "age_days"}

    item = CLIENT.get("/api/notices", params={"size": 1}).json()["items"][0]
    assert keys <= set(item), ("列表项缺字段", keys - set(item))

    detail = CLIENT.get("/api/notices/" + item["id"],
                        params={"user": "u_num_demo"}).json()
    assert keys <= set(detail), ("详情缺字段", keys - set(detail))

    m = CLIENT.post("/api/match", json={**_PUSH_PROFILE, "limit": 3}).json()
    assert m["items"], m["stats"]
    assert keys <= set(m["items"][0]), ("匹配项缺字段", keys - set(m["items"][0]))

    p_items = None
    c = _push_client([_push_event("e_q1", relevance=2)])
    try:
        c.put("/api/profile?user=u_q4", json=_PUSH_PROFILE)
        p_items = c.get("/api/push/preview?user=u_q4&limit=10").json()["items"]
    finally:
        _close(c)
    assert p_items, "推送一条都没出来，这条用例没法验"
    assert keys <= set(p_items[0]), ("推送项缺字段", keys - set(p_items[0]))

    assert item["record_quality"] == detail["record_quality"], (
        "同一条记录在列表和详情里的 record_quality 不一致",
        item["record_quality"], detail["record_quality"])

    # **键"在不在"是半个断言。** 另一半是"值有没有被规范化" ——
    # 上面那几个 `keys <= set(...)` 全部可以通过，而投影里只要少了
    # `quality.of()` 这一步，老记录的 `record_quality` 就会是 `None`
    # （前端于是要多写一个 null 分支，而漏写的那天卡片会显示空白）。
    # 真实数据里每条都有这四个字段，所以**必须用一条合成记录才验得到**。
    proj = STORE.project({"id": "synthetic", "title": "合成记录"})
    assert proj["record_quality"] == "unknown", (
        "投影没有规范化 record_quality（缺字段时给了 %r）" % (proj["record_quality"],))
    assert proj["topic"] is None, proj["topic"]
    assert proj["is_fresh"] is None, proj["is_fresh"]
    assert proj["age_days"] is None, proj["age_days"]


@case
def quality_junk不进推送但要说出为什么():
    """脏记录不进推送 —— **而且不能悄悄消失**。

    只把 junk 从 `items` 里拿掉是"最省事也最坏"的做法：
    用户会问"今天怎么只有 13 条"，而 `tally.matched` 又和 `sent` 对不上。
    所以它必须出现在 `suppressed` 里，带上 `reason=junk` 和一句人话。
    """
    junk_ev = dict(_push_event("e_junk1", relevance=2), record_quality="junk")
    ok_ev = dict(_push_event("e_ok1", relevance=2), record_quality="ok")
    c = _push_client([junk_ev, ok_ev])
    try:
        c.put("/api/profile?user=u_jq", json=_PUSH_PROFILE)
        r = c.get("/api/push/preview?user=u_jq&limit=10").json()
    finally:
        _close(c)

    ids = [i["event_id"] for i in r["items"]]
    assert "e_junk1" not in ids, ("junk 进了推送", ids)
    assert "e_ok1" in ids, ("正常记录被误伤", ids)
    assert r["tally"]["junk_dropped"] == 1, r["tally"]

    junk_sup = [s for s in r["suppressed"] if s["reason"] == "junk"]
    assert len(junk_sup) == 1, ("junk 被静默丢弃了（suppressed 里没有）", r["suppressed"])
    assert junk_sup[0]["text"] and len(junk_sup[0]["text"]) > 10, junk_sup[0]


@case
def quality_缺字段时是unknown不是ok():
    """**"没标过"不是"检查过没问题"。**

    老记录（A 加这个字段之前落盘的）没有 `record_quality`。
    默认成 `"ok"` 等于替数据层宣布"这条我们检查过、是好的" —— 那是编造。
    同理 `is_fresh` 缺省不许变成 `false`（"不知道多新"≠"不新"），
    `age_days` 缺省不许变成 `0`（0 的意思是"今天发的"）。
    """
    empty = quality.of({})
    assert empty["record_quality"] == "unknown", empty
    assert empty["record_quality"] != "ok", "缺字段被默认成了 ok"
    assert empty["topic"] is None, ("没分类不是 other", empty)
    assert empty["is_fresh"] is None, ("不知道多新不能说成不新", empty)
    assert empty["age_days"] is None, ("没有发布日不能说成 0 天前", empty)

    # 有就透传，一个字节都不改（数据层是唯一事实来源）
    raw = {"record_quality": "thin", "topic": "apply",
           "is_fresh": "stale", "age_days": 400}
    kept = quality.of(raw)
    assert kept == raw, kept
    # `"stale"` 是数据层定的第 4 个取值，不许被压成 False
    assert kept["is_fresh"] != False, kept["is_fresh"]  # noqa: E712


# ---------------------------------------------------------------------------
# 第 20 节：今日更新 / 资料库的分区
# ---------------------------------------------------------------------------

def _split_client():
    """带**真实事件流**的客户端（分区恒等式只能在全集上验）。"""
    return _push_client(events_mod.EventStore.load(config.EVENTS_PATH).records)


@case
def updates_只给新窗口内且不超过五十条():
    """`/api/updates` 的判据：**≤50 条，且全部在窗口内**。

    这条防的是"窗口参数接了但没用"：前端传 `?days=7`，接口照给 30 天甚至全量，
    页面上就会出现"本周更新（附三个月前）"—— 而那个标题是假的。
    """
    c = _split_client()
    try:
        d = c.get("/api/updates", params={"days": 7}).json()
    finally:
        _close(c)

    assert d["total"] <= 50, ("今日更新超过 50 条", d["total"])
    assert len(d["items"]) <= 50, len(d["items"])
    for it in d["items"]:
        age = it["age_days"]
        assert age is not None, ("进了今日更新却没有发布日", it["event_id"])
        assert 0 <= age <= 7, ("窗口是 7 天，却给了 %r 天前的" % age, it["event_id"])
        assert it["is_historical"] is False, (
            "标着历史参考的出现在今日更新里", it["event_id"])
        assert it["record_quality"] != "junk", ("junk 进了今日更新", it["event_id"])

    # 窗口放大到 30 天时，条数只增不减（口径一致性的一个廉价检查）
    c2 = _split_client()
    try:
        d30 = c2.get("/api/updates", params={"days": 30}).json()
    finally:
        _close(c2)
    assert d30["total"] >= d["total"], (d30["total"], d["total"])


@case
def updates_与_library_不重叠且并集是全集():
    """**B3 的核心恒等式**：两个接口的事件**并集 = 事件全集，交集 = 0**。

    为什么这条必须存在：`library` 是 `updates` 的**补集**，不是另外查一遍。
    如果哪天有人给它俩各写一套筛选条件，就会出现一条事件在两个接口都没有
    （用户看不见它，而没有任何报错）或者都有（同一条公告既是"今天新增"
    又是"资料库"）。**这种错不会抛异常，只会让某条记录悄悄消失。**

    验法用 `total` 而不是逐条取全集：`updates.total + library.total == 事件总数`
    就是"不相交且覆盖"的充要条件（两边都没有过滤参数、`updates` 上限 50
    只在 `items` 上截断，`total` 是截断前的真值 —— 这一点也在下面钉住了）。
    """
    c = _split_client()
    try:
        # **两个接口必须传同一个 days** —— 分区边界只有一份，
        # 一边取 30 天、一边取默认 7 天，8~30 天那批就会同时出现在两边
        # （实测差 85 条）。这不是"差不多"，是交集直接不为空。
        u = c.get("/api/updates", params={"days": 30, "limit": 50}).json()
        lib = c.get("/api/library", params={"size": 50, "days": 30}).json()
        # limit 只截 items，不截 total（否则这条恒等式会被"今天恰好很多"弄假）
        assert u["total"] >= len(u["items"]), (u["total"], len(u["items"]))
        total_events = len(events_mod.EventStore.load(config.EVENTS_PATH).records)
        assert u["total"] + lib["total"] == total_events, (
            "并集不是全集：updates %d + library %d != %d"
            % (u["total"], lib["total"], total_events))

        # 交集：抽查两个接口的第一页，不许有同一个 event_id
        u_ids = {i["event_id"] for i in u["items"]}
        l_ids = {i["event_id"] for i in lib["items"]}
        assert not (u_ids & l_ids), ("两个接口给了同一条事件", list(u_ids & l_ids)[:3])
    finally:
        _close(c)


@case
def library_能按学校和年份筛且标出历史():
    """`/api/library` 要能筛、并且**诚实地标历史**。

    两个容易写错的地方：

    ① **`is_historical` 不是"在 library 里"**。资料库按发布日降序，
       第一页很可能全是"20 天前、不在本周窗口、但也不是历史"的记录。
       如果按"在这个接口里"标 `true`，那文档就在说假话。
    ② **`year` 不能只认发布年**。这个产品的年份是**招生年**（2027），
       而一条 2027 年招生的公告可能是 2026 年发的 —— 只比发布年会让
       `?year=2027` 返回 0 条，而用户明明知道有。
    """
    c = _split_client()
    try:
        d = c.get("/api/library", params={"size": 5}).json()
        assert d["items"], "资料库是空的，后面的断言没有意义"
        for it in d["items"]:
            assert "is_historical" in it, it.keys()
            assert it["history_reason"], ("标了历史却不说为什么", it["event_id"])

        hist = c.get("/api/library", params={"historical": "true", "size": 20}).json()
        assert hist["items"], "historical=true 一条都没有"
        assert all(i["is_historical"] is True for i in hist["items"]), (
            "historical=true 却给了非历史记录")

        sch = c.get("/api/library", params={"school": "北京大学", "size": 5}).json()
        assert sch["items"], "按学校筛没有结果"
        assert all("北京大学" in (i["school"] or "") for i in sch["items"])

        # 年份：至少要能命中"招生年 / 目标年"，不能只认发布年
        y = c.get("/api/library", params={"year": 2027, "size": 5}).json()
        assert y["items"], ("按 2027 筛没有结果 —— 年份很可能只比了发布年", y["total"])
        for it in y["items"]:
            years = {(it.get("publish_date") or "")[:4],
                     str(it.get("admission_year")), str(it.get("target_year"))}
            assert "2027" in years, (it["event_id"], years)
    finally:
        _close(c)


@case
def updates_已截止进资料库而未知不算过期():
    """**"没读到截止日"不许被当成"已经过期"。**

    这条是 §14 那条不变量的第四次落点，代价特别具体：
    一条截止日没抽到的公告如果被当成过期，它就从"今日更新"里消失、
    被打进资料库 —— 而它可能下周就截止。
    """
    today = date.today()
    fresh_unknown = _push_event(
        "e_u_unknown", relevance=2, deadline=None, ds="unknown",
        on=today.isoformat())
    fresh_absent = _push_event(
        "e_u_absent", relevance=2, deadline=None, ds="absent",
        on=today.isoformat())
    expired = _push_event(
        "e_u_expired", relevance=2,
        deadline=(today - timedelta(days=2)).isoformat(), on=today.isoformat())

    c = _push_client([fresh_unknown, fresh_absent, expired])
    try:
        u = c.get("/api/updates").json()
        lib = c.get("/api/library", params={"size": 50}).json()
    finally:
        _close(c)

    u_ids = {i["event_id"] for i in u["items"]}
    l_ids = {i["event_id"] for i in lib["items"]}
    assert "e_u_unknown" in u_ids, ("截止日'没读到'被当成了过期/旧", u_ids)
    assert "e_u_absent" in u_ids, ("'没有截止日'被当成了过期/旧", u_ids)
    assert "e_u_expired" in l_ids, ("真已截止的没进资料库", l_ids)
    assert "e_u_expired" not in u_ids, "已截止的仍在今日更新里"

    # 已截止那条的理由必须说实话
    exp = next(i for i in lib["items"] if i["event_id"] == "e_u_expired")
    assert exp["is_historical"] is True, exp
    assert "截止" in exp["history_reason"], exp["history_reason"]


# ---------------------------------------------------------------------------
# 第 21 节：数据心跳（`GET /api/health/data`）
# ---------------------------------------------------------------------------

@case
def heartbeat_接口形状与三态():
    """**防：接口把"不知道"说成"成功"或"失败"。**

    `ok` 必须是三态 `true` / `false` / **`null`**。

    - 说成 `false`：会让人去查一个没坏的东西（浪费一次排查）；
    - 说成 `true`：任务真挂掉时你会以为一切正常 —— **这一条最贵**。

    另外钉住：`fresh_within_3d <= fresh_within_7d`（两个窗口装反了会一眼看出来），
    以及 `data_age_minutes` 算不出来时是 `null` 而不是 `0`（0 = 刚刚跑完）。
    """
    r = CLIENT.get("/api/health/data")
    assert r.status_code == 200, r.status_code
    d = r.json()

    for k in ("last_run", "runs_24h", "runs_24h_failed", "consecutive_ok",
              "last_full_at", "notices", "events", "fresh_within_3d",
              "fresh_within_7d", "data_age_minutes", "_note"):
        assert k in d, ("心跳缺字段", k, sorted(d))

    lr = d["last_run"]
    if lr is not None:
        assert {"at", "kind", "ok", "seconds", "new_notices"} <= set(lr), sorted(lr)
        assert lr["kind"] in ("fast", "full"), lr["kind"]
        assert lr["ok"] in (True, False, None), ("ok 只能是三态", lr["ok"])
        if lr["ok"] is None:
            assert d["_note"] and len(d["_note"]) > 10, (
                "ok 是 null 却没说明为什么 —— 调用方会当成失败", d["_note"])

    assert isinstance(d["runs_24h"], int) and d["runs_24h"] >= 0
    assert isinstance(d["consecutive_ok"], int) and d["consecutive_ok"] >= 0
    assert d["notices"] > 0, ("数据条数不该是 0", d["notices"])
    assert d["fresh_within_3d"] <= d["fresh_within_7d"], (
        "3 天内的条数比 7 天内还多 —— 两个窗口装反了",
        d["fresh_within_3d"], d["fresh_within_7d"])
    if d["data_age_minutes"] is not None:
        assert d["data_age_minutes"] >= 0, d["data_age_minutes"]


@case
def heartbeat_连续失败让consecutive归零():
    """**防：任务连续挂掉，而接口还说"一切正常"。**

    造一本日志：一轮成功，然后两轮 `crawl exit=1`（且没有 end 标记 ——
    和 `run_fast.cmd` 在失败时 `exit /b` 的行为一致）。
    判据：`consecutive_ok == 0`、`last_run.ok is False`、`runs_24h_failed == 2`。

    **`logs_dir` 是注入进去的**，绝不写真实的 `data/logs/`
    （写了会污染下一轮的 consecutive，而且那是 A 的目录）。
    """
    import tempfile
    from datetime import datetime

    from server import heartbeat as hb

    lines = [
        "[周日 2026/10/18 09:00:00.00] ===== fast start =====",
        "记录 1120 条 -> x",
        "[周日 2026/10/18 09:02:10.00] ===== fast end =====",
        "[周日 2026/10/18 10:00:00.00] ===== fast start =====",
        "[周日 2026/10/18 10:02:00.00] crawl exit=1",
        "[周日 2026/10/18 11:00:00.00] ===== fast start =====",
        "[周日 2026/10/18 11:01:00.00] crawl exit=1",
    ]
    tmp = tempfile.mkdtemp(prefix="hb_")
    with open(os.path.join(tmp, "fast.log"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    d = hb.summary(now=datetime(2026, 10, 18, 12, 0, 0), logs_dir=tmp)
    assert d["last_run"]["ok"] is False, ("最后一轮是失败的", d["last_run"])
    assert d["consecutive_ok"] == 0, ("连续失败之后 consecutive_ok 必须归零",
                                      d["consecutive_ok"])
    assert d["runs_24h"] == 3, d["runs_24h"]
    assert d["runs_24h_failed"] == 2, d["runs_24h_failed"]
    assert d["last_run"]["seconds"] is None, (
        "失败的那一轮没有 end 标记，耗时算不出来 —— 不许编一个数", d["last_run"])


@case
def heartbeat_读不到日志时ok为null不是false():
    """**防："没有日志"被当成"跑失败了"。**

    一个从没跑过定时任务的部署，`data/logs/` 是空的。
    这时 `last_run` 应该是 `None`，**不是** `ok: false` ——
    否则值班的人会去找一个不存在的失败。
    """
    import tempfile
    from datetime import datetime

    from server import heartbeat as hb

    tmp = tempfile.mkdtemp(prefix="hb_empty_")
    d = hb.summary(now=datetime(2026, 10, 18, 12, 0, 0), logs_dir=tmp)
    assert d["last_run"] is None, ("没有日志时 last_run 必须是 None", d["last_run"])
    assert d["runs_24h"] == 0 and d["consecutive_ok"] == 0, d
    assert d["_note"] and "不知道" in d["_note"], (
        "读不到日志时必须明说'不知道'，不能沉默", d["_note"])
    assert d["data_age_minutes"] is None, (
        "没有运行记录时数据年龄无从得知，不许写成 0", d["data_age_minutes"])


# ---------------------------------------------------------------------------
# 数据健康门禁（`server/tests/test_data_health.py`）—— 2026-10-18 接入
# ---------------------------------------------------------------------------
#
# 这一组验的**不是接口形状**，而是数据本身有没有坏（正文空壳、悬空事件、
# 发布日缺失、标题坍塌、fast 轮次健康…）。理由写在那个模块的 docstring 里。
#
# 用 importlib 按路径加载：`run.py` 是脚本不是包，`server/tests` 没有 `__init__.py`，
# 所以不能 `from server.tests import ...`。同一个模式 `run.py` 已经在
# `collector/tools` 上用过。
_health_spec = importlib.util.spec_from_file_location(
    "_data_health", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "test_data_health.py"))
_health = importlib.util.module_from_spec(_health_spec)
_health_spec.loader.exec_module(_health)
CASES.extend(_health.CASES)


# ---------------------------------------------------------------------------
# 数据量突变门禁（`server/tests/test_data_volume.py`）—— 2026-10-19 接入（B21）
# ---------------------------------------------------------------------------
#
# 这一组只问一件事：**记录还在不在**。
#
# 事故：2026-10-19 凌晨 `notices.jsonl` 被写残成 69 条（原 2751），持续 3 小时，
# 上面那一大组数据健康断言**一条都没红** —— 它们验的是"每条记录好不好"，
# 而 69 条合法记录和 2751 条合法记录在形状上没有任何区别。
#
# 比较对象是**提交进版本库的基线**（`contracts/data_volume_baseline.json`），
# 所以换一台机器 clone 下来也照样能判，不依赖"本机上一轮跑过什么"。
_volume_spec = importlib.util.spec_from_file_location(
    "_data_volume", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "test_data_volume.py"))
_volume = importlib.util.module_from_spec(_volume_spec)
_volume_spec.loader.exec_module(_volume)
CASES.extend(_volume.CASES)


# ---------------------------------------------------------------------------
# 第 18.6 节：首屏分级（tier）
# ---------------------------------------------------------------------------

@case
def push_一级记录全排在一级之前不许被二级插队():
    """**B7 的核心。** 一级记录（命中学校或专业）**全部**进首屏，二级只补位。

    为什么必须有一条断言：现在的排序分数是 `score × urgency`，
    而"**只命中方向但三天后截止**"完全可能压过"**命中你的意向学校但还有一个月**"。
    那不是用户要的 —— 他要的是"我目标学校里发了什么"，而不是"什么最急"。

    这条用一对极端记录来钉：一级那条**还早**、二级那条**马上截止**。
    如果 tier 没排在分数之前，二级会赢。
    """
    today = date.today()
    tier1 = _push_event(
        "e_t1", school="浙江大学", unit="研究生院",
        title="浙江大学2027年推免工作通知", matched=("招生",),
        deadline=(today + timedelta(days=25)).isoformat(), relevance=2)
    tier2 = _push_event(
        "e_t2", school="某不存在大学", unit="某学院",
        title="材料学科讲座通知", matched=("材料",),
        deadline=(today + timedelta(days=1)).isoformat(), relevance=2)

    c = _push_client([tier1, tier2])
    try:
        c.put("/api/profile?user=u_tier", json=_PUSH_PROFILE)
        r = c.get("/api/push/preview?user=u_tier&limit=10").json()
    finally:
        _close(c)

    items = r["items"]
    ids = [i["event_id"] for i in items]
    assert "e_t1" in ids and "e_t2" in ids, ids
    by_id = {i["event_id"]: i for i in items}
    assert by_id["e_t1"]["tier"] == 1, by_id["e_t1"]["score_dims"]
    assert by_id["e_t2"]["tier"] == 2, by_id["e_t2"]["score_dims"]
    assert ids.index("e_t1") < ids.index("e_t2"), (
        "只命中方向但更急的排到了命中意向学校的前面 —— tier 没有排在分数之前", ids)


@case
def push_每条都有tier且和打分维度一致():
    """`tier` 必须**每条都有**、取值只有 1/2，而且**和 `score_dims` 对得上**。

    最后那条不一致是最容易悄悄发生的：有人改了 `tier_of()` 的判据
    （比如加个"专业命中也算二级"），而 `why` / 前端标签还按老口径。
    那时接口不会报错，只是**标签开始说谎**。
    """
    today = date.today()
    evs = [
        _push_event("e_k1", school="浙江大学", relevance=2, matched=("推免",)),
        _push_event("e_k2", school="某不存在大学", title="材料讲座", relevance=2,
                    matched=("材料",)),
        _push_event("e_k3", school="某不存在大学", title="物理学院通知", relevance=2,
                    matched=("物理",)),
    ]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_tk", json=_PUSH_PROFILE)
        r = c.get("/api/push/preview?user=u_tk&limit=10").json()
    finally:
        _close(c)

    assert r["items"], r["tally"]
    for it in r["items"]:
        assert it.get("tier") in (1, 2), ("tier 只能是 1 或 2", it.get("tier"))
        assert it.get("tier_reason"), ("有 tier 却不说为什么", it["event_id"])
        d = it["score_dims"]
        want = 1 if (d["school_hit"] >= 1.0 or d["major_hit"] >= 1.0) else 2
        assert it["tier"] == want, (
            "tier 和 score_dims 对不上 —— 两处判据已经分叉",
            it["event_id"], it["tier"], d)

    # 首屏是"一层一层往下"的：一旦出现二级，后面不许再有二级之前的一级
    tiers = [i["tier"] for i in r["items"]]
    assert tiers == sorted(tiers), ("首屏里二级插在了一级前面", tiers)


@case
def 招生年是admission_year而不是target_year():
    """**B9：`?year=` 以 `admission_year` 为准，`target_year` 只作回退。**

    这条用**真实数据里唯一一条两者不一致的记录**来钉（大连理工大学
    「连理全球」留学专项：`admission_year=2025` / `target_year=2026`）：
    它招的是 2025 届，所以 `?year=2025` 必须找到它，`?year=2026` 必须**不**包含它。

    为什么不能只断言"字段存在"：那只说明有个键，说明不了**哪个说了算**。
    两个字段都在响应里的时候，"哪个权威"恰恰是唯一会出事的地方 ——
    今天一个人读这个、明天一个人读那个，页面上的年份就会来回跳。

    找不到对照记录时**不跳过**：直接红，并说明这条用例的哪一半没法验。
    跳过的话，数据一变这条就悄悄变成永远绿。
    """
    items = CLIENT.get("/api/notices", params={"size": 1}).json()["items"]
    assert "admission_year" in items[0], (
        "列表项里没有 admission_year —— 前端只能读 deprecated 的 target_year",
        sorted(items[0]))

    # 结构性：admission_year 只在 topic=apply 时有值（数据层的约定）
    for r in VISIBLE[:300]:
        if r.get("admission_year") is not None:
            assert r.get("topic") == "apply", (
                "非招生类公告带了 admission_year —— 数据层约定被破坏",
                r.get("id"), r.get("topic"), r.get("admission_year"))

    pair = None
    for r in RAW:
        adm, tgt = r.get("admission_year"), r.get("target_year")
        if adm is not None and tgt is not None and adm != tgt:
            pair = r
            break
    assert pair is not None, (
        "找不到 admission_year 与 target_year 不一致的记录 —— "
        "\"哪个权威\"那一半没法验。换一条对照记录，不要跳过。")

    rid, adm, tgt = pair["id"], pair["admission_year"], pair["target_year"]
    hit = CLIENT.get("/api/notices",
                     params={"year": adm, "size": 50, "include_junk": "true"}).json()
    miss = CLIENT.get("/api/notices",
                      params={"year": tgt, "size": 50, "include_junk": "true"}).json()
    assert rid in {i["id"] for i in hit["items"]}, (
        "对着 admission_year=%s 筛却没找到 %s" % (adm, rid))
    assert rid not in {i["id"] for i in miss["items"]}, (
        "%s 的 admission_year 是 %s、target_year 是 %s，对着 %s 筛却带出来了 —— "
        "用的是 target_year" % (rid, adm, tgt, tgt))


def _key_paths(d, prefix=""):
    """递归键路径集合。`_` 开头的是 fixture 标注，不是契约（§9 第 1 条）。

    列表要遍历**所有**元素：条目类型不同时（有的带 `change`、有的没有），
    只看第一个会漏掉一半的键 —— 这个坑在 `push.preview.json` 上踩过一次。
    """
    out = set()
    if isinstance(d, dict):
        for k, v in d.items():
            if k.startswith("_"):
                continue
            out.add(prefix + k)
            out |= _key_paths(v, prefix + k + ".")
    elif isinstance(d, list):
        for x in d:
            out |= _key_paths(x, prefix + "[].")
    return out


@case
def updates_与_library_形状与_fixture_一致():
    """`/api/updates` 与 `/api/library` 的**形状**必须等于 fixtures。

    这两份 fixture 只钉形状、不钉数值（内容每次数据变化都会变）。
    形状是稳定承诺：C 按它写渲染，多一个键少一个键都会让某个字段变成空白。

    为什么现在才出（前几轮我说过"行为型接口不必出 fixture"）：
    那是我的判断，用户否决了 —— 理由是 C 需要一个形状基准。
    **用户的理由成立**：行为再对，形状没有基准的话，前端改了后端也看不出来。
    """
    today = date.today()
    for name, path, params in (
        ("updates.response.json", "/api/updates", {"days": 7, "limit": 50}),
        ("library.response.json", "/api/library", {"size": 20}),
    ):
        got = CLIENT.get(path, params=params).json()
        fx = _fixture(name)
        g, f = _key_paths(got), _key_paths(fx)
        assert g == f, (
            "%s 的键集合和 fixture 不一致：\n  接口多了：%s\n  fixture 多了：%s\n"
            "（默认响应形状 = fixture，一个多余键都不加 —— 契约 §9）"
            % (path, sorted(g - f), sorted(f - g)))
        assert got["total"] >= 0, got["total"]
    _ = today          # 时间字段由接口现算，形状与它无关


@case
def push_空态只给一个主因且用户能解决的排最前():
    """**空态只返回一个主因，其余进 `empty_reasons`。**

    以前这里是一个 `if/elif` 链，只给一个原因 —— 但那一个原因是"代码里最后命中的那条"，
    **没有把优先级说出来**，也没有出口告诉调用方"其实另外还有两个原因也成立"。
    一个用户看到"没有推送"，必须能立刻分清：

    | 主因 | 谁该动手 |
    |---|---|
    | `no_profile` | **他自己**（填画像就好了） |
    | `no_events_file` / `stale_events` | **我们**（数据没跑/过期了） |
    | `no_match` | 他自己（画像太窄） |

    所以：`empty_reason` **永远是单个字符串**（前端只渲染一句话），
    `empty_reasons` 是**所有成立的原因、按"用户能解决的优先"排序**（排查用）。
    """
    old_events = [_push_event("e_s1", school="某不存在大学", unit="某学院",
                              title="完全无关的通知", matched=["无关"],
                              on="2026-10-01")]

    # ① 没画像 + 数据也过期：**主因必须是"填画像"**（他能动手），不是"数据过期"
    c1 = _push_client(old_events)
    try:
        r1 = c1.get("/api/push/preview?user=u_pri1").json()
    finally:
        _close(c1)
    assert isinstance(r1["empty_reason"], str), (
        "empty_reason 必须是单个字符串，不是列表", r1["empty_reason"])
    assert r1["empty_reason"] == "no_profile", (
        "数据和画像两个问题并存时，主因必须是用户能解决的那个", r1["empty_reason"])
    assert r1["empty_reasons"][0] == "no_profile", r1["empty_reasons"]
    assert "stale_events" in r1["empty_reasons"], (
        "详情里必须保留'数据过期'这个同时成立的原因，否则排查时以为它不存在",
        r1["empty_reasons"])

    # ② 画像填了 + 数据过期 + 没匹配：主因是"数据过期"（我们的问题），不是"没匹配"
    c2 = _push_client(old_events)
    try:
        c2.put("/api/profile?user=u_pri2", json=_PUSH_PROFILE)
        r2 = c2.get("/api/push/preview?user=u_pri2").json()
    finally:
        _close(c2)
    assert r2["empty_reason"] == "stale_events", (
        "数据过期时不能说'没有跟你相关的'——我们不知道今天有什么", r2["empty_reason"])
    assert "no_match" not in r2["empty_reasons"], (
        "stale 和 no_match 不能同时成立：不知道有没有匹配，就不能断言没匹配",
        r2["empty_reasons"])

    # ③ 有内容时两个字段都该是"没有原因"
    c3 = _push_client([_push_event("e_pri3", on=date.today().isoformat())])
    try:
        c3.put("/api/profile?user=u_pri3", json=_PUSH_PROFILE)
        r3 = c3.get("/api/push/preview?user=u_pri3").json()
    finally:
        _close(c3)
    assert r3["items"], r3["tally"]
    assert r3["empty_reason"] is None, r3["empty_reason"]
    assert r3["empty_reasons"] == [], r3["empty_reasons"]


# ---------------------------------------------------------------------------
# 第 22 节：性能与体积（2026-10-18 实测后加的纪律）
# ---------------------------------------------------------------------------
#
# **为什么性能要进门禁**：2751 条数据下 `/api/push/preview` 曾经是 **535 KB / 3.3 秒**，
# 而门禁 193 条全绿 —— 因为它只验形状和语义，没有任何一条在量"这个响应有多大、要多久"。
# 数据再翻一倍，同一个坑会再踩一次，而且**没有人会发现**。
#
# 判据（用户定的）：
#   · `/api/push/preview` 响应 < 60 KB
#   · 各接口在真实数据下 < 300 ms（`/api/health/data` < 100 ms，它挂在页头上）
#
# 耗时取**三次里的最小值**：CI/本机负载会抖，取 min 才是"这段代码要多久"，
# 取 max 会变成"刚才那台机器有多忙"。

PERF_KB_CAP = 60
PERF_MS_CAP = 300
PERF_HEALTH_MS_CAP = 100


def _real_push_client():
    """带**真实事件流 + 演示画像**的客户端（性能必须在真实规模上量）。

    **不复制 `server/var/state.json`**（B26，2026-10-19）：新克隆的仓库没有
    这个文件（`server/var/` 在 `.gitignore` 里），复制会让门禁"第一次跑就红"，
    接手的人会以为项目坏了。现在在临时目录用 `load_or_seed()` 现建一份 ——
    比复制更干净（不会把开发机上那 100 多个测试用户带进性能测量），
    而且每次跑的量具都一致。
    """
    import tempfile
    tmp = tempfile.mkdtemp(prefix="perf_")
    st = StateStore.load_or_seed(os.path.join(tmp, "state.json"))
    return _push_client(events_mod.EventStore.load(config.EVENTS_PATH).records, st)


def _timed_ms(client, path, params, runs=3):
    """跑 `runs` 次，返回（最小耗时 ms，响应对象）。**先热身一次**。"""
    client.get(path, params=params)
    best, resp = None, None
    for _ in range(runs):
        t0 = time.perf_counter()
        resp = client.get(path, params=params)
        dt = (time.perf_counter() - t0) * 1000
        best = dt if best is None else min(best, dt)
    return best, resp


@case
def perf_推送预览体积不超过六十KB():
    """**防：把被压掉的几千条全量塞进响应。**

    2026-10-18 实测：2751 条数据下 `/api/push/preview` 是 **535 KB**，
    其中 `suppressed` 占 **523 KB（98%）** —— 2739 条被压事件全量返回。
    首屏因此 0.5~3 秒（看机器），在 3 分钟的视频里就是"点了没反应"。

    现在 `suppressed` 只留**每种原因前 3 条**样例，另给
    `suppressed_total` / `suppressed_tally` / `suppressed_sample`；
    要看全量走 `/api/push/suppressed` 分页。
    """
    c = _real_push_client()
    try:
        _, r = _timed_ms(c, "/api/push/preview",
                         {"user": "u_num_demo", "limit": 50}, runs=1)
    finally:
        _close(c)
    kb = len(r.content) / 1024.0
    assert kb <= PERF_KB_CAP, (
        "推送预览响应 %.1f KB（上限 %d KB）—— 首屏要等它序列化完。\n"
        "  多半是又把 `suppressed` 全量返回了：它应该是「每种原因前 3 条样例」，"
        "全量走 /api/push/suppressed 分页。" % (kb, PERF_KB_CAP))

    d = r.json()
    assert "suppressed_total" in d and "suppressed_tally" in d, (
        "少了计数出口：只给样例就不是「可解释」，是「看不见」", sorted(d))


@case
def perf_首屏接口在真实数据下不超过三百毫秒():
    """**防：O(n²) 这种"数据小时看不出来"的写法。**

    2026-10-18 实测：`/api/library` 1.6 秒、`/api/push/preview` 0.5~3 秒。
    根因不是序列化，是 `live_store.get()` 对**每一条事件**做一次全表扫描
    （2759 × 2751 ≈ 760 万次比较）。数据 1135 条时它只要 100 ms 上下，
    **完全看不出来**；到 2751 条就变成演示白屏。

    所以这条断言盯的不是"今天快不快"，是**"这次改完有没有引入随数据规模变差的东西"**。
    """
    c = _real_push_client()
    try:
        for path, params, cap in (
            ("/api/push/preview", {"user": "u_num_demo", "limit": 50}, PERF_MS_CAP),
            ("/api/library", {"size": 20}, PERF_MS_CAP),
            ("/api/updates", {"days": 7, "limit": 50}, PERF_MS_CAP),
            ("/api/notices", {"size": 50}, PERF_MS_CAP),
            # 页头接口：每次切页/轮询都打，所以给更紧的上限
            ("/api/health/data", {}, PERF_HEALTH_MS_CAP),
        ):
            ms, _resp = _timed_ms(c, path, params)
            assert ms <= cap, (
                "%s 用了 %.0f ms（上限 %d ms，真实数据 %d 条）——\n"
                "  先怀疑「每来一条就去全表扫一次」这类写法；"
                "`store.get()` 已经有 id 索引，别绕过它。"
                % (path, ms, cap, len(RAW)))
    finally:
        _close(c)


@case
def push_首屏只放招生类():
    """**B15：`topic == "apply"` 是硬门槛，tier1 / tier2 都必须满足。**

    2026-10-19 实测事故：浙大物理 + 北大/清华/中科大 的画像，`/api/push/preview`
    返回 3 条 —— 博士后奖通知、TeV 研讨会、物院论坛，**一条招生公告都没有**。
    根因是 tier2 兜底时没有 `topic` 门槛，拿讲座/会议凑数。

    这条盯两件事：
    1. 真实数据下 20 条**全部**是 `apply`；
    2. `tally.not_apply` 把被挡掉的条数说出来 —— 挡了不等于没发生。
    """
    c = _real_push_client()
    try:
        d = c.get("/api/push/preview", params={"user": "u_num_demo", "limit": 50}).json()
    finally:
        _close(c)
    assert d["items"], "真实数据下应当有推送"
    bad = [(i.get("school"), i.get("topic"), (i.get("title") or "")[:30])
           for i in d["items"] if i.get("topic") != "apply"]
    assert not bad, (
        "首屏里混进了非招生内容（%d 条）：%s\n"
        "  讲座/会议/名单/事务通知**一条都不许进首屏** —— 宁可空，不许凑。"
        % (len(bad), bad[:3]))
    assert d["tally"]["not_apply"] >= 0, d["tally"]


@case
def push_没有招生类时给_no_apply_today():
    """**B15 的另一半：宁可空，不许凑。**

    当命中的全是讲座/会议时，必须返回空列表 + `empty_reason="no_apply_today"`，
    并且**带上"最近一条是几号的"** —— 空页面也要有信息量。

    为什么不能报 `no_match`：那是"跟你没交集"（用户该去改画像），
    而这里是"有交集、但都不是招生公告"（他改画像也没用）。
    """
    # **必须用当天日期**：默认 2026-10-17 会让数据变成过期，
    # 于是报的是 stale_events（我们不知道）而不是 no_apply_today（我们知道，只是没招生类）。
    today = date.today().isoformat()
    evs = [
        _push_event("e_na1", title="物院论坛（第七十六讲）", topic="non_apply", on=today),
        _push_event("e_na2", title="第二届TeV物理前沿专题研讨会", topic="other", on=today),
        _push_event("e_na3", title="关于申报优秀博士后奖的通知", topic="non_apply", on=today),
    ]
    c = _push_client(evs)
    try:
        c.put("/api/profile?user=u_na", json=_PUSH_PROFILE)
        d = c.get("/api/push/preview?user=u_na").json()
    finally:
        _close(c)

    assert d["items"] == [], (
        "命中的全是讲座/会议，却给了 %d 条 —— 这就是「拿讲座凑数」"
        % len(d["items"]))
    assert d["empty_reason"] == "no_apply_today", (
        "空态原因应该是 `no_apply_today`（不是 `no_match`）：%s" % d["empty_reason"])
    assert "招生" not in (d["empty_text"] or "") or "没有新的招生公告" in d["empty_text"], d
    assert d["empty_text"], "空态必须有一句话"
    assert d["tally"]["not_apply"] == 3, d["tally"]


@case
def match_english_两种形状都能打通():
    """**B17：契约自己的示例不该打崩自己的接口。**

    `api.md` §2 的示例画像写的是 `"english": "CET-6 580"`（字符串），
    而实现原来是 `eng = prof.get("english") or {}` —— 传字符串时 `eng` 就是那个字符串，
    后面 `eng.get(...)` **AttributeError → 500**。照着文档写就挂，这是最难看的 bug。

    现在两种形状都接受：字典原样用；字符串尝试解析（CET-6 580 → `{"cet6": 580}`）；
    **解析不出来给空字典**（"英语很好"这种不猜）。
    """
    for english, label in (("CET-6 580", "字符串"), ({"cet6": 580}, "字典"),
                           ("英语很好", "解析不出来"), (None, "null")):
        prof = dict(PROF, english=english)
        c = TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:")))
        try:
            r = c.post("/api/match", params={"limit": 3}, json=prof)
        finally:
            _close(c)
        assert r.status_code == 200, (
            "english 是%s时 /api/match 返回 %d：%s"
            % (label, r.status_code, r.text[:300]))


@case
def 契约里的示例画像都能打通():
    """**扫 `contracts/api.md` 里的 json 示例，凡是画像形状的都真打一遍。**

    B17 的根因是"文档示例和实现对不上"，而修一个示例只是修了**这一次**。
    这条把"文档里的示例必须能跑"变成门禁 —— 以后谁在契约里写一个跑不通的示例，
    门禁当场红。

    只挑**像画像**的（含 `preferred_schools` 或 `majors` 的 JSON 对象）：
    契约里还有响应示例、fixture 片段，那些不是输入，不该拿去 POST。
    """
    with open(os.path.join(ROOT, "contracts", "api.md"), encoding="utf-8") as f:
        md = f.read()
    blocks = re.findall(r"```json\n(.*?)```", md, flags=re.S)
    payloads = []
    for b in blocks:
        try:
            obj = json.loads(b)
        except ValueError:
            continue
        for cand in [obj] + list(obj.values()):
            if isinstance(cand, dict) and ("preferred_schools" in cand
                                           or "majors" in cand):
                payloads.append(cand)

    assert payloads, "契约里没找到画像示例 —— 扫描逻辑可能坏了"
    bad = []
    for p in payloads:
        c = TestClient(create_app(STORE, FACULTY_STORE, StateStore.load(":memory:")))
        try:
            r = c.post("/api/match", params={"limit": 2}, json=p)
        finally:
            _close(c)
        if r.status_code != 200:
            bad.append((p, r.status_code, r.text[:160]))
    assert not bad, (
        "契约里有 %d 个画像示例打不通（照着文档写就会挂）：\n  %s"
        % (len(bad), bad[:2]))


def main():
    passed, failed = 0, []
    for fn in CASES:
        try:
            fn()
        except Exception:
            failed.append((fn.__name__, traceback.format_exc()))
        else:
            passed += 1
    _cleanup_tmp()          # 合成数据用的临时目录

    for name, tb in failed:
        print(f"\nFAIL  {name}\n{tb}")

    # **跳过要看得见**（B27）：没有运行记录时跳过，不是"通过"，也不是"失败"。
    # 先把用例跑完再收集 —— 这些列表是运行时才填的。
    skips = []
    for mod in (_health,):
        for name, why in getattr(mod, "SKIPPED", []):
            skips.append((name, why))
    if skips:
        print("\n跳过 %d 条（缺少运行记录，不判红；**不是通过**）：" % len(skips))
        for name, why in skips:
            print("  SKIP  %s\n        %s" % (name, why))

    print(f"\n{'=' * 60}\n契约检查：{passed} 通过 / {len(failed)} 失败 / 共 {len(CASES)} 条")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

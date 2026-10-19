# -*- coding: utf-8 -*-
"""数据健康门禁：**让"数据对不对"变成会红的断言**。

## 为什么要有这个文件

`server/tests/run.py` 的 172 条用例验的全是**接口形状**（键集合、分页、错误格式、
三态文案）。它们全部通过，而数据里可以有 41 条正文不到 200 字、13 条事件指向不存在的公告。
**形状对 ≠ 内容对** —— 这个项目最贵的一次教训就是：接口 200、形状对、内容旧/空。

所以这里换一个视角：不问"接口返回得对不对"，问"**数据本身有没有坏**"。

## 八类判据，每一类都对应一个真实发生过（或几乎发生）的事故

| # | 判据 | 防的事故 |
|---|---|---|
| 1 | 非 junk 记录正文 < 100 字 == 0 | 数据层自己标了"不是垃圾"，正文却是空壳 —— 用户点开一条公告只有一句话 |
| 2 | `ok` 档正文 < 300 字 == 0 | `ok` 的定义就是"≥300 字"，出现即**分类器和数据打架** |
| 3 | junk 里正文 ≥300 字 的比例 ≤ 5% | **天津那次**：39 条正文完好，标题却被抽成了站点名（「天津大学研究生招生网」），于是整条被判 junk。这个比例一高，说明**坏的是标题抽取，不是正文** |
| 4 | `events_orphans.json` 条数 ≤ 20 | 悬空事件 = 事件指向的公告已经不在库里。少量是覆盖漂移（正常），成片就是抓取把栏目选丢了（复旦/吉大各 6 条那次） |
| 5 | 无事件公告 == 0 | 公告存在但没有任何事件 → **它对推送和"今日更新"是隐形的**（655 条那次断层） |
| 6 | 发布日缺失率 ≤ 3% | 没有发布日就排不了序，首屏永远见不到它（A3 修的那 52 条） |
| 7 | 截止日 `unknown` 占比与基线相比变化 ≤ 10 个百分点 | 抓取/解析规则一改，整批截止日会从 `found` 掉成 `unknown`，而接口毫无异常 |
| 8 | fast 轮次退出码与耗时健康 | 定时任务挂了没人知道 —— 这才是"数据停了但页面照常"的根源 |

另外两条**结构性**判据（不在上表编号里，但同样重要）：

| # | 判据 | 防的事故 |
|---|---|---|
| 9 | 同一 `url` 出现多条 == 0 | 去重真正失效（浙大物理 http/https 各抓一次，列表成对出现一模一样的卡片） |
| 10 | 同一 (school, title) 最多重复条数不超基线 | **标题坍塌**：一校几十条标题全变成站点名时，这个数会从 10 跳到 39。第 3 条看的是 junk 比例，这条看的是**标题本身**，两者互补 |

## 为什么第 6/7/10 条是"对基线"，不是绝对阈值

这三条衡量的是**变化**（分布突变），不是绝对值。
绝对阈值会随数据规模自然漂移，写死它就会变成"门禁在测今天抓了几条"
（这个项目栽过五次）。基线存在 `contracts/data_health_baseline.json`，
**是提交进版本库的**，改它必须在 journal 里说明理由。

重出基线：

    D:\\python.3.10.11\\python.exe server/tests/test_data_health.py --write-baseline
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import config, quality                          # noqa: E402

NOTICES = config.NOTICES_PATH
EVENTS = config.EVENTS_PATH
ORPHANS = os.environ.get(
    "TUIMIAN_ORPHANS", os.path.join(ROOT, "data", "out", "events_orphans.json"))
LOGS = os.environ.get("TUIMIAN_LOGS", os.path.join(ROOT, "data", "logs"))
BASELINE = os.path.join(ROOT, "contracts", "data_health_baseline.json")

# ---- 阈值（写死在这里，改它要在 journal 里说理由）----
MAX_NO_EVENT = 0                    # 无事件公告
MAX_ORPHANS = 20                    # 悬空事件
MAX_PUBLISH_MISSING_RATE = 0.03     # 发布日缺失率（实测 0.80%）
MAX_JUNK_WITH_GOOD_BODY = 0.05      # junk 里正文完好的比例（A6 前实测 33%，见第 3 条）
MIN_THIN_BODY = 100                 # 非 junk 的正文下限
MIN_OK_BODY = 300                   # ok 档的正文下限
MAX_UNKNOWN_SHIFT = 0.10            # 截止日 unknown 占比的最大变化
MAX_FAST_SECONDS = 20 * 60          # fast 轮次耗时上限（实测约 130 秒）
TITLE_COLLAPSE_SLACK = 5            # 同校同名的最大重复条数，允许比基线多几条

# 院系单位后缀。标题以它结尾 + 正文里是招生目录，说明抓到的是**索引页**而不是公告
# （北大 zsml_ss_yx.html 那一类）。这类记录的标题是院系名，用户看到的卡片
# 就写着「地球与空间科学学院」，点开是一个导航列表。
UNIT_SUFFIXES = ("学院", "学系", "系", "研究院", "中心")
INDEX_PAGE_MARK = "招生目录"
MAX_INDEX_PAGE_OK = 0               # 这种记录**一条**都不许判成 ok

# 第二类索引页：标题 = 「001信息与通信工程学院」，正文是一串 001/002/003… 的院系列表。
# 电子科技大学 	dfc/a001… 那一类。只看招生目录会漏掉它（正文写的是招生宣传/夏令营）。
_CODE_UNIT_RE = re.compile(r"^\d{3}[\u4e00-\u9fff（）()]+(学院|学系|系|研究院|中心)$")
_CODE_TOKEN = re.compile(r"\d{3}[\u4e00-\u9fff]")
INDEX_CODE_MIN = 5                  # 正文里至少这么多个不同的编号+汉字


def _code_count(body):
    return len(set(_CODE_TOKEN.findall(body or "")))


def _load_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _body_len(rec):
    return len((rec.get("body_text") or "").strip())


def _notices():
    return _load_jsonl(NOTICES)


def _events():
    return _load_jsonl(EVENTS)


def _baseline():
    if not os.path.exists(BASELINE):
        raise AssertionError(
            "缺少数据健康基线 %s。\n"
            "  它是第 6/7/10 条判据的比较对象（突变检测），没有它这三条会变成"
            "\"永远通过\"—— 那比没有更糟。\n"
            "  重出：python server/tests/test_data_health.py --write-baseline"
            % os.path.relpath(BASELINE, ROOT))
    with open(BASELINE, encoding="utf-8") as f:
        return json.load(f)


def measure():
    """当前数据的关键指标。**基线文件和门禁都用它算**，避免两套口径。"""
    recs = _notices()
    evs = _events()
    n = len(recs)

    quality_of = {r.get("id"): (r.get("record_quality") or "unknown") for r in recs}
    junk = [r for r in recs if quality_of[r.get("id")] == quality.QUALITY_JUNK]
    ok = [r for r in recs if quality_of[r.get("id")] == quality.QUALITY_OK]
    non_junk = [r for r in recs if quality_of[r.get("id")] != quality.QUALITY_JUNK]
    # 部署版脱敏数据：名单类公告正文被有意替换成一句说明，不算"空壳"（①②只看未脱敏的）
    _redacted = {r.get("id") for r in recs
                 if (r.get("body_text") or "").startswith(config.REDACTED_BODY_PREFIX)}
    ok_bodies = [r for r in ok if r.get("id") not in _redacted]
    non_junk_bodies = [r for r in non_junk if r.get("id") not in _redacted]

    # 截止日三态
    states = {}
    for r in recs:
        st = (r.get("field_state") or {}).get("deadline") or "unknown"
        states[st] = states.get(st, 0) + 1

    # 同校同名
    same = {}
    for r in recs:
        key = (r.get("school"), (r.get("title") or "").strip())
        same[key] = same.get(key, 0) + 1
    max_shared = max(same.values()) if same else 0

    urls = {}
    for r in recs:
        urls[r.get("url")] = urls.get(r.get("url"), 0) + 1

    have = {e.get("notice_id") for e in evs}
    no_event = sum(1 for r in recs if r.get("id") not in have)

    orphans = 0
    if os.path.exists(ORPHANS):
        with open(ORPHANS, encoding="utf-8") as f:
            orphans = int(json.load(f).get("count") or 0)

    return {
        "notices": n,
        "events": len(evs),
        "junk": len(junk),
        "thin": sum(1 for r in recs if quality_of[r.get("id")] == quality.QUALITY_THIN),
        "ok": len(ok),
        # ① ②
        "non_junk_body_lt_100": sum(1 for r in non_junk_bodies if _body_len(r) < MIN_THIN_BODY),
        "ok_body_lt_300": sum(1 for r in ok_bodies if _body_len(r) < MIN_OK_BODY),
        # ③
        # "正文完好却被判 junk" —— 但**排除索引页**（ody_is_index* / _is_index_page）：
        # 索引页的正文是院系导航列表，长度天然 >=300，被判 junk 是**正确**的。
        # 不排除的话，A14 把招生目录判成 junk 之后这条会从 0% 跳到 13%，
        # 报出来的意思是"标题抽取坏了"，而真相是"我们刚修好了索引页误收"。
        "junk_with_good_body": sum(1 for r in junk if _wrongly_condemned(r)),
        "junk_with_good_body_ratio": round(
            (sum(1 for r in junk if _wrongly_condemned(r)) / len(junk)), 4) if junk else 0.0,
        # ④ ⑤ ⑥
        "orphans": orphans,
        "no_event": no_event,
        "publish_missing": sum(1 for r in recs if not (r.get("publish_date") or "").strip()),
        "publish_missing_ratio": round(
            sum(1 for r in recs if not (r.get("publish_date") or "").strip()) / n, 4) if n else 0.0,
        # ⑦
        "deadline_states": states,
        "unknown_ratio": round(states.get("unknown", 0) / n, 4) if n else 0.0,
        # ⑨ ⑩
        "dup_url": sum(1 for v in urls.values() if v > 1),
        "max_same_title": max_shared,
        # ⑪ 索引页冒充公告
        "index_pages": sum(1 for r in recs if _is_index_page(r)),
        "index_page_ok": sum(1 for r in recs if _is_index_page(r)
                             and (r.get("record_quality") or "") == quality.QUALITY_OK),
    }


def _wrongly_condemned(rec):
    """被判 junk，但**很可能是一条真公告被冤枉了**。

    ## 这个判据 2026-10-18 改过一次，原因是原版有个逻辑漏洞

    原版是"junk 且正文 ≥300 字 且 不是索引页"。问题在于：
    **索引页的正文天然很长**（北大「招生目录 硕士 理学部 数学科学学院…」有 2.7 万字），
    而我的"是不是索引页"判据只认了两种形状，漏掉了标题是「北京大学研究生招生网」
    的那几个 `zsml_*.html`。于是它们被算成"真公告被冤枉"，
    把比例顶到 9.4% —— **这等于逼着数据层把正确的索引页判据放宽**。
    判据设计错了会反向伤害正确的实现。

    现在的判据（三条同时成立）：

    1. **`record_quality == "junk"`**
    2. **不是索引页**（先排除 —— 索引页正文长是正常的，见 `_is_index_page`）
    3. **标题确实不可靠**（站点名 / 机构名）**且正文里有完整句子**（`。！？`）

    第 3 条是关键：真公告的正文是**成句的文章**，索引页/导航页是**词串**。
    加上"标题不可靠"这个前提，才不会把"标题正常但正文短"的记录也算进来。
    """
    if (rec.get("record_quality") or "") != quality.QUALITY_JUNK:
        return False
    if _is_index_page(rec):
        return False
    if not _title_is_unreliable(rec):
        return False
    return _has_sentence(rec.get("body_text") or "")


# 数据层标"这一页是索引/列表"的命中名（`collector/quality.py`）。
# 用命中名而不是自己再判一遍：数据层判过的，接口层不要重新发明。
INDEX_HITS = ("body_is_index", "body_nav_list", "body_numbered_nav")

# 标题不可靠的两类后缀：站点名。
SITE_NAME_SUFFIXES = ("招生网", "研究生招生信息网", "研究生院", "招生信息网", "研招网")


def _title_is_unreliable(rec):
    """标题是站点名 / 机构名，**不是一条公告的标题**。

    两个来源都认：数据层的 `quality_hits`（它已经判过了），以及我自己的后缀匹配
    （老数据可能没有 hits，但标题一眼就是站点名）。
    """
    hits = [str(h).split("(")[0] for h in (rec.get("quality_hits") or [])]
    if any(h in ("title_is_site_name", "title_is_org_only") for h in hits):
        return True
    title = (rec.get("title") or "").strip()
    return bool(title) and title.endswith(SITE_NAME_SUFFIXES)


def _has_sentence(body):
    """正文里有**成句**的标点。索引页/导航页是词串，通常一个句号都没有。"""
    return bool(_SENTENCE_RE.search(body or ""))


_SENTENCE_RE = re.compile(r"[。！？；]")


def _is_index_page(rec):
    """这条是**栏目索引页**冒充公告吗？三类，都属同一个事故。

    **① 招生目录索引页**：标题以院系单位结尾（学院/学系/系/研究院/中心）
    且正文里出现"招生目录"（北大 `zsml_ss_yx.html` 那一类）。

    **② 院系导航页**：标题就是「001信息与通信工程学院」这种**编号 + 院系名**，
    正文是一串 001/002/003… 的院系列表（电子科技大学 `tdfc/a001…` 那一类）。

    **③ 正文**开头就是**一张目录**（2026-10-18 补）：标题可能是「北京大学研究生招生网」
    这种站点名、不以后缀结尾，判不到 ① 里，但正文一上来就是
    「招生目录 硕士 理学部 数学科学学院 物理学院…」—— 那也不用再看标题了。

    > ⚠️ 第 ③ 条**必须是"开头就是"**，不能写成"正文前 300 字里出现招生目录"。
    > 后者实测会误抓 **25 条正常公告**（南大/西安交大那种正文里带导航痕迹的），
    > 而那 25 条全是 `ok` —— 判据一宽就会反过来逼着数据层去改正确的东西。

    为什么要限定正文：光看标题会误伤 ——「化学与分子工程学院」完全可能是
    一条真的复试安排通知。加正文特征之后，判的是"这是一张索引，不是一条公告"。

    > ① 实测 4 条、② 1 条、③ 4 条（北大研招网 2.7 万字那几条），合计 9 条。
    """
    title = (rec.get("title") or "").strip()
    body = rec.get("body_text") or ""
    if title.endswith(UNIT_SUFFIXES) and INDEX_PAGE_MARK in body:
        return True
    if _CODE_UNIT_RE.match(title) and _code_count(body) >= INDEX_CODE_MIN:
        return True
    # ③ 只认"正文开头就是目录"。放宽到"前 300 字里出现"，实测误抓 25 条正常公告。
    if body.lstrip().startswith(INDEX_PAGE_MARK) and not _has_sentence(body[:300]):
        return True
    return False


# ---------------------------------------------------------------------------
# 判据 1-2：正文空壳
# ---------------------------------------------------------------------------

def data_非junk记录不许有正文空壳():
    """**防：一条公告被标成"正常"，点开只有一句话。**

    判据：`record_quality != "junk"` 且正文 < 100 字 的记录数 == 0。

    为什么下限是 100 而不是派单写的 200：**`thin` 档的定义就是 100~300 字**
    （`collector/quality.py`）。按 200 判，41 条 `thin` 会全部命中 ——
    而那 41 条是**设计上允许短**的"见附件"型公告，附件里才是正文。
    按 200 判等于让门禁去红一批本来正确的记录，红久了所有人都会学会忽略它。
    """
    m = measure()
    assert m["non_junk_body_lt_100"] == 0, (
        "有 %d 条非 junk 记录正文不足 %d 字 —— 用户点开会看到一句空壳"
        % (m["non_junk_body_lt_100"], MIN_THIN_BODY))


def data_ok档正文必须达到三百字():
    """**防：分类器和数据自己打架。**

    `ok` 的定义就是"≥300 字"。如果一条 `ok` 记录正文只有 150 字，
    说明 `record_quality` 不是按它声明的规则算出来的 —— 那整列的信用就没了，
    而下游（首屏、推送、套磁信）全都按这个字段决定收不收。
    """
    m = measure()
    assert m["ok_body_lt_300"] == 0, (
        "有 %d 条标成 ok 的记录正文不足 %d 字 —— 分类判据和数据不一致"
        % (m["ok_body_lt_300"], MIN_OK_BODY))


# ---------------------------------------------------------------------------
# 判据 3：junk 里正文完好的比例（天津那次）
# ---------------------------------------------------------------------------

def _junk_good_body_recs():
    """被判 junk、但正文有实质内容的记录（"好记录被当垃圾扔了"的嫌疑犯）。"""
    return [r for r in _notices()
            if (r.get("record_quality") or "") == quality.QUALITY_JUNK
            and _wrongly_condemned(r)]


def _hit_breakdown(recs):
    """按 `quality_hits` 分组 —— 报错里直接告诉人**是哪条判据**太宽了。

    只报总数的话，值班的人还得自己筛一遍；而"哪条判据"恰好是修法的入口。
    """
    from collections import Counter
    c = Counter()
    for r in recs:
        hits = r.get("quality_hits") or ["(无判据)"]
        c[" + ".join(str(h).split("(")[0] for h in hits)] += 1
    return "，".join("%s ×%d" % (k, v) for k, v in c.most_common())


def data_junk里正文完好的比例不许超过百分之五():
    """**防天津那次：39 条正文完好、标题被抽成站点名，于是整条被判 junk。**

    这条和"正文空壳"是同一个病的两面：
    - 正文空壳 → 记录本身没内容；
    - **这条 → 内容还在，坏的是标题**。

    ## 判据 2026-10-18 改过，因为原版会反向伤害正确的实现

    原版是"junk 且正文 ≥300 字 且 不是索引页"。漏洞：**索引页的正文天然很长**
    （北大「招生目录…」2.7 万字），而"是不是索引页"的判据当时漏了几个形状，
    于是它们被算成"真公告被冤枉"，比例顶到 9.4% ——
    **这等于逼着数据层把正确的索引页判据放宽**。用户指出了这一点，判据跟着改。

    现在的口径（见 `_wrongly_condemned`）：
    **junk + 不是索引页 + 标题确实不可靠（站点名/机构名）+ 正文里有完整句子**。
    只统计"标题坏了但正文是真文章"的记录 —— 那才是"真公告被冤枉"。
    """
    m = measure()
    ratio = m["junk_with_good_body_ratio"]
    assert ratio <= MAX_JUNK_WITH_GOOD_BODY, (
        "被判 junk 但**像是真公告被冤枉**的占 %.1f%%（%d/%d，上限 %.0f%%）——\n"
        "  口径：junk + 非索引页 + 标题不可靠 + 正文有完整句子。\n"
        "  按判据分组：%s\n"
        "  样例（标题 / 正文前 40 字 / url）：\n%s\n"
        "  修法（数据层）：把命中的那条判据收窄，或先从正文/缓存页回捞标题 —— 见 issues/011。\n"
        "  **不要放宽本断言的上限**：它红一次，就说明有一批真公告用户看不到了。\n"
        "  **也不要靠放宽索引页判据来让它变绿**：索引页判 junk 是对的。"
        % (ratio * 100, m["junk_with_good_body"], m["junk"],
           MAX_JUNK_WITH_GOOD_BODY * 100,
           _hit_breakdown(_junk_good_body_recs()),
           "\n".join("    · %s / %s / %s"
                     % ((r.get("title") or "")[:26],
                        (r.get("body_text") or "")[:40].replace("\n", " "),
                        (r.get("url") or "")[:64])
                     for r in _junk_good_body_recs()[:5])))


# ---------------------------------------------------------------------------
# 判据 4-5：事件与公告的对齐
# ---------------------------------------------------------------------------

def data_悬空事件不许超过二十条():
    """**防：成片的事件指向已经不存在的公告。**

    少量悬空是覆盖漂移的必然（复旦 6 条：那一栏今年没被选中；吉大 6 条：
    栏目识别把导航条当成了栏目）。它们不删（事件流是 append-only 的历史），
    只在 `events_orphans.json` 记账。

    **但成片出现就是事故**：说明栏目配置把某所学校整片丢了，
    而接口层毫无异常 —— 用户只是"看不到那所学校的新公告"。
    """
    m = measure()
    assert m["orphans"] <= MAX_ORPHANS, (
        "悬空事件 %d 条（上限 %d）—— 事件指向的公告成片消失，"
        "通常是栏目选择漂移把整栏丢了" % (m["orphans"], MAX_ORPHANS))


def data_每条公告都必须有事件():
    """**防 655 条那次断层：公告进了库，却没有对应事件。**

    `events.jsonl` 是推送和"今日更新"的**唯一输入**。一条公告没有事件，
    就等于它对这两个功能**隐形** —— 而列表页看得见它。
    用户会发现"这条在列表里有，但从来没推送过"，而我们没有任何报错。
    """
    m = measure()
    assert m["no_event"] == MAX_NO_EVENT, (
        "%d 条公告没有任何事件 —— 它们对推送和今日更新是隐形的"
        % m["no_event"])


# ---------------------------------------------------------------------------
# 判据 6：发布日
# ---------------------------------------------------------------------------

def data_发布日缺失率不许超过百分之三():
    """**防：公告排不了序，首屏永远见不到它。**

    `sort=publish_desc` 是默认排序。没有发布日的记录会沉到最后一页，
    而用户只看第一屏 —— 等于这条公告**没有发布日的时候就等于不存在**。
    A3 修的那 52 条（"更新时间"标签没认出来）就是这个形状。
    """
    m = measure()
    assert m["publish_missing_ratio"] <= MAX_PUBLISH_MISSING_RATE, (
        "发布日缺失 %.2f%%（%d/%d，上限 %.0f%%）—— 这些记录排不了序，"
        "在默认列表里等于不存在"
        % (m["publish_missing_ratio"] * 100, m["publish_missing"],
           m["notices"], MAX_PUBLISH_MISSING_RATE * 100))


# ---------------------------------------------------------------------------
# 判据 7：截止日分布突变
# ---------------------------------------------------------------------------

def data_截止日unknown占比不许突变():
    """**防：解析规则一改，整批截止日从 found 掉成 unknown，而接口毫无异常。**

    这条**不看绝对值**（25% 的 unknown 是目前的事实，其中大量是公告本来就没写截止日），
    看的是**变化**：与基线相比超过 10 个百分点，说明有一整批记录的判定翻转了。

    为什么必须对基线而不是对固定阈值：unknown 的真实占比取决于"今年有多少公告
    会写截止日"，那是数据的事实，写死一个数就成了"门禁在测今天抓了什么"。
    """
    base = _baseline()
    m = measure()
    was = float(base["unknown_ratio"])
    now = m["unknown_ratio"]
    shift = abs(now - was)
    assert shift <= MAX_UNKNOWN_SHIFT, (
        "截止日 unknown 占比从基线的 %.1f%% 变成 %.1f%%（变化 %.1f 个百分点，上限 %.0f）—— "
        "通常是一整批记录的截止日判定翻转了（抽取规则改动 / 模板改版）。\n"
        "  若确认是数据侧的正常变化：重出基线并写 journal：\n"
        "  python server/tests/test_data_health.py --write-baseline"
        % (was * 100, now * 100, shift * 100, MAX_UNKNOWN_SHIFT * 100))


# ---------------------------------------------------------------------------
# 判据 8：fast 轮次健康
# ---------------------------------------------------------------------------

def _fast_runs():
    """fast 轮次列表 —— **直接复用 server/heartbeat.py 的解析**。

    这里原本有一份自己的日志解析器（_RUN_START / _EXIT / _parse_time…），
    和心跳接口那份**逐字重复**。两份解析同一个日志，迟早分叉 ——
    而这个项目已经因为"同一件事两处实现"栽过四次。

    合并之后有个额外好处："正在跑"的判定（in_progress）也只有一份。
    第一版我在这里把"没有 end 标记"一律当成"中途退了"，
    于是定时任务每跑一次、门禁就红一次 —— 而它跑得好好的。
    """
    from server import heartbeat as hb
    return [r for r in hb.all_runs(LOGS) if r.get("kind") == "fast"]


# 跳过的用例：**门禁会打印出来**，不静默。
# 为什么需要"跳过"这个概念：新克隆的仓库里 `data/logs/` 不存在（它在 .gitignore），
# 那是**正常状态**，不是"定时任务挂了"。红着等人加日志，久了所有人都会忽略这条。
SKIPPED = []


def _skip(name, why):
    SKIPPED.append((name, why))


def data_最近一轮fast必须成功():
    """**防：定时任务挂了没人知道。**

    这条是"数据停了但页面照常"的直接哨兵。接口永远 200、形状永远对，
    只有这一条能说出"最近一次抓取失败了"。

    判据分两层，因为**日志里可能没有退出码**：

    2026-10-18 实测 `data/logs/fast.log` 里**一条 `exit=` 都没有** ——
    `run_fast.cmd` 第 11 行那句 `echo ... crawl exit=%RC%` 没落进这份日志
    （那一轮不是从那个脚本起的）。这时只有两个诚实的选择：

    - **编一个"成功"**（文件看起来是完整的）—— 不行，那是把"不知道"说成"知道"；
    - **永远红着等别人加退出码** —— 也不行，红久了所有人都会学会忽略这条。

    所以：**先读显式退出码；没有就退到结构性证据**（这一轮有 `fast end` 标记
    **且** 写出过 `记录 N 条 ->` 那一行 —— 那行是 `crawl.py` 走到最后才打的）。
    两条证据都没有才红。用的是哪一层，从断言消息里能看出来，不静默。

    日志文件不存在 / 没有任何轮次时**跳过，不红**（B27，2026-10-19）：
    `data/logs/` 在 `.gitignore` 里，新克隆的仓库根本没有它 —— 那是
    "这台机器还没跑过定时任务"，不是"任务挂了"。**有日志且最近一轮失败才红。**
    """
    runs = _fast_runs()
    if not runs:
        _skip("data_最近一轮fast必须成功",
              "data/logs/ 里没有任何 fast 轮次（新克隆 / 从未跑过定时任务）—— 不判红")
        return

    last = runs[-1]
    ok = last.get("ok")

    # **"正在跑"是正常状态，不是故障。**
    # 第一版把"没有 end 标记"一律当成"中途退了"，于是定时任务每跑一次门禁就红一次
    # —— 而它其实跑得好好的。判定逻辑现在只有一份：`server/heartbeat.py`。
    if last.get("in_progress"):
        return

    assert ok is not None, (
        "最近一轮 fast 的成败**判断不出来**（既没有 `crawl exit=`，也没有 `记录 N 条 ->`）。\n"
        "  读不到不等于成功 —— 要么让运行脚本把 `crawl exit=%ERRORLEVEL%` 写进 fast.log，\n"
        "  要么这一轮真的没跑完。")
    assert ok is True, (
        "最近一轮 fast 失败了（退出码 %r）。日志最后一行：%s"
        % (last.get("exits"), last.get("start_line")))


def data_fast轮次耗时不许异常():
    """**防：fast 轮次从 2 分钟退化成 16 分钟（A4 修的就是这个）。**

    `--fast` 的意义是"两分钟跑完，可以一小时一次"。它一旦退化成全量，
    定时任务会自己踩自己（下一轮起来时上一轮还没完），而接口毫无异常。
    上限 20 分钟：足够容纳慢站点，但拦得住"fast 悄悄变成 full"。
    """
    runs = [r for r in _fast_runs() if r.get("ended") and r.get("seconds") is not None]
    if not runs:
        return                       # 上一条用例负责报"没有轮次 / 没跑完"
    last = runs[-1]
    seconds = last["seconds"]
    assert seconds <= MAX_FAST_SECONDS, (
        "最近一轮 fast 用了 %.0f 秒（上限 %d 秒）—— fast 可能退化成全量了"
        % (seconds, MAX_FAST_SECONDS))


# ---------------------------------------------------------------------------
# 判据 9-10：去重与标题坍塌
# ---------------------------------------------------------------------------

def data_同一个url不许出现多条():
    """**防去重真失效：浙大物理 http/https 各抓一次，列表成对出现一样的卡。**

    那次是 8 组记录、`id` 一模一样只有 url 协议不同。`make_fixtures.py` 里有
    `_check_unique` 但只查 `id`，不查 url —— 而 url 才是"同一篇公告"的真凭据。
    """
    m = measure()
    assert m["dup_url"] == 0, (
        "%d 个 url 对应多条记录 —— 去重失效，列表会出现成对的重复卡片"
        % m["dup_url"])


def data_标题不许坍塌():
    """**防标题坍塌：一校几十条标题全变成站点名。**

    天津那次就是 39 条同名「天津大学研究生招生网」。
    这条看的是**同校同名的最多重复条数**，和第 3 条（junk 比例）互补：
    第 3 条看"被判垃圾的比例"，这条看"标题本身有没有塌"。

    对基线而不是绝对值：同校同名有合法的（南开 9 条「2026年博士招生专业目录」
    是不同的 PDF 分册，url 各不相同），所以不能用"必须唯一"来判。
    """
    base = _baseline()
    m = measure()
    was = int(base["max_same_title"])
    now = m["max_same_title"]
    assert now <= was + TITLE_COLLAPSE_SLACK, (
        "同一 (学校, 标题) 最多重复 %d 条，基线是 %d 条 —— 标题很可能坍塌了"
        "（一整批抽成了同一个站点名）。" % (now, was))


# ---------------------------------------------------------------------------
# 注册与基线重出
# ---------------------------------------------------------------------------

def data_索引页不许冒充公告():
    """**防"招生目录索引页被当成一条公告"。**

    形状（北大 `zsml_ss_yx.html` 那一类，实测 4 条）：

    - `title` = **院系名**（「地球与空间科学学院」「化学与分子工程学院」）；
    - `url` = 招生目录索引页；
    - `body_text` = 院系导航列表（里面出现"招生目录"）。

    后果比"难看"严重：这类记录的 `relevance` 会是 **2**，于是**进首屏** ——
    用户看到一条叫「地球与空间科学学院」的"公告"，点开是一堆链接。
    它同时会污染套磁信的引文（标题引不到，只能引一段导航文字）。

    判据：标题以院系单位结尾 **且** 正文含"招生目录" 的记录，
    `record_quality` **一条都不许是 `ok`**（`thin` / `junk` 都可以）。

    为什么限定"正文含招生目录"：光看标题会误伤真公告 ——
    「化学与分子工程学院」完全可能是一条真的复试安排通知。
    """
    m = measure()
    bad = m["index_page_ok"]
    offenders = [r for r in _notices()
                 if _is_index_page(r)
                 and (r.get("record_quality") or "") == quality.QUALITY_OK]
    assert bad <= MAX_INDEX_PAGE_OK, (
        "有 %d 条**索引页**被判成 ok（共 %d 条这类记录）—— 它们的标题是院系名、"
        "正文是导航列表，而靠前的 relevance 会让它们进首屏。\n"
        "  用户会看到一条标题叫「地球与空间科学学院」的“公告”。\n"
        "  具体是：\n%s\n"
        "  修法（数据层）：`collector/quality.py` 把这两类都判成 junk ——\n"
        "    ① 标题以院系单位结尾 且 正文含\"招生目录\"（A14 已修）\n"
        "    ② 标题 = 三位编号+院系名 且 正文里 ≥%d 个 001/002… 的院系编号\n"
        "  见 issues/011。"
        % (bad, m["index_pages"],
           "\n".join("    · %-26s rel=%s  %s"
                     % ((r.get("title") or "")[:26], r.get("relevance"),
                        (r.get("url") or "")[:64]) for r in offenders[:5]),
           INDEX_CODE_MIN))


def data_正文完好判据要能抓到天津形状():
    """**验"验证方式本身"** —— 这条不测数据，测的是判据函数还灵不灵。

    背景：A14 把招生目录索引页判成 junk 之后，"junk 里正文完好"从 0% 跳到 13%，
    而那一次是**修对了**。为了不误报，`_wrongly_condemned()` 加了"排除索引页"。

    但排除条件是可以被写成无限宽的 —— 加一条 `title_is_site_name` 就能让
    **天津那次（39 条正文完好、标题是站点名）再也报不出来**，而门禁会一直绿。
    那是这个项目反复栽过的那种错：**修掉误报的同时把守卫也改死了。**

    所以这里用**四条**合成记录把函数的边界钉死（2026-10-18 加了第 ④ 条）：

    | 形状 | 应该 | 因为 |
    |---|---|---|
    | 标题站点名 + 正文成句的公告（**天津**） | **计入** | 好记录被扔了，必须报 |
    | 索引页 + 正文 500 字 | 不计入 | 判 junk 是对的 |
    | 正文 50 字 | 不计入 | 本来就是空壳 |
    | **标题站点名 + 正文是词串（无句号）** | 不计入 | 那是导航/目录，不是被冤枉的公告 |

    第 ④ 条是我改判据时补的：新判据要求"正文里有完整句子"，
    否则北大那种「招生目录 硕士 理学部 数学科学学院…」会被算成真公告。

    任何一条变了，说明判据被改动了，而"改判据"必须是一个有意识的决定。
    """
    # ① 天津形状：标题站点名、正文是成句的公告 → **必须计入**
    tianjin = {"title": "天津大学研究生招生网",
               "body_text": "根据学校安排，现就2027年推荐免试研究生报名工作通知如下。" * 8,
               "record_quality": quality.QUALITY_JUNK,
               "quality_hits": ["title_is_site_name"]}
    assert _wrongly_condemned(tianjin) is True, (
        "天津形状（标题是站点名 + 正文是成句公告）没有被计入 —— "
        "说明「排除索引页」的收窄把守卫改死了")

    # ② 索引页形状：正文是导航列表 → 判 junk 是对的，不该计入
    index_page = {"title": "地球与空间科学学院",
                  "body_text": "招生目录 " + "导航" * 250,
                  "record_quality": quality.QUALITY_JUNK,
                  "quality_hits": ["body_is_index(25)"]}
    assert _wrongly_condemned(index_page) is False, (
        "索引页被计入了「好记录被扔」—— 那会让这条断言对 A14 的修复误报")

    # ③ 正文空壳 → 不该计入（那时 junk 是对的）
    shell = {"title": "某公告", "body_text": "x" * 50,
             "record_quality": quality.QUALITY_JUNK, "quality_hits": ["body_lt_100"]}
    assert _wrongly_condemned(shell) is False, shell

    # ④ 标题站点名 + 正文是**词串**（没有句号）→ 不该计入。
    #    这一条防的是"新判据把索引页又收回来"（北大 2.7 万字那几条就是这个形状）。
    word_soup = {"title": "北京大学研究生招生网",
                 "body_text": "招生目录 硕士 理学部 数学科学学院 物理学院 " * 40,
                 "record_quality": quality.QUALITY_JUNK,
                 "quality_hits": ["title_is_site_name"]}
    assert _wrongly_condemned(word_soup) is False, (
        "标题站点名但正文是词串的记录被算成了「真公告被冤枉」—— "
        "那会逼着数据层放宽正确的索引页判据")


# ---------------------------------------------------------------------------
# 判据 12-14：freshness.json 的完整性（B14）
# ---------------------------------------------------------------------------
#
# 2026-10-18 事故：A 逐校分批跑，`freshness.json` 被某一批覆盖成 **2 所学校 / 0 条记录**，
# 而库里是 2751 条 / 29 所 —— 门禁 193 条**一条都没发现**。
# 原因：所有断言都只看 `notices.jsonl` 和事件流，**没人核对过 freshness 和主库对不对得上**。
# 而 `/api/health/data`、`/api/freshness` 都读它，页头会直接显示错的数。

FRESHNESS = config.FRESHNESS_PATH

# `generated_at` 允许比 `notices.jsonl` 的 mtime 早**这么多秒**。
#
# 为什么不是严格"不早于"：freshness 是按学校分批写的，而 notices/fixtures 可能在其后
# 几十秒内被同一轮流程重写一次（实测差 121 秒）。严格不早于会变成"每次跑完都红"，
# 红久了就没人看。**5 分钟足够抓住真事故**（那一批 freshness 落后了整轮，差几小时）。
FRESHNESS_MAX_LAG_S = 300


def _freshness():
    """读 `freshness.json`。**读不到就红** —— 它是页头和 §12 的唯一数据源。"""
    if not os.path.exists(FRESHNESS):
        raise AssertionError(
            "没有 %s —— `/api/freshness` 和 `/api/health/data` 都读它。\n"
            "  它不存在时接口会给三态 `status`，但「从来没生成过」本身就该被看见。"
            % os.path.relpath(FRESHNESS, ROOT))
    with open(FRESHNESS, encoding="utf-8") as f:
        return json.load(f)


def _fresh_rows(fr):
    """freshness 里的行（新形状 `schools`，老形状 `boards`）—— 两种都认。"""
    return fr.get("schools") or fr.get("boards") or []


def freshness_学校数与主库一致():
    """**防 2026-10-18 那次：freshness 被一批覆盖成 2 所学校，而库里 29 所。**

    页头"数据新鲜度"读的就是它。它少了 27 所学校时，用户看到的是
    "只有清华在更新"，而实际上我们抓了 29 所 —— **不是缺数据，是那份汇总错了**。

    判据：freshness 里出现的**学校名集合** == `notices.jsonl` 里的学校名集合。
    """
    fr = _freshness()
    rows = _fresh_rows(fr)
    have = {str(r.get("school") or "").strip() for r in rows} - {""}
    want = {(r.get("school") or "").strip() for r in _notices()} - {""}
    assert have == want, (
        "freshness 里的学校和主库对不上：\n"
        "  只在 freshness 里：%s\n"
        "  只在主库里：%s\n"
        "  （freshness %d 行 / 主库 %d 所）\n"
        "  这通常是「某一批把 freshness 整个覆盖成了那批的学校」。"
        % (sorted(have - want)[:8], sorted(want - have)[:8], len(have), len(want)))


def freshness_记录合计等于主库行数():
    """**防：freshness 说有 2 条，库里 2751 条。**

    学校名对齐但条数对不上，比"少几所学校"更难发现 —— 页面照常显示，
    只是"共 2751 条"和"各校合计 2 条"两句话同时存在，没人会去算。
    """
    fr = _freshness()
    total = sum(int(r.get("records") or 0) for r in _fresh_rows(fr))
    n = len(_notices())
    assert total == n, (
        "freshness 的 records 合计 %d ≠ 主库 %d 条（差 %+d）。\n"
        "  两句话会同时出现在页面上，而用户不会去算。" % (total, n, total - n))


def _newest_crawled_at():
    """主库最后一次抓取时刻：`notices.jsonl` 里 `crawled_at` 的**最大值**。

    **不用文件 mtime**（B27，2026-10-19）：Git 检出、bundle 克隆、复制、
    备份还原都会把 mtime 改成"操作时刻"，而内容里的时间不会变。
    空克隆跑门禁时，mtime 与 freshness 必然差几个小时 —— 用 mtime 天生失真。

    返回 `(datetime_or_None, 无法解析的条数)`。
    """
    from datetime import datetime
    newest, bad = None, 0
    for rec in _notices():
        s = rec.get("crawled_at")
        if not s:
            bad += 1
            continue
        try:
            dt = datetime.fromisoformat(str(s).strip())
        except (ValueError, TypeError):
            bad += 1
            continue
        if newest is None or dt > newest:
            newest = dt
    return newest, bad


def freshness_不是过期快照():
    """**防：freshness 是上一批/上一轮的快照，而主库已经换了。**

    时间维度是最容易漏的一维：条数对得上、学校对得上，但它其实是**昨天那份**。
    那时"数据新鲜度"会报一个陈旧的 `last_ok`，用户以为某校两天没更新，
    其实我们刚抓过。

    判据（B27 改）：**比数据内部时间，不比文件 mtime** ——

    - 主库时刻 = `notices.jsonl` 里 `crawled_at` 的**最大值**；
    - 汇总时刻 = `freshness.generated_at`；
    - `max(crawled_at) - generated_at <= 阈值` 才算正常。

    **只查一个方向**：freshness 不许落后于数据。反方向（汇总比数据新）
    通常是"这一轮没抓到新记录、但重新生成了汇总"，是正常的；
    用绝对差会把这种情况误判成"过期快照"（实测就有 16 小时的反向差）。
    """
    fr = _freshness()
    gen = str(fr.get("generated_at") or "").strip()
    assert gen, ("freshness 没有 generated_at —— 无法判断它是不是快照", sorted(fr))
    from datetime import datetime
    try:
        gen_dt = datetime.fromisoformat(gen)
    except ValueError:
        raise AssertionError("freshness.generated_at 不是 ISO 时间：%r" % (gen,))

    newest, bad = _newest_crawled_at()
    if newest is None:
        _skip("freshness_不是过期快照",
              "notices.jsonl 里没有可解析的 crawled_at —— 无法比较，不判红")
        return

    lag = (newest - gen_dt).total_seconds()
    assert lag <= FRESHNESS_MAX_LAG_S, (
        "freshness 比主库旧 %.0f 秒（上限 %d 秒）—— 它是**过期快照**。\n"
        "  freshness.generated_at = %s\n"
        "  max(notices.crawled_at) = %s\n"
        "  通常是分批跑时某一批把 freshness 写早了，而之后主库又被换掉。\n"
        "  （判据用**内容里的时间**，不用文件 mtime —— mtime 会被 Git 检出/复制改掉）"
        % (lag, FRESHNESS_MAX_LAG_S,
           gen_dt.isoformat(timespec="seconds"), newest.isoformat(timespec="seconds")))


CASES = [
    data_非junk记录不许有正文空壳,
    data_ok档正文必须达到三百字,
    data_junk里正文完好的比例不许超过百分之五,
    data_悬空事件不许超过二十条,
    data_每条公告都必须有事件,
    data_发布日缺失率不许超过百分之三,
    data_截止日unknown占比不许突变,
    data_最近一轮fast必须成功,
    data_fast轮次耗时不许异常,
    data_同一个url不许出现多条,
    data_标题不许坍塌,
    data_索引页不许冒充公告,
    # 不测数据、测判据本身：防止"修掉误报的同时把守卫改死"
    data_正文完好判据要能抓到天津形状,
    # B14：freshness 与主库的一致性
    freshness_学校数与主库一致,
    freshness_记录合计等于主库行数,
    freshness_不是过期快照,
]


def _write_baseline():
    m = measure()
    base = {
        "_note": ("数据健康门禁的比较基线（server/tests/test_data_health.py 的第 6/7/10 条判据）。"
                  "**改它必须在 journal 里说明理由** —— 它一改，突变检测就重设了。"),
        "recorded_on": __import__("datetime").date.today().isoformat(),
        "unknown_ratio": m["unknown_ratio"],
        "max_same_title": m["max_same_title"],
        "_observed": {
            k: m[k] for k in ("notices", "events", "junk", "thin", "ok",
                              "orphans", "no_event", "publish_missing_ratio",
                              "deadline_states", "junk_with_good_body")
        },
    }
    with open(BASELINE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(base, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("已写入 %s" % os.path.relpath(BASELINE, ROOT))
    print(json.dumps(base, ensure_ascii=False, indent=1))
    return 0


def main():
    if "--write-baseline" in sys.argv:
        return _write_baseline()
    m = measure()
    print(json.dumps(m, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

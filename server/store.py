# -*- coding: utf-8 -*-
"""公告数据访问层：加载 notices.jsonl、筛选、排序、分页、按 id 取详情。

设计约束（都来自契约，不是我自己加的）：
- 数据源只读。A 的 JSONL 是唯一真源，B 不写回、不改字段、不重新抓。
- 顺序即契约。`data/out/notices.jsonl` 本身是 `publish_date` 降序的，
  `contracts/fixtures/notices.list.json` 就是它的前 50 行（已验证逐行一致）。
  所以**默认排序必须是"按日期降序的稳定排序"**，否则 fixture 对不上。
- 不丢字段。列表项按 `LIST_FIELDS`（抄自 A 的 `collector/tools/make_fixtures.py`）
  投影，**和 fixture 逐字段一致**；详情接口给完整记录，一个字段都不裁。
- 派生字段：`has_attachments` / `attachments_count` 不在 JSONL 里（实测 0/78 条有），
  由 `project()` 从 `attachments` 现算 —— 契约要求列表必须给这两个。
- id 唯一性：**当前数据是 78 行 / 78 个唯一 id，重复已归零。**
  上一版（74 行 / 66 唯一）有 8 组浙大物理学院同一公告的 http/https 双版本，
  A 已在 `collector/crawl.py` 按 `canonical_url()` 归一化去重修掉（记 CHANGELOG）。
  消歧逻辑保留着（`get` 优先 https）—— 它是防御，不是当前需要，
  哪天去重再出问题，详情接口不会跟着飘。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

from server import config
from server import credibility
from server import quality

# 列表页保留的字段 = `contracts/fixtures/notices.list.json` 的形状。
# 逐字来自 collector/tools/make_fixtures.py 的 LIST_FIELDS，见 config.py 注释。
LIST_FIELDS = config.LIST_FIELDS

# `relevance` 缺失时的取值：1 = "说不清"那一档（见 `_sort_key_publish_desc`）。
# 不给 0，因为 0 的含义是"明显不是招生信息"，那是判断，不是缺失。
RELEVANCE_UNKNOWN = 1


def _year_hit(rec, year):
    """`?year=` 的判据：**以 `admission_year` 为准**，`target_year` 只作回退（契约 §1.5）。

    为什么不能两个都"精确匹配"：
    - `admission_year` 只在 `topic=apply` 时才有值（实测 470/1120），
      非招生类公告它是 null —— 但它本来就**不该**出现在"2027 招生"的结果里；
    - `target_year` 覆盖面大（1005 条），但它把"2027 年招生"和
      "2026 年写的日常事务"混在一起，所以它只能当**回退**。

    实测两者同时有值且不等只有 1 条（大连理工「连理全球」留学专项：
    `admission_year=2025` / `target_year=2026`）—— 那种情况下**以 admission_year 为准**，
    因为它回答的是"这条在招哪一届"。
    """
    adm = rec.get("admission_year")
    if adm is not None:
        return adm == year
    return rec.get("target_year") == year


# `/api/health` 里最多列出几条坏行的**细节**。坏行总数不截（`skipped_lines` 是准的），
# 只截"每条都摊开给你看"的那一份 —— 一个几千行全坏的文件不该把 health 刷成几 MB。
MAX_BAD_LINES_REPORTED = 20


def load_health(meta):
    """把 `NoticeStore.load()` 的 `load_meta` 变成一份**机器能判断**的自检结果。

    ## 这一节存在的理由

    坏行原来只被记成 `skipped_lines: 1` 这种裸数。它是**记下来了**（不是黑盒），
    但没有任何东西会因为它变红：某天数据源里真的坏了一行，服务静默少一条公告，
    126 条门禁全绿、`/api/health` 照常 200 —— **"少了东西"和"东西都在"长得一样**。

    这是本项目反复出现的那条原则的第六次：**"没有"和"我们不知道"必须是两句话**，
    这里对应的是 **"数据完整"和"数据缺了一块、但服务还活着"** 必须是两个状态。

    ## 三个状态，不是两个

    | `state` | 含义 | 该做什么 |
    |---|---|---|
    | `ok` | 文件里每一行都解析成功了 | 什么都不用做 |
    | `degraded` | **至少有一行解析失败/缺字段，已被跳过** | 去看 `bad_lines`，修数据源 |
    | `empty` | 文件读到了，但一条记录都没有 | 数据管道没跑/跑空了，不是"筛选没命中" |

    `degraded` 排在 `empty` 前面判断：全是坏行时 `loaded == 0`，
    但根因是"坏了"不是"还没跑"，报成 `empty` 会把人带去错误的方向。

    ## 一个刻意的取舍：**能用，但要响**

    `degraded` **不拒绝启动**，服务照常按能读到的记录工作（`usable` 恒为 True）。
    理由是演示现场一个字节坏了，要的是"还能用、但我知道它坏了"，
    而不是"整个站打不开"。但"响"必须有两处：**门禁变红**（`server/tests/run.py`
    的 `加载自检_坏行必须让门禁变红`）+ **这里说清第几行什么错**。

    `empty` 同理：0 条不是错误，是"数据还没进来"，接口不能假装"筛选没匹配到"。
    """
    bad = list(meta.get("bad_lines") or [])
    skipped = int(meta.get("skipped") or 0)
    loaded = int(meta.get("loaded") or 0)
    total_lines = loaded + skipped

    if skipped > 0:
        state = "degraded"
    elif loaded == 0:
        state = "empty"
    else:
        state = "ok"

    if state == "ok":
        message = f"{loaded} 条公告全部加载成功（共 {total_lines} 行，0 行损坏）"
    elif state == "degraded":
        head = bad[0] if bad else {}
        where = f"第 {head.get('line')} 行：{head.get('error')}" if head else "未给出细节"
        message = (f"{total_lines} 行里有 {skipped} 行无法解析（{where}）—— "
                   f"已跳过这 {skipped} 行，服务仍可用，但**少了 {skipped} 条公告**。"
                   f"要修的是数据源，不是接口")
    else:
        message = ("数据源读到了，但里面一条公告都没有（0 行）—— 接口能跑，"
                   "但没有任何数据可展示。这不是‘筛选没命中’，是数据还没进来")

    return {
        "state": state,
        # 恒 True：`degraded` / `empty` 都不阻止服务启动（见上面那段取舍）
        "usable": True,
        "loaded": loaded,
        "lines": total_lines,
        "skipped_lines": skipped,
        # 细节截断到 MAX_BAD_LINES_REPORTED 条，但 `bad_lines_total` 是准确的
        "bad_lines": bad[:MAX_BAD_LINES_REPORTED],
        "bad_lines_total": len(bad),
        "bad_lines_truncated": len(bad) > MAX_BAD_LINES_REPORTED,
        "message": message,
    }


def _parse_date(s):
    """'2026-10-16' -> date；解析不出来返回 None（不抛，脏数据不该打挂接口）。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _sort_key_publish_desc(rec):
    """默认排序键：`(relevance, publish_date)`，配合 `reverse=True` 就是两级降序。

    **两级，不是一级**（C 走查 #5 / A 的 `fields.relevance_of` 注释里也这么建议）：

    1. `relevance`（0|1|2，A 产出）降序 —— "像不像招生信息"
    2. 同一档内 `publish_date` 降序 —— 档内时效性不破坏

    为什么需要这个：原来只按日期排，于是最新的那几条**行政/科研信息**
    （「答辩评审名单」「论文送审双盲审名单」「Science 论文报道」）占据列表前几位，
    评委点开列表看到的第一条不是招生信息。

    **`relevance` 只用于排序，绝不用于隐藏**（A 的 `fields.relevance_of` 明确写了这条）。
    所以这里只是排序键，没有任何过滤。

    **缺字段按 1 处理**（"说不清"那档），不是 0 —— 老记录没有 `relevance` 时
    不该被当成"明显不是招生信息"往下压。缺失给中档，和 `matching-spec`
    "字段缺失给 0.4 不给 0"是同一条原则。

    方向由 `SORT_DIRECTIONS` 决定，本函数只给键。稳定排序保证同档同日的
    保持文件原始顺序。别再往里塞标题/url 之类的次键。
    """
    rel = rec.get("relevance")
    if rel is None:
        rel = RELEVANCE_UNKNOWN
    return (rel, _parse_date(rec.get("publish_date")) or date.min)


def _deadline_found_date(rec):
    """`found` 的截止日；不是 found / 解析不出来 → `None`。"""
    if credibility.state_of(rec, "deadline") != credibility.FOUND:
        return None
    return _parse_date(rec.get("deadline"))


def _sort_key_deadline_asc(rec):
    """按截止日升序：**未来最近的在前 →（可选）已过期的 → 没日期的沉底**。

    ## 2026-10-19 修（B25 反馈）：不能让 2010 年的排最前

    实测 `sort=deadline_asc` 前 5 条是 **2010 年**的已截止公告 ——
    用户点"按截止日排序"要的是"哪些快来不及了"，不是"哪些早就过期了"。

    现在的分组：

    | 组 | 谁 | 组内顺序 |
    |---|---|---|
    | 0 | 未来截止（`deadline >= 今天`） | 日期**升序**（最近的先） |
    | 1 | 已截止 | 日期**降序**（刚过期的先）—— 只有 `include_expired=true` 才在 |
    | 2 | `None`（absent / unknown） | 沉底，**不进主序列** |

    `None` 不能和已截止混在一起排：那是两件事（"我们没读到" vs "已经过了"）。
    """
    d = _deadline_found_date(rec)
    if d is None:
        return (2, 0)                      # absent / unknown：沉底
    today = date.today()
    if d < today:
        return (1, -d.toordinal())         # 已截止：刚过期的在前面
    return (0, d.toordinal())              # 未来：最近截止的在前


def _sort_key_deadline_desc(rec):
    """按截止日**倒序**：**已过期的在前（刚过期的先）→ 未来（最远的先）→ None**。

    「想看已过期的」用这个（用户 2026-10-19 的要求）：它就是 `deadline_asc` 的镜像 ——
    asc 先给"快到了的"，desc 先给"已经过了的"。
    """
    d = _deadline_found_date(rec)
    if d is None:
        return (2, 0)
    today = date.today()
    if d < today:
        return (0, -d.toordinal())         # 已截止：最近过期的在前
    return (1, -d.toordinal())             # 未来：最远的（最不急）先


SORT_KEYS = {
    "publish_desc": _sort_key_publish_desc,
    "deadline_asc": _sort_key_deadline_asc,
    "deadline_desc": _sort_key_deadline_desc,
}

# 排序方向。`reverse=True` 的语义是"最新在前"。
# 单独一张表，是为了让"键"和"方向"不会被谁不小心分开改。
SORT_DIRECTIONS = {
    "publish_desc": True,
    "deadline_asc": False,
    "deadline_desc": False,     # 分组键本身已经表达方向（B25）
}


def listing_sort_key(rec):
    """列表默认排序键 —— **给 `collector/tools/make_fixtures.py` 复用**。

    fixture 是照着接口响应切出来的，所以它必须用**和接口完全一样**的排序。
    两边各写一遍排序，迟早会分叉，而分叉的那次「接口和 fixture 对不上」
    会让人以为是接口坏了。

    A 生成 fixture 时直接 `from server.store import listing_sort_key`，
    或者至少把这段抄过去 —— 但抄的话就要接受它可能漂移。
    """
    return _sort_key_publish_desc(rec)


LISTING_SORT_DESC = (
    "relevance desc, publish_date desc（稳定）—— 见 server/store.py 的 "
    "_sort_key_publish_desc。relevance 只用于排序，不用于隐藏。"
)


def _hit(needle, hay):
    """大小写不敏感的子串命中。needle 为空视为不约束。"""
    if not needle:
        return True
    return needle.lower() in (hay or "").lower()


def _any_hit(needles, hay):
    return any(_hit(n, hay) for n in needles)


# 公开别名。`server/watch.py` 要复用**完全同一套**子串语义 ——
# 契约第 11 节承诺"列表页筛出来的"和"关注后收到的"是同一批，
# 那就必须是同一个函数，不能是复制过去的第二份实现。
# 复制出来的两份迟早漂移，而漂移的表现是"我在列表页筛到了，关注了却收不到提醒" ——
# 用户不会怀疑代码，他会认为提醒功能是坏的。
substring_hit = _hit
any_substring_hit = _any_hit


class NoticeStore:
    """把 JSONL 读进内存。当前 78 条的量级，不需要数据库。"""

    def __init__(self, path, records, load_meta):
        self.path = path
        self.records = records
        self.load_meta = load_meta
        # id -> 记录。同 id 多条时保留第一条（文件里 http 版在前），
        # 详情接口另有 https 优先的消歧逻辑。
        self.by_id = {}
        for rec in records:
            self.by_id.setdefault(rec.get("id"), rec)

    # ---------- 加载 ----------

    @classmethod
    def load(cls, path):
        """读 JSONL。**坏行不抛异常、不中断加载** —— 跳过并记进 `load_meta`。

        为什么不像"文件不存在"那样 fail loud：两种输入的后果完全不同。
        文件不存在 = 配错了路径，服务做什么都是错的，**必须打挂**（`FileNotFoundError`）；
        一行坏 = 数据管道某天写坏了，剩下 195 条仍然是对的、仍然该能看，
        **拒绝启动反而把一次数据事故升级成一次服务事故**。

        但"能读"不等于"没事"：坏行会进 `load_meta["bad_lines"]`，
        由 `load_health()` 变成机器可判断的三态，再由门禁
        `加载自检_坏行必须让门禁变红` 变红。**"能用，但要响"** 是这里的取舍，
        别改成"1 行坏了就不启动"。
        """
        records = []
        bad_lines = []
        if not os.path.exists(path):
            raise FileNotFoundError(f"找不到数据源: {path}")

        with open(path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as e:
                    bad_lines.append({"line": lineno, "error": f"不是合法 JSON：{e}",
                                      "excerpt": raw[:120]})
                    continue
                if not isinstance(obj, dict):
                    # 合法 JSON 但不是对象（`[]` / `"x"` / `123`）也算坏行 ——
                    # 它们进不了后面的 `rec.get(...)`，而"缺少 id 字段"这个说法
                    # 会把人带去查字段，实际是整行形状就不对。
                    bad_lines.append({"line": lineno,
                                      "error": f"顶层不是 JSON 对象（是 {type(obj).__name__}）",
                                      "excerpt": raw[:120]})
                    continue
                if not obj.get("id"):
                    bad_lines.append({"line": lineno, "error": "缺少 id 字段（或 id 为空）",
                                      "excerpt": raw[:120]})
                    continue
                records.append(obj)

        ids = [r["id"] for r in records]
        unique_ids = set(ids)
        dup_ids = sorted({i for i in ids if ids.count(i) > 1}) if len(unique_ids) != len(ids) else []

        meta = {
            "path": os.path.abspath(path),
            "lines": len(records) + len(bad_lines),
            "loaded": len(records),
            "skipped": len(bad_lines),
            "bad_lines": bad_lines,
            "unique_ids": len(unique_ids),
            # 注意：total 用的是 loaded（74），不是 unique_ids（66）。见模块 docstring。
            "duplicate_ids": dup_ids,
        }
        return cls(path, records, meta)

    # ---------- 查询 ----------

    def list(self, school=None, unit=None, apply_mode=None, level=None, year=None,
             q=None, sort="publish_desc", page=1, size=50, include_body=False,
             include_junk=False, age_tier=None, notice_kind=None,
             topic=None, offset=None, deadline_within=None, include_expired=False):
        """筛选 -> 排序 -> 分页。返回 (total, items)，total 是筛选后的总数。

        `include_junk=False`（默认）时**排除 `record_quality=="junk"`** 的记录
        （契约 §19.2）。`total` 也是排除后的数 —— 前端拿它算页数，
        两个数不一致会让"最后一页是空的"。

        **只排除 `junk`，不排除 `thin`**：`thin` 是"正文 100~300 字"，
        多半是"见附件"型公告，附件里才是正文 —— 那不是脏，是形态。

        `age_tier`（A24）：`live` / `recent` / `archive`，逗号分隔可多选。
        和 `unit` 一样是"命中任一即可"——`?age_tier=live,recent` 是前端默认口径。
        **字段名不是 `tier`**：那个名字是推送层的匹配档（1/2），别混。
        """
        units = [u.strip() for u in (unit or "").split(",") if u.strip()]
        modes = [m.strip() for m in (apply_mode or "").split(",") if m.strip()]
        levels = [l.strip() for l in (level or "").split(",") if l.strip()]
        tiers = [t.strip() for t in (age_tier or "").split(",") if t.strip()]
        kinds = [k.strip() for k in (notice_kind or "").split(",") if k.strip()]
        topics = [t.strip() for t in (topic or "").split(",") if t.strip()]

        out = []
        for rec in self.records:
            if not include_junk and quality.is_junk(rec):
                continue
            if school and not _hit(school, rec.get("school")):
                continue
            if units and not _any_hit(units, rec.get("unit")):
                continue
            if modes:
                mode = rec.get("apply_mode")
                # 抽不到招生方式的记录不因筛选被丢掉 —— 44/78 是 null，
                # 丢掉它们等于把"没抽到"伪装成"不符合"。和 matching-spec 同一原则。
                if mode is not None and mode not in modes:
                    continue
            if levels and not _any_hit(levels, " ".join(rec.get("degree_types") or [])):
                continue
            if year is not None and not _year_hit(rec, year):
                continue
            if tiers and (rec.get("age_tier") or "archive") not in tiers:
                continue
            # `notice_kind`（A29，B24）：逗号分隔可多选，和 `unit`/`age_tier` 一样
            # "命中任一即可"。**老记录没有这个字段**（A29 之前的产出）→ 不命中。
            # 筛"招生通知"时把它算进来就是编造；它自己会出现在 `?meta=1` 的
            # `unknown_fields` 一类说明里（数据层口径问题，见 issues/012 之后的记录）。
            if kinds and (rec.get("notice_kind") or "") not in kinds:
                continue
            # 主题三档（B25）：`apply` / `non_apply` / `other`，逗号多选。
            # 和 notice_kind 一样，**老记录没有这个字段 → 不命中**（不猜）。
            if topics and (rec.get("topic") or "") not in topics:
                continue
            # 「N 天内截止」（B25）：**只认 found** —— absent / unknown 不能算进
            # "7 天内截止"，那是替公告编一个截止日（三态那条不变量）。
            # 已截止（days < 0）也**不算**：用户问的是"还来得及的"。
            if deadline_within is not None:
                if credibility.state_of(rec, "deadline") != credibility.FOUND:
                    continue
                dl = _parse_date(rec.get("deadline"))
                if dl is None:
                    continue
                left = (dl - date.today()).days
                if left < 0 or left > int(deadline_within):
                    continue
            if q and not self._fulltext_hit(rec, q):
                continue
            out.append(rec)

        # `sort=deadline_asc` 默认**只排未来截止的**（B25 反馈）：
        # 已过期的不进列表 —— 用户要看的是"还来不及来得及"，
        # 不是"2010 年什么时候截止的"。想看它们：`include_expired=true`
        # 或 `sort=deadline_desc`（后者就是"过期优先"）。
        if sort == "deadline_asc" and not include_expired:
            today_ = date.today()
            out = [r for r in out
                   if not ((_deadline_found_date(r) or date.max) < today_)]

        key = SORT_KEYS.get(sort, _sort_key_publish_desc)
        reverse = SORT_DIRECTIONS.get(sort, True)     # 默认 publish_desc = 最新在前
        out.sort(key=key, reverse=reverse)

        total = len(out)
        # `offset` 优先于 `page`（B25）：两个都是"从第几条开始"，
        # 同时给时以更直接的 offset 为准；响应里的 `page` 由调用方算好再回显。
        start = int(offset) if offset is not None else (page - 1) * size
        page_items = out[start:start + size]
        items = [self.project(r, include_body=include_body) for r in page_items]
        return total, items

    @staticmethod
    def _fulltext_hit(rec, q):
        """全文搜索。搜标题/摘要/单位/专业/标签/栏目 + 正文。

        正文单独处理：`q=a` 这种单字母会命中所有含 a 的英文正文，噪音极大；
        先只在元数据里找，找不到再退到正文。这样"找某篇公告"仍然好用，
        又不会被正文淹没。要不要把正文命中拆成独立参数，等 C 有反馈再定。
        """
        meta_hay = " ".join(str(rec.get(k) or "") for k in
                            ("title", "school", "unit", "board", "source"))
        meta_hay += " " + " ".join(rec.get("majors") or [])
        meta_hay += " " + " ".join(rec.get("tags") or [])
        meta_hay += " " + " ".join(rec.get("materials") or [])
        if _hit(q, meta_hay):
            return True
        return _hit(q, rec.get("body_text") or "")

    def project(self, rec, include_body=False):
        """按 `LIST_FIELDS` 投影出列表项，再补两个派生字段。

        `include_body=True` 时返回完整记录（给需要正文的场景，如导出）。

        这里刻意用"白名单"而不是"黑名单"（以前是"去掉 body_text、其余全给"）：
        黑名单会让 A 往 JSONL 加字段时，列表响应**悄悄多出字段**，
        和 fixture 的逐字段比对就红了，而那时没人知道是谁加的。

        `has_attachments` / `attachments_count` 是**派生**的，见模块 docstring。
        顺序必须是"先 LIST_FIELDS 再这两个"，fixture 的键顺序就是这样，
        而我的测试逐键比对了顺序。
        """
        if include_body:
            return rec
        d = {k: rec.get(k) for k in LIST_FIELDS}
        # 质量/时效四个字段**统一走 `quality.of()` 规范化**（契约 §19）：
        # 数据里有就透传，没有就补 `unknown` / `null`。
        # 不规范化的话老记录会给出 `record_quality: null`，而"没标过"
        # 和"标成了空"在前端是两种渲染，多一个 null 分支就多一个写错的地方。
        d.update(quality.of(rec))
        atts = rec.get("attachments") or []
        d["has_attachments"] = bool(atts)
        d["attachments_count"] = len(atts)
        return d

    def _id_index(self):
        """`id -> [记录...]` 的懒建索引。**这是性能修复，不是重构。**

        2026-10-18 实测：`/api/push/preview` 要 0.5~3 秒、`/api/library` 1.6 秒，
        而 `assemble` 本身只要 80ms。瓶颈在这里 —— `get()` 原来是

            [r for r in self.records if r.get("id") == notice_id]

        对**每一条事件**做一次全表扫描：2759 条事件 × 2751 条公告 ≈ **760 万次比较**。
        数据到 2751 条之后它从"看不出问题"变成"演示第一屏白屏"。

        索引只建一次、之后 O(1)。`records` 在 `load()` 之后不再变（LiveStore 重载会
        换一个新实例，索引跟着重建），所以不需要失效逻辑。
        """
        idx = getattr(self, "_id_cache", None)
        if idx is None:
            idx = {}
            for rec in self.records:
                idx.setdefault(rec.get("id"), []).append(rec)
            self._id_cache = idx
        return idx

    def get(self, notice_id):
        """按 id 取完整记录。同 id 多条时优先 https —— 见模块 docstring。"""
        same = self._id_index().get(notice_id)
        if not same:
            return None
        for rec in same:
            if str(rec.get("url", "")).startswith("https://"):
                return rec
        return same[0]

    def variants(self, notice_id):
        """同一 id 的所有版本（http/https）。数量 >1 说明这条有重复抓取。"""
        return list(self._id_index().get(notice_id) or [])

    # ---------- 元信息给前端 ----------

    def facets(self):
        """给前端的筛选项清单。硬编码列表会让 C 的筛选器出现空选项
        （比如 apply_mode 里的「预推免」在真实数据里一条都没有）。"""
        schools = {}
        units = {}
        modes = {}
        years, adm_years = {}, {}
        # 「截止日」分桶（B25）。**和 `?deadline_within=` 同一套口径**：
        # `within_N` 是**累计**（0..N 天）—— within_7 的 count 正好等于
        # `deadline_within=7` 返回的 total，前端下拉写「7 天内 (57)」时
        # 不会出现"点了之后条数对不上"。
        #
        # 累计桶**不能相加**；但 `within_30 + later + expired + unknown + absent`
        # 正好等于非 junk 总数（门禁盯着这条恒等式）。
        buckets = {k: 0 for k in ("expired", "within_1", "within_3", "within_7",
                                  "within_30", "later", "unknown", "absent")}
        # **互斥**桶（B25 反馈，2026-10-19）：累计桶不能相加（within_7 含 within_3），
        # 前端要饼图 / 要"加起来等于总数"时用这一份。两边都从同一个 `left` 算，
        # 不会出现"两套口径各说各的"。
        exclusive = {k: 0 for k in ("within_1", "within_2_3", "within_4_7",
                                    "within_8_30", "later", "expired",
                                    "unknown", "absent")}
        today = date.today()
        for rec in self.records:
            # **facets 也排除 junk**（契约 §19.2 的延伸）。
            #
            # 不排除会出现一个很难发现的坏状态：某个学校的 46 条正文全是空壳
            # （上海交大就是这个形态），它的筛选选项仍然写着"上海交通大学 47 条"，
            # 用户点进去却是一片空 —— 而且"我们覆盖了 29 所学校"这个数字
            # 正是靠这些空壳撑起来的。**覆盖学校数好看，不等于内容能用。**
            if quality.is_junk(rec):
                continue
            schools[rec["school"]] = schools.get(rec["school"], 0) + 1
            units[rec.get("unit") or ""] = units.get(rec.get("unit") or "", 0) + 1
            mode = rec.get("apply_mode") or ""
            modes[mode] = modes.get(mode, 0) + 1
            y = rec.get("target_year")
            if y is not None:
                years[str(y)] = years.get(str(y), 0) + 1
            ay = rec.get("admission_year")
            if ay is not None:
                adm_years[str(ay)] = adm_years.get(str(ay), 0) + 1

            # ---- 截止日分桶（B25）----
            st = credibility.state_of(rec, "deadline")
            if st == credibility.FOUND:
                dl = _parse_date(rec.get("deadline"))
                if dl is None:
                    # 状态说 found、日期却解析不出来 —— 按"读不到"算，
                    # 不能算进任何"N 天内"（那等于替它编一个日期）。
                    buckets["unknown"] += 1
                    exclusive["unknown"] += 1
                else:
                    left = (dl - today).days
                    if left < 0:
                        buckets["expired"] += 1
                        exclusive["expired"] += 1
                    else:
                        # 累计桶（给"7 天内"这类下拉，和 deadline_within 对齐）
                        for n in config.DEADLINE_WINDOWS:
                            if left <= n:
                                buckets[f"within_{n}"] += 1
                        if left > max(config.DEADLINE_WINDOWS):
                            buckets["later"] += 1
                        # 互斥桶（给饼图 / 可相加的展示）
                        if left <= 1:
                            exclusive["within_1"] += 1
                        elif left <= 3:
                            exclusive["within_2_3"] += 1
                        elif left <= 7:
                            exclusive["within_4_7"] += 1
                        elif left <= 30:
                            exclusive["within_8_30"] += 1
                        else:
                            exclusive["later"] += 1
            else:
                key = "absent" if st == credibility.ABSENT else "unknown"
                buckets[key] += 1
                exclusive[key] += 1

        def _sorted(d):
            return [{"value": k, "count": v} for k, v in
                    sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]

        # `target_years` **保留**（C 可能已经在用），但契约 §1.5 说明它 deprecated：
        # 新代码筛年份用 `admission_years`。
        # `deadline_buckets`（B25）：给前端做「截止日」下拉，**带真实条数**，
        # 和 `?deadline_within=` 的筛选结果一一对应。
        _labels = {"expired": "已截止", "within_1": "1 天内",
                   "within_3": "3 天内", "within_7": "7 天内",
                   "within_30": "30 天内", "later": "30 天以后",
                   "unknown": "未能读取截止日", "absent": "未公布截止日"}
        deadline_buckets = [
            {"value": k, "label": _labels[k], "count": buckets[k]}
            for k in ("expired", "within_1", "within_3", "within_7",
                      "within_30", "later", "unknown", "absent")
        ]
        _ex_labels = {"within_1": "1 天内", "within_2_3": "2-3 天",
                      "within_4_7": "4-7 天", "within_8_30": "8-30 天",
                      "later": "30 天以后", "expired": "已截止",
                      "unknown": "未能读取截止日", "absent": "未公布截止日"}
        deadline_exclusive_buckets = [
            {"value": k, "label": _ex_labels[k], "count": exclusive[k]}
            for k in ("within_1", "within_2_3", "within_4_7", "within_8_30",
                      "later", "expired", "unknown", "absent")
        ]
        return {"schools": _sorted(schools), "units": _sorted(units),
                "apply_modes": _sorted(modes), "target_years": _sorted(years),
                "admission_years": _sorted(adm_years),
                "deadline_buckets": deadline_buckets,
                "deadline_exclusive_buckets": deadline_exclusive_buckets}

    def warnings_of(self, rec):
        """单条记录的数据质量提醒（现算，不是数据自带）。

        注意：**JSONL 里没有 `warnings` 字段，schema 也没有**
        （`additionalProperties: false`，0/78 条带这个键）。
        B-KICKOFF 第 59 行「把 warnings 数组透传」和 matching-spec 里把
        `warnings` 写在**匹配结果**里，两处指的是不同东西 —— 前者是文档笔误。
        A 已在 CHANGELOG 里把口径改成"响应层给全"。

        所以列表接口默认**不带** `warnings`（会和 fixture 的形状对不上），
        而是提供 `?meta=1` 在响应层现算。已记 issues/004。
        """
        warnings = []
        if credibility.state_of(rec, "deadline") != credibility.FOUND:
            # **措辞按三态分**（契约第 14 节）：`absent` 是有依据的确定回答
            # （"该公告未公布截止日"），`unknown` 才是"我们没读到"。
            # 混成一句话就等于把不知道说成知道 —— 那正是这一节要消灭的。
            warnings.append(credibility.deadline_display(rec)["text"])
        conf = (rec.get("extract") or {}).get("confidence")
        conf = 0.5 if conf is None else float(conf)
        if conf < 0.5:
            warnings.append(f"该公告字段抽取置信度低（{conf:.2f}），请核对原文")
        for att in rec.get("attachments") or []:
            if att.get("note"):
                warnings.append(f"附件「{att.get('name')}」未能抽取正文：{att['note']}")
        return warnings

    def extract_meta(self, rec):
        conf = (rec.get("extract") or {}).get("confidence")
        conf = 0.5 if conf is None else float(conf)
        atts = rec.get("attachments") or []
        return {
            "confidence": conf,
            "suspected": conf < 0.5,          # 前端标"疑似"的判据，契约写死 0.5
            # **`deadline_missing` 保留但语义要读准**：它只说明"值不在"，
            # **不等于"没有截止日"** —— 值不在可能是 absent 也可能是 unknown。
            # 要区分请看 `field_state.deadline`（契约第 14 节）。
            # 保留它是为了不破坏已有调用方（fixture 里没有这个键，但 C 可能在用）。
            "deadline_missing": not rec.get("deadline"),
            "deadline_state": credibility.state_of(rec, "deadline"),
            "field_state": credibility.field_state_of(rec, normalize=False),
            "field_trust": credibility.untrusted_summary(rec),
            "attachments_total": len(atts),
            "attachments_unparsed": sum(1 for a in atts if a.get("note")),
        }

    def data_state(self, matched):
        """`total == 0` 到底是"没数据"还是"筛得太窄"——**这两句话对用户完全不同**。

        ## 为什么必须分开（环 5）

        `{"total": 0, "items": []}` 有两种完全不同的成因：

        | 成因 | 用户该看到什么 | 该做什么 |
        |---|---|---|
        | 数据源是空的（管道没跑/跑空了） | 「数据还没加载」 | 等 / 找我们 |
        | 数据有 196 条，但你的条件没命中 | 「没有符合条件的公告」 | 放宽筛选 |

        原来接口**长得一模一样**：都是 200 + `total: 0`。调用方（包括前端和
        我自己的冒烟脚本）一定会把第一种说成第二种 —— 这正是本项目反复栽的
        「把'被拒绝/没加载'说成'没有数据'」（§7.4）。§11 用 `empty_reason`、
        §12 用三态 `status` 分开过同样的东西，这里是第三处，口径照抄。

        **只在 `?meta=1` 里给**：默认响应形状被 fixture 钉死，一个多余键都不能加。
        """
        loaded = int(self.load_meta.get("loaded") or 0)
        skipped = int(self.load_meta.get("skipped") or 0)
        if matched:
            reason = None
            note = f"共 {loaded} 条公告，本次命中 {matched} 条"
        elif loaded == 0:
            reason = "source_empty"
            note = ("数据源里一条公告都没有（管道还没跑完/跑空了）—— "
                    "这不是‘你的筛选太窄’，是现在没有任何数据可展示")
        else:
            reason = "no_match"
            note = (f"数据源有 {loaded} 条公告，但没有一条符合当前筛选条件 —— "
                    f"放宽筛选试试")
        if skipped:
            # 少数据这件事也要在这条链路上说一遍：列表页是用户唯一必看的地方
            note += f"（另有 {skipped} 行数据损坏已被跳过，见 /api/health 的 load）"
        return {
            "loaded": loaded,
            "matched": matched,
            "skipped_lines": skipped,
            "source_empty": loaded == 0,
            "empty_reason": reason,
            "note": note,
        }

    def list_response(self, **kw):
        """组装列表响应。

        默认形状 = `contracts/fixtures/notices.list.json`，**一个多余字段都不加** ——
        B-KICKOFF 要求"和 fixtures 完全一样形状"，加了 `warnings` 这类新键，
        C 的比对和 fixture 回归就全废了。

        `with_meta=True` 时每条额外挂 `warnings` / `extract_meta` / `field_trust`
        （响应层包装，不动数据契约），**顶层再挂一个 `data_state`**：
        `total == 0` 时用它分清"没数据"和"筛得太窄"（见 `data_state()`）。
        需要"未抽到截止日期""置信度低""附件需人工查看"这些提示就带 `?meta=1`：
        JSONL 里没有 `warnings` 字段、schema 也没有（`additionalProperties: false`），
        而列表默认形状又必须和 fixture 一致，所以只能现算 + 显式开关。已记 issues/004。
        """
        include_body = kw.pop("include_body", False)
        with_meta = kw.pop("with_meta", False)
        if with_meta:
            # **必须拿完整记录来算 meta**：`warnings_of` / `extract_meta` /
            # `field_trust` 都要看数据里那些**不在 `LIST_FIELDS` 里**的字段 ——
            # 尤其是 `field_state`。
            #
            # 这里原来是个真 bug：先 `self.list()`（已经投影过了）再算 meta，
            # 而 `field_state` 不在投影里 → 每条的三态都算成 `unknown`，
            # **连数据层明确说的 `absent` 也丢了**，用户看到的提示全是"未能读取"。
            # 是门禁里那条"unknown 不许被当成 absent"的用例把它揪出来的：
            # 它从 `field_trust.states` 读，读出来五条全是 unknown。
            total, full_items = self.list(include_body=True, **kw)
            items = []
            for rec in full_items:
                row = self.project(rec)      # 列表项仍然严格等于 LIST_FIELDS
                row["warnings"] = self.warnings_of(rec)
                row["extract_meta"] = self.extract_meta(rec)
                # 列表给**摘要**（详情给完整 field_state，契约第 14 节）——
                # "这条有几个字段不可信"要能一眼算出来，不用逐条拉详情。
                row["field_trust"] = credibility.untrusted_summary(rec)
                items.append(row)
            return {
                "total": total,
                "page": kw.get("page", 1),
                "size": kw.get("size", 50),
                "items": items,
                # 顶层：`total == 0` 时"没数据"和"筛太窄"必须分得开（环 5）
                "data_state": self.data_state(total),
            }
        total, items = self.list(include_body=include_body, **kw)
        return {
            "total": total,
            "page": kw.get("page", 1),
            "size": kw.get("size", 50),
            "items": items,
        }


def group_kind_of(name, rec=None):
    """栏目名的性质：身份 / 机构 / 其他。

    **判据顺序有讲究**（照抄 A 的参考实现 `collector/faculty.py`）：
    「中国科学院院士」里同时含「学院」，先判身份才不会被机构抢走。
    数据里有 `group_kind` 就直接用它（A 已产出），没有才现算 ——
    这样老数据也能用，而且不会和 A 的判定悄悄分叉。
    """
    if rec is not None and rec.get("group_kind"):
        return rec["group_kind"]
    for kind, words in config.GROUP_KINDS:
        if any(w in (name or "") for w in words):
            return kind
    return config.GROUP_KIND_FALLBACK


class FacultyStore:
    """导师数据访问层。数据源 `data/out/faculty.jsonl`（契约 `faculty.schema.json`）。

    这一层为什么单独一个类：**它不是 notice 的扩展**（A 在 CHANGELOG 里说清了）——
    公告天天变、导师信息几个月才动一次，两者的失效模式完全不同。
    所以字段表、接口、文件全部分开，不共用投影。
    """

    def __init__(self, path, records):
        self.path = path
        self.records = records

    @classmethod
    def load(cls, path):
        if not os.path.exists(path):
            # 导师库是可选数据源：没有它，公告那一整套照常工作。
            return cls(path, [])
        with open(path, "r", encoding="utf-8") as f:
            records = [json.loads(ln) for ln in f if ln.strip()]
        return cls(path, records)

    def list(self, school=None, unit=None, group=None, group_kind=None, q=None,
             matchable=None, page=1, size=50):
        """筛导师。`q` 在**姓名和研究方向**里搜（契约第 8 节）。

        `group_kind` 可按「身份 / 机构 / 其他」过滤 —— 前端要"只看研究所"时靠它，
        否则「中国科学院院士」和「凝聚态物理研究所」混在一起没法分。

        `matchable`（2026-10-17 新增）：`true` 只看可匹配的、`false` 只看名录级、
        `None` 全给。**筛的是"能不能进匹配池"，不是"质量好坏"** ——
        没研究方向的导师不是差导师，只是我们没有判断依据。
        """
        units = [u.strip() for u in (unit or "").split(",") if u.strip()]
        groups = [g.strip() for g in (group or "").split(",") if g.strip()]
        kinds = [k.strip() for k in (group_kind or "").split(",") if k.strip()]

        out = []
        for rec in self.records:
            if school and not _hit(school, rec.get("school")):
                continue
            if units and not _any_hit(units, rec.get("unit")):
                continue
            if groups and not _any_hit(groups, rec.get("group")):
                continue
            if kinds and group_kind_of(rec.get("group"), rec) not in kinds:
                continue
            if matchable is not None:
                if bool(rec.get("research_areas")) != matchable:
                    continue
            if q:
                hay = (rec.get("name") or "") + " " + " ".join(rec.get("research_areas") or [])
                if not _hit(q, hay):
                    continue
            out.append(rec)

        # **不排序，保持文件原始顺序** —— 这一条是从 fixture 反推出来的：
        # `collector/tools/make_fixtures.py:357-359` 直接投影后取前 50，**没有任何 sort**，
        # 所以 `faculty.list.json` 的顺序就是文件的顺序。
        #
        # 我原来在这里按 `(无研究方向沉底, group, name)` 排，和 fixture 对不上。
        # 而分组的责任其实在 `group_kind` 上 —— 前端拿它分组/筛"只看研究所"，
        # 列表顺序不需要再承担这件事；文件里 `group` 本来就是成块的，
        # 且**无研究方向的导师在文件里已经沉底**（当前 67 人里 0 条），
        # 契约那条"不要排到前面"仍然成立。
        total = len(out)
        start = (page - 1) * size
        items = [self.project(r) for r in out[start:start + size]]
        return total, items

    @staticmethod
    def project(rec):
        """按 `FACULTY_LIST_FIELDS` 投影，并保证 `group_kind` 一定有值。

        `group_kind` 在 `faculty.schema.json` 里是 **required**，所以不能是 None：
        数据里有就透传，没有就现算（判据顺序见 `group_kind_of`）。

        `matchable` 是**派生字段**（和公告列表的 `has_attachments` 同一个性质）：
        JSONL 里没有这个键，这里现算。
        """
        d = {k: rec.get(k) for k in config.FACULTY_LIST_FIELDS}
        d["group_kind"] = group_kind_of(rec.get("group"), rec)
        # **能不能进匹配池**：有研究方向才可匹配。
        #
        # 为什么要这个布尔（2026-10-17）：557 位导师里 **178 位没有研究方向**，
        # 而它们全都在 `match_faculty` 里出结果 —— 命中 0 个兴趣词、只拿到
        # 0.15×单位 + 0.15×层次 的分，排在最后但**仍然出现**，还带着
        # "无法判断契合度"的警告。后果是"匹配结果"看起来比实际多，
        # 而且用户会以为自己的匹配结果里混着几条坏的。
        #
        # 分成两层的意义不是"少返回几条"，是**让"557 位导师"这个数能被拆开说**：
        # 「379 位可匹配 + 178 位是名录级」。虚报覆盖比少报严重得多。
        d["matchable"] = bool(rec.get("research_areas"))
        return d

    def list_response(self, **kw):
        total, items = self.list(**kw)
        return {"total": total, "page": kw.get("page", 1),
                "size": kw.get("size", 50), "items": items}

    def facets(self):
        """筛选项。`group`（研究所/身份）和 `group_kind` 是导师页的主要导航维度。"""
        def _counts(key=None, derive=None):
            d = {}
            for r in self.records:
                v = (derive(r) if derive else r.get(key)) or ""
                d[v] = d.get(v, 0) + 1
            return [{"value": k, "count": v} for k, v in
                    sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]

        return {
            "total": len(self.records),
            "schools": _counts("school"),
            "units": _counts("unit"),
            "groups": _counts(None, derive=lambda r: r.get("group") or ""),
            "group_kinds": _counts(None, derive=lambda r: group_kind_of(r.get("group"), r)),
            # 契约硬要求：邮箱缺失要能被前端发现（实测 50 人里 1 人没抽到）
            "missing_email": sum(1 for r in self.records if not r.get("email")),
            "missing_research_areas": sum(1 for r in self.records
                                          if not (r.get("research_areas") or [])),
        }

    def unit_suggestions(self):
        """A 产出的 `data/out/units.json`：院系建议值（含公告/导师计数）。

        给论坛的 `/api/posts/facets` 当**初始种** —— 论坛一开始没人发帖，
        光靠已有取值做下拉建议会是空的，用户就会自由发挥把「物理学院」
        和「物理系」敲成两个版块。
        """
        path = config.UNITS_PATH
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f).get("units") or []
        except (json.JSONDecodeError, OSError):
            return []      # 建议值读不到不该让接口挂掉

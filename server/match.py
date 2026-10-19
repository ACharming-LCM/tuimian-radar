# -*- coding: utf-8 -*-
"""匹配打分（公告 + 导师）。规则契约：`docs/matching-spec.md`。

**这是 `collector/tools/match_preview.py` 的生产移植，不是重新设计。**
规则一个字都没改 —— A 的参考实现就是基线，改规则要走 `contracts/CHANGELOG.md`。
移植时逐字段对过：用同一份画像和数据跑参考实现，
`stats` 与 30 条 item 的 13 个同名字段**零差异**。

为什么值得原样移植而不"顺手优化"：
这套公式的每条规则背后都有一个踩过的坑，注释里都留着 ——
"缺失给 0.4 不给 0"（否则抽取缺陷会伪装成匹配结论）、
"标题与抽取矛盾时不许参与排序裁判"（东南大学硕士章程被抽成博士，照样排第 2）、
"材料提醒只留硬门槛词"（v1 把论文/科研成果也放进去，结果每条都刷提醒，真提醒被淹没）。
优化公式之前，先读那些注释。

与参考实现的差异（都是"接进 server"必需的，不是规则改动）：
1. 多了 `extract` / `has_attachments` / `attachments_count` 三个字段（api.md 第 2 节要求）。
2. 去掉 `parts` 这种调试字段，响应里不给。
3. `__import__` 那些参考实现的小把戏换成正常 import。
4. **B18 方案 A（2026-10-19 用户拍板）**：`config.MATCH_SCORE_FROM_PUSH` 为真时，
   分数改用 `push.score_dims` 的四维加权，和推送同一个口径。
   ⚠️ **默认关着** —— A 的参考实现还没改，切过去门禁会红 7 条（清单见 issues/012）。
   开关关着时行为与移植版完全一致。
"""
from __future__ import annotations

import re

from datetime import date, datetime

from server import config, credibility
from server import push as push_mod
from server import quality

# 权重之和必须等于 1。改权重前先想清楚"缺字段给 0.4"这条还成不成立。
WEIGHTS = {
    "major": 0.30,
    "mode": 0.20,
    "level": 0.15,
    "affinity": 0.15,
    "year": 0.10,
    "freshness": 0.10,
}
MISSING = 0.4          # 字段缺失的得分：降权，但不判定为"不相关"
CONF_FLOOR = 0.7       # 置信度降权的下限系数

# 材料缺口检查：只对真正的"硬门槛"词提醒。
# 坑（已踩）：v1 把 论文/科研成果/专家推荐 也放进来，结果几乎每条公告都刷一遍提醒
# （因为 materials 字段本身就是全文关键词扫描的产物，精度不够），真提醒被淹没。
# 这里只保留"没有就很可能不符合条件"的几个，并放软语气。
# 另外：四级由六级推导，不单独提醒 —— 六级 580 的人被提示"要求四级"是纯噪声。
MATERIAL_SIGNALS = {
    "六级": ("cet6", "六级"),
    "托福": ("toefl", "托福"),
    "雅思": ("ielts", "雅思"),
    "推荐信": ("recommenders", "推荐信"),
}

# 契约里的 bucket 顺序。C 的工具条按这个顺序排芯片。
BUCKET_ORDER = ["立即行动", "重点关注", "可规划", "需人工确认", "已过期"]


def _hit(a, b):
    """互为子串即算命中：'物理学' vs '物理学类' 要能匹配上。"""
    return bool(a) and bool(b) and (a in b or b in a)


def parse_date(s):
    """'2026-10-16' -> date；解析不出来返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ---------- 公告匹配 ----------

def _english_dict(v):
    """english 允许**两种形状**：字典（标准）或字符串（人写的 "CET-6 580"）。

    ## 为什么必须兼容字符串

    契约 pi.md §2 自己的示例 payload 写的就是 "english": "CET-6 580"，
    而实现原来是 eng = prof.get("english") or {} —— 传字符串时 eng 就是那个字符串，
    后面 eng.get(...) 直接 **AttributeError → /api/match 500**。
    **契约给的示例能打崩自己的接口**，这是最难看的一类 bug：照着文档写就挂。

    解析不出来的（"英语很好"这种）返回空字典 —— **不猜**。空字典的后果只是
    "没有英语相关信息"，和"用户没填"等价，比编一个 580 出来安全。
    """
    if isinstance(v, dict):
        return v
    if not isinstance(v, str):
        return {}
    out = {}
    m = re.search(r"(?:CET\s*-?\s*([46])|([四六])级)[^0-9]{0,8}(\d{2,3})", v, re.I)
    if m:
        out["cet6" if (m.group(1) == "6" or m.group(2) == "六") else "cet4"] = int(m.group(3))
    m = re.search(r"(?:TOEFL|托福)[^0-9]{0,8}(\d{2,3})", v, re.I)
    if m:
        out["toefl"] = int(m.group(1))
    m = re.search(r"(?:IELTS|雅思)[^0-9]{0,8}(\d(?:\.\d)?)", v, re.I)
    if m:
        out["ielts"] = float(m.group(1))
    return out


def event_view(rec):
    """公告记录 → `push.score_dims` 认识的事件形状（B18 方案 A）。

    **只借它认识的四个字段。** 公告没有 `matched[]`（那是事件流的专题词），
    这里用公告自己的 `majors` 顶上去：方向词命中公告的专业列表，
    和命中标题一样算对口 —— 对"这条公告讲什么方向"这件事，两者是同一个信号。
    """
    return {
        "title": rec.get("title"),
        "school": rec.get("school"),
        "unit": rec.get("unit"),
        "matched": list(rec.get("majors") or []),
    }


def score_notice(rec, prof, today):
    """返回 (final_score, raw_score, reasons, warnings)。

    公式见 matching-spec 第三节；降权见第五节。
    """
    n_majors = rec.get("majors") or []
    p_majors = prof.get("majors") or []

    # ---- major ----
    if not n_majors:
        major, major_note = MISSING, None
    elif any(_hit(m, p) for m in n_majors for p in p_majors):
        hits = [m for m in n_majors for p in p_majors if _hit(m, p)]
        major, major_note = 1.0, f"专业对口：{'、'.join(dict.fromkeys(hits))}"
    else:
        major, major_note = 0.0, f"专业不符（公告是 {'、'.join(n_majors[:3])}）"

    # ---- mode ----
    mode = rec.get("apply_mode")
    wanted = prof.get("wanted_modes") or []
    if mode and mode in wanted:
        mode_s, mode_note = 1.0, mode
    elif not mode:
        mode_s, mode_note = MISSING, None
    else:
        mode_s, mode_note = 0.0, None

    # ---- level ----
    levels = rec.get("degree_types") or []
    targets = prof.get("target_levels") or []
    title = rec.get("title") or ""
    contradiction = None
    mismatch = levels and targets and not any(_hit(l, t) for l in levels for t in targets)
    if mismatch:
        # 坑（已踩）：抽取与标题自相矛盾时，"不匹配"这个判断本身不可信。
        # 真实案例：东南大学2027年**硕士**研究生招生章程被抽出 degree_types=["博士"]，
        # 打分照样给出 0.88 排到第 2 —— 抽取错误就这么悄悄污染了排序。
        # 处理原则：**已知不可信的字段不许参与排序裁判**，降为"缺失"并明确报警。
        if ("硕士" in title and "博士" not in title and "博士" in levels) or \
           ("博士" in title and "硕士" not in title and "硕士" in levels):
            t_word = "硕士" if "硕士" in title else "博士"
            contradiction = (f"标题写「{t_word}」但抽取到「{'、'.join(levels)}」，"
                             f"层次判断可能有误，请核对原文")
            level, level_note = MISSING, None
        else:
            level, level_note = 0.0, f"层次不符（公告是 {'、'.join(levels)}）"
    elif not levels:
        level, level_note = MISSING, None
    else:
        level, level_note = 1.0, "、".join(levels)

    # ---- affinity（学校 / 单位）----
    units = prof.get("preferred_units") or []
    schools = prof.get("preferred_schools") or []
    if any(_hit(rec.get("unit", ""), u) for u in units):
        aff, aff_note = 1.0, f"目标单位：{rec.get('unit')}"
    elif any(_hit(rec.get("school", ""), s) for s in schools):
        aff, aff_note = 0.7, f"目标学校：{rec.get('school')}"
    else:
        aff, aff_note = 0.2, None

    # ---- year ----
    y, want_y = rec.get("target_year"), prof.get("target_entry_year")
    if y is None:
        year_s, year_note = 0.3, None
    elif want_y is None or y == want_y:
        year_s, year_note = 1.0, f"{y} 年招生（对应你这一届）"
    elif y > want_y:
        year_s, year_note = 0.6, f"{y} 年招生（比你晚一届，可作参考）"
    else:
        year_s, year_note = 0.0, f"{y} 年招生（已过届）"

    # ---- freshness ----
    d0 = parse_date(rec.get("publish_date"))
    if d0:
        age = (today - d0).days
        fresh = 1.0 if age <= 30 else 0.7 if age <= 90 else 0.4 if age <= 365 else 0.1
    else:
        fresh = 0.2

    parts = {"major": major, "mode": mode_s, "level": level,
             "affinity": aff, "year": year_s, "freshness": fresh}
    raw = sum(parts[k] * WEIGHTS[k] for k in WEIGHTS)

    # ---- 口径来源（B18 方案 A，2026-10-19 用户拍板）----
    #
    # 默认仍是六维（参考实现钉住的那套）。切到 `push` 时改用推送的**四维** ——
    # 同一个画像，`/api/match` 和 `/api/push/preview` 才会给出同一批答案。
    #
    # **`level` / `year` 不进四维分**：推送那条路径看不到公告的 `degree_types` /
    # `target_year`（事件流里没有这两个字段）。一边算、一边不算，就不叫"同一套口径"。
    # 它们仍然留在 `reasons` / `warnings` 里说明"这条是不是你那届、层次对不对"，
    # 只是不再偷偷改动分数（**改分数和只解释，是两件事**）。
    if config.MATCH_SCORE_FROM_PUSH:
        dims, _detail = push_mod.score_dims(event_view(rec), prof, today)
        raw = push_mod.weighted_score(dims)

    # reasons：只收正向说明（0.4 的"缺失"没有 note，自然不会进来）
    reasons = [n for n in (major_note, mode_note, level_note, aff_note, year_note) if n]
    warnings = []

    # ---- 置信度降权 ----
    conf = (rec.get("extract") or {}).get("confidence")
    conf = 0.5 if conf is None else float(conf)
    final = raw * (CONF_FLOOR + (1 - CONF_FLOOR) * conf)

    # ---- 必配的提醒 ----
    if not rec.get("deadline"):
        # **措辞保持和 A 的参考实现逐字一致**（`match_preview.py`）——
        # `match_与参考实现逐字段一致` 那条门禁会逐字段比对，
        # 而 `match.response.json` 也钉住了形状。
        #
        # 这句本身**没有犯三态那个错**：它说的是"未抽到…务必点开原文确认"
        # （倾向于谨慎，不声称"没有"），对 `unknown` 是正确的。
        # 对 `absent` 偏保守（我们其实读到了、确实没有），但**不构成错误信息**。
        # 三态的精确定义在 B 自己的面上给：`?meta=1` 的 `deadline_display`、
        # `field_state`、`/api/quality`。**改这句要和 A 对一下参考实现。**
        warnings.append("未抽到截止日期，务必点开原文确认")
    if conf < 0.5:
        warnings.append(f"该公告字段抽取置信度低（{conf:.2f}），请核对原文")
    if contradiction:
        warnings.append(contradiction)

    mats = rec.get("materials") or []
    eng = _english_dict(prof.get("english"))
    have = {"cet6": eng.get("cet6"), "toefl": eng.get("toefl"), "ielts": eng.get("ielts"),
            "recommenders": prof.get("recommenders")}
    for word, (key, label) in MATERIAL_SIGNALS.items():
        if word in mats and not have.get(key):
            warnings.append(f"公告提到「{label}」，若你没有相应材料请先确认是否影响申请")

    return round(final, 4), round(raw, 4), reasons, warnings


BUCKET_UNKNOWN = "需人工确认"
BUCKET_ABSENT = "无截止日"
# 桶的词表和顺序**由服务端给出去**（响应里带 `buckets_order`）。
# C 原来在 `match.js:295` 硬编码了一份 `order` —— 两份词汇表迟早分叉，
# 而分叉的表现是"某个桶的条目飘到列表最前面"。凡前端渲染要用的字段，
# 同一个响应里给全（这也是本项目一贯的规矩）。
BUCKET_ORDER = ["立即行动", "重点关注", "可规划",
                BUCKET_UNKNOWN, BUCKET_ABSENT, "已过期"]


def bucket_of(deadline, today, state=None):
    """按截止日分桶。**三态分桶，`unknown` 不许被当成 `absent`**（契约第 14 节）。

    - `unknown`（我们没读到）→ `需人工确认`：用户必须去看原文；
    - `absent`（正文读到了、确实没有）→ `无截止日`：这是一个**确定回答**，
      和"我们没读到"是两件事，混成一个桶就等于把不知道说成知道。

    改动前两者都是"需人工确认" —— 那时数据层也分不出来，所以不算错；
    现在能分了还不分，就是**把信息扔掉**。

    > 新增了 `无截止日` 这个桶名，**C 的 JS 里那份硬编码顺序要更新**
    > （`web/assets/match.js` 的 `order`、`common.js` 的提示表）。
    > 所以响应里同时给 `buckets_order`，让 C 可以用服务端的顺序而不是自己那份。
    """
    d = parse_date(deadline)
    if d is None:
        if state == credibility.ABSENT:
            return BUCKET_ABSENT, None
        return BUCKET_UNKNOWN, None
    left = (d - today).days
    if left < 0:
        return "已过期", left
    if left <= 7:
        return "立即行动", left
    if left <= 30:
        return "重点关注", left
    return "可规划", left


def match_notices(records, prof, today, limit=None):
    """硬过滤 → 打分 → 排序。返回 (items, stats)。

    items 的字段表 = api.md 第 2 节 + fixture，**必须一次给全**：
    `extract` / `has_attachments` / `attachments_count` 都在里面。
    C 原来为了 confidence 又拉一次列表接口，30 条只关联上 24 条 ——
    分页和筛选会让 id 对不齐。凡前端渲染要用的字段，同一个响应里给全。
    """
    dropped_expired = dropped_mode = 0
    wanted = prof.get("wanted_modes") or []
    out = []
    for rec in records:
        # **排序/分桶走三态判定**，不看 `deadline is None`（契约第 14 节）
        dl_state = credibility.state_of(rec, "deadline")
        bucket, left = bucket_of(rec.get("deadline"), today, state=dl_state)
        # 硬过滤 ① 已过期
        if prof.get("exclude_expired") and bucket == "已过期":
            dropped_expired += 1
            continue
        # 硬过滤 ② 招生方式不符（null 不丢 —— 44/78 的记录抽不到它）
        if rec.get("apply_mode") and wanted and rec["apply_mode"] not in wanted:
            dropped_mode += 1
            continue

        final, raw, reasons, warnings = score_notice(rec, prof, today)
        atts = rec.get("attachments") or []
        out.append({
            "id": rec.get("id"), "score": final, "raw_score": raw, "bucket": bucket,
            "days_left": left, "title": rec.get("title"), "school": rec.get("school"),
            "unit": rec.get("unit"), "publish_date": rec.get("publish_date"),
            "deadline": rec.get("deadline"), "url": rec.get("url"),
            # **不给 `deadline_state` / `field_state`**：
            # `match.response.json` 的形状是契约，加键会让 C 的比对和 fixture 回归全废。
            # 三态在这条响应里的出口是 **`bucket`**（`需人工确认` vs `无截止日`）、
            # 上面那个 `field_trust` 摘要，以及 B 自己的面（列表 `?meta=1` / 详情 `?meta=1` / `/api/quality`）。
            "reasons": reasons, "warnings": warnings,
            "has_attachments": bool(atts), "attachments_count": len(atts),
            "extract": rec.get("extract"),
            # **`field_trust` 是摘要，不是 `field_state`**（契约 §14.2：列表给摘要、详情给完整）。
            #
            # 2026-10-17 补（统筹派单第 4 条）：C 的匹配页要标"这条有几个字段不可信"，
            # 而它只认这个键。原来这里什么都没有 —— 于是匹配页只能靠 `warnings` 的文案
            # 去猜，猜不准就干脆不标。
            #
            # 形状和列表 `?meta=1` 里的完全一致（`untrusted_count` / `untrusted_fields` /
            # `states`），**前端一套渲染逻辑**。
            #
            # 仍然**不给 `field_state`**（那是详情的形状，逐字段的完整三态）：
            # 匹配页是"扫一眼决定点哪条"，给完整三态只会让它变重。
            "field_trust": credibility.untrusted_summary(rec),
            # ---- 质量 / 主题 / 时效（契约 §19）----
            # 和列表项**同名字、同含义**，这样 C 的卡片在两处共用一份渲染逻辑。
            # `quality.of()` 负责把"老记录没有这个字段"规范化成
            # `record_quality="unknown"` / `is_fresh=null`，而不是留一个 null
            # 让前端多写一个分支（"没标过"和"标成了空"在前端是两种渲染）。
            **quality.of(rec, today),
        })

    # 稳定排序：并列分数保持原顺序（matches 参考实现，也 matches fixture）
    out.sort(key=lambda x: -x["score"])
    stats = {"total": len(records), "dropped_expired": dropped_expired,
             "dropped_mode": dropped_mode, "scored": len(out)}
    if limit:
        out = out[:limit]
    return out, stats


# ---------- 导师匹配（matching-spec 第七节） ----------

def match_faculty(faculty, prof):
    """把导师和用户兴趣对上。这是"专业契合的实验室"这一层唯一的数据支撑。

    score = 0.70 × 兴趣覆盖率   （命中几个兴趣词 / 一共写了几个）
          + 0.15 × 单位匹配
          + 0.15 × 层次匹配（博导 / 硕导）
    `interest_keywords` 为空时退化为用 `majors` 粗匹配，**并在 warnings 里明说**
    （不许静默降级）。没有研究方向的导师带警告，且不因字段缺失排到前面。
    """
    kws = [k for k in (prof.get("interest_keywords") or []) if k]
    majors = [m for m in (prof.get("majors") or []) if m]
    targets = prof.get("target_levels") or []
    units = prof.get("preferred_units") or []
    schools = prof.get("preferred_schools") or []
    fallback = not kws
    probes = kws or majors

    out = []
    for f in faculty:
        areas = f.get("research_areas") or []
        # **名录级的不进匹配池**（2026-10-17，`matchable` 分层的实现）。
        #
        # 原来它们也出现在结果里：命中 0 个兴趣词、只拿 0.15×单位 + 0.15×层次，
        # 排在最后但**仍然返回**，还带一条"该导师页面没有研究方向，无法判断契合度"。
        # 后果有两个，而且都不是"少了几个结果"那么简单：
        #
        #   1. **匹配结果数虚高** —— 用户以为"有 40 个导师对口"，其实 18 个是没法判的；
        #   2. **警告变成噪音** —— 每一条都挂"无法判断契合度"，用户会怀疑整页数据。
        #
        # 判据就是"有没有研究方向"，**不看学校**：
        # 大连理工整体是名录级（方向 42%），但它那 97 个**有方向**的照样可匹配 ——
        # 按学校一刀切会把这 97 个能用的也扔掉。
        #
        # 跳过了多少条**不在这条响应里说**（会改形状、C 要跟着动）。
        # 它在 `/api/faculty` 的 `matchable` 字段和契约 §8 里说得清：
        # 557 位里 178 位无方向，不参与匹配。
        if not areas:
            continue
        hits = []
        for p in probes:
            for a in areas:
                if _hit(p, a) and a not in hits:
                    hits.append(a)
        if not probes:
            coverage = 0.3                      # 什么都没填，给个中性分，不排前面
        else:
            coverage = min(1.0, len(hits) / len(probes))

        if any(_hit(f.get("unit", ""), u) for u in units):
            aff, aff_note = 1.0, f"目标单位：{f.get('unit')}"
        elif any(_hit(f.get("school", ""), s) for s in schools):
            aff, aff_note = 0.7, None
        else:
            aff, aff_note = 0.2, None

        wants_doc = any(_hit(t, "博士") or _hit(t, "直博") for t in targets)
        if f.get("is_phd_supervisor") and wants_doc:
            lvl, lvl_note = 1.0, "博士生导师"
        elif f.get("is_phd_supervisor"):
            lvl, lvl_note = 0.8, "博士生导师"
        else:
            lvl, lvl_note = 0.6, None

        score = round(0.70 * coverage + 0.15 * aff + 0.15 * lvl, 4)
        reasons = []
        if hits:
            reasons.append(f"研究方向命中：{'、'.join(hits[:4])}")
        if lvl_note:
            reasons.append(lvl_note)
        if aff_note:
            reasons.append(aff_note)

        warnings = []
        if fallback:
            warnings.append("你没填 interest_keywords，这里只用专业名做了粗匹配")
        # **"该导师页面没有研究方向"这条警告删掉了**（2026-10-17）：
        # 没方向的现在**根本不进匹配池**（上面 `skipped_no_areas` 那里 return 了），
        # 所以这条永远不会触发。留着一句永不触发的文案，比没有更坏 ——
        # 下一个人会以为它还在生效，而实际上没有任何路径能走到它。
        if not f.get("email"):
            warnings.append("没抽到邮箱，联系前请到个人主页核对")

        out.append({
            "id": f.get("id"), "name": f.get("name"), "title": f.get("title"),
            "school": f.get("school"), "unit": f.get("unit"), "group": f.get("group"),
            "email": f.get("email"), "homepage": f.get("homepage"),
            "research_areas": areas, "hits": hits,
            "score": score, "reasons": reasons, "warnings": warnings,
        })
    # 并列分按姓名排：参考实现就是这样，别改（否则同一分数每次顺序不同）
    out.sort(key=lambda x: (-x["score"], x["name"] or ""))
    return out

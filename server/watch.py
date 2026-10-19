# -*- coding: utf-8 -*-
"""关注规则：存、取、匹配。契约见 `contracts/api.md` 第 11 节。

## 这一层在卖什么

`/api/changes` 回答"**这一轮**变了什么"——所有人的、同一份。
这里回答"**和我有关的**变了什么"。差别就是用户付费的理由：
没有关注，日报每天长得一样（反正是那几条），看两天就不再打开；
有了关注，每天的结果都是他没见过的，而且"错过一条"有真实代价（截止日、材料要求）。

所以这一层的正确性标准不是"能筛出东西"，而是：
**筛出来的和用户在列表页手动筛的必须是同一批** —— 差一条，他就不会再信任提醒。

## 为什么 `id` 由内容派生

`w_` + 归一化规则的 sha1 前 12 位。于是**幂等是自然结果而不是额外判断**：
同一条规则重复 POST 必然得到同一个 id，`add_watch` 一看 id 在就已经能判重。
换成自增计数器，第二次 POST 会造出两条一模一样的规则，
digest 里就会出现两遍"为什么提醒我：学校=浙江大学"。
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from server.store import any_substring_hit, substring_hit

# 公开的一条 watch 有哪些字段。白名单，加字段要显式改这里
# （和 forum.PUBLIC_POST_FIELDS 同一个约定）。
PUBLIC_WATCH_FIELDS = ("id", "user_id", "school", "unit", "keywords", "created_at")

MAX_KEYWORDS = 20
MAX_KEYWORD_LEN = 40


class WatchError(ValueError):
    """入参不合法。调用方转成 400 BAD_REQUEST。"""


def normalize_rule(school=None, unit=None, keywords=None):
    """归一化。**归一化决定了幂等**：`" 浙江大学 "` 和 `"浙江大学"` 必须是同一条规则。

    - 首尾空白去掉；空串按 `None`（`""` 和 `None` 都表示"这个字段不约束"）
    - `keywords` 去空白、丢空串、**去重、排序** —— 顺序不同不该是两条规则
    - `keywords` 接受字符串（按逗号切）或列表，因为前端传什么都可能
    """
    def _clean(v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    kws = []
    if isinstance(keywords, str):
        kws = [k.strip() for k in keywords.split(",")]
    elif isinstance(keywords, (list, tuple)):
        kws = [str(k).strip() for k in keywords]
    elif keywords is not None:
        raise WatchError("keywords 必须是数组或逗号分隔的字符串")

    kws = [k for k in kws if k]
    for k in kws:
        if len(k) > MAX_KEYWORD_LEN:
            raise WatchError(f"关键词太长（上限 {MAX_KEYWORD_LEN} 字）：{k[:20]}…")
    kws = sorted(set(kws))
    if len(kws) > MAX_KEYWORDS:
        raise WatchError(f"关键词太多（上限 {MAX_KEYWORDS} 个）")

    rule = {"school": _clean(school), "unit": _clean(unit), "keywords": kws}
    if not rule["school"] and not rule["unit"] and not rule["keywords"]:
        # 三个都不给 = 关注"所有变化"，那和 /api/changes 没区别，等于没表达意图
        raise WatchError("至少要给 school / unit / keywords 之一")
    return rule


def watch_id_of(rule):
    """确定性 id。同一个归一化规则永远得到同一个 id（这就是幂等的来源）。"""
    key = "|".join([
        (rule.get("school") or ""),
        (rule.get("unit") or ""),
        ",".join(rule.get("keywords") or []),
    ])
    return "w_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def build_watch(user_id, rule, now=None):
    return {
        "id": watch_id_of(rule),
        "user_id": user_id,
        "school": rule.get("school"),
        "unit": rule.get("unit"),
        "keywords": list(rule.get("keywords") or []),
        "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
    }


def public_view(watch):
    return {k: watch.get(k) for k in PUBLIC_WATCH_FIELDS}


def facets(watches):
    """给前端画"你关注了哪些学校/院系"的标签用的去重值清单。

    前端自己从 `watches` 里 reduce 也行，但那就多一份实现要维护，
    而且它得知道"空串按 null 处理"这条约定。
    """
    schools, units = [], []
    for w in watches:
        s = w.get("school")
        if s and s not in schools:
            schools.append(s)
        u = w.get("unit")
        if u and u not in units:
            units.append(u)
    return {"schools": schools, "units": units}


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------

def match_reasons(watch, notice):
    """这条规则为什么命中这条公告。返回人类可读的原因列表；不命中返回 `[]`。

    **语义必须和 `GET /api/notices` 的筛选完全一致**（契约第 11 节承诺的）：
    - `school` / `unit`：大小写不敏感子串匹配 —— 直接调 `store.substring_hit`，
      **不是复制一份实现**。复制会漂移，而漂移的表现是"我在列表页筛到了，
      关注了却收不到提醒"，用户会认为提醒坏了。
    - `keywords`：**任一**命中即可（OR），在**标题或正文**里找 ——
      和 `/api/notices` 的 `q` 同义（那边是先匹配标题/单位/专业/标签再退到正文，
      这里为了"不漏"直接在标题+正文里找；见下面 `_hay` 的说明）。

    三个字段是 **AND**（都给了就都要满足），每个字段内部的关键词是 OR。
    """
    reasons = []
    if watch.get("school"):
        if not substring_hit(watch["school"], notice.get("school")):
            return []
        reasons.append(f"学校={watch['school']}")
    if watch.get("unit"):
        if not substring_hit(watch["unit"], notice.get("unit")):
            return []
        reasons.append(f"院系={watch['unit']}")
    kws = watch.get("keywords") or []
    if kws:
        hay = _hay(notice)
        hit = [k for k in kws if substring_hit(k, hay)]
        if not hit:
            return []
        reasons.append("关键词=" + "、".join(hit))
    return reasons


def _hay(notice):
    """关键词的搜索面：标题 + 院系 + 专业 + 标签 + 正文。

    **比 `/api/notices?q=` 的面更宽一点，是刻意的**：那边搜是"我现在想找什么"，
    搜不到就换个词；这里是"以后出现这类东西请告诉我"，
    **漏掉的代价远大于多报的代价**（多报一条，用户扫一眼就划过；
    漏一条，他可能错过截止日）。所以宁可宽。
    """
    parts = [
        notice.get("title") or "",
        notice.get("unit") or "",
        notice.get("board") or "",
        " ".join(notice.get("majors") or []),
        " ".join(notice.get("tags") or []),
        notice.get("body_text") or "",
    ]
    return " ".join(parts)


def matched_by(watches, notice):
    """这条公告命中了哪些规则（可能多条）。返回 `[{watch_id, reason}]`。"""
    out = []
    for w in watches:
        for r in match_reasons(w, notice):
            out.append({"watch_id": w["id"], "reason": r})
    return out

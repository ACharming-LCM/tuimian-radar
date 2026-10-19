# -*- coding: utf-8 -*-
"""用户画像：最小字段 + 完整性判断（契约 api.md §16.2）。

## 为什么只有四个字段

推送要回答"什么事值得打扰**你**"，而"你是谁"的最小集合就是：
你在哪所学校 —— 但本科院校**只展示、不参与匹配**（浙大学生不等于想看浙大招生）
- 你学什么（`majors`）
- 你想去哪（`preferred_schools`）
- 你想做什么方向（`interest_keywords`）

外加可选的 `preferred_units`（意向院系），`/api/match` 已经在用它。

**字段名逐字沿用 `POST /api/match` 的画像**（`school` / `majors` /
`preferred_schools` / `interest_keywords`）—— 不造第二套。用户填一份画像，
匹配、推送、套磁信都用它；造两套之后两边一定会漂，而漂的那一刻没人知道
是哪边错了。

## 两条不许做的事

1. **不许因为一个字段格式不对就整份拒收。** 用户填了 5 个字段、其中 1 个是
   字符串而不是数组，正确的做法是**收下 4 个、丢掉 1 个并说出来**。
   整份 400 会让用户重填一遍，而他并不知道是哪一项错了。
2. **不许静默丢掉未知字段。** `/api/match` 吃 `gpa_rank` / `english` / `research`，
   我们这一层用不到它们，但**原样保存** —— 今天用不到不等于以后不用，
   而"填了以后消失"是最让人不信任表单的行为。
   但**保留 ≠ 认识**：认不出的字段现在会在响应里报 `unknown_fields` + 一句人话
   （B19，2026-10-19）。改之前 `intended_schools` 会被原样保存、然后被彻底忽略 ——
   用户看到 200，以为填上了，推送却完全不知道他的意向学校。
"""
from __future__ import annotations

from server import config

# 最小字段（展示用）：本科院校 + 三个"意向"。
CORE_FIELDS = ("school", "majors", "preferred_schools", "interest_keywords")

# 参与匹配的字段：本科院校**不在里面**（本科院校不是"意向"）。
MATCH_FIELDS = config.PUSH_MATCH_FIELDS

# 列表型字段：这些必须是字符串数组（用户传单个字符串时宽容成一项）。
LIST_FIELDS = ("majors", "preferred_schools", "interest_keywords", "preferred_units",
               "target_levels", "wanted_modes")

# 字符串字段
STR_FIELDS = ("school", "grade")

# 写入上限（公网部署后这些值来自不可信输入）。只截断、不报错：
# 用户填了 200 个专业多半是粘贴了一份文档，整份拒绝不如收下前 N 个并说明。
MAX_STR_LEN = 120
MAX_LIST_ITEMS = 30
MAX_LIST_ITEM_LEN = 80

# 近似字段 → 我们的字段名（契约 §16.2，2026-10-19 B19）。
#
# 为什么是**自动映射**而不是报错让用户改：用户表达的是 intent，不是 API。
# `intended_schools` 和 `preferred_schools` 是同一个意思，让他重填一遍
# 是在为我们的命名买单；而更常见的结局是他以为填上了，
# 然后一直收到"不相关"的推送 —— QA 实测就是这个：README 里那份固定画像
# 用的是 `intended_schools` / `major`，结果能参与匹配的只剩方向词，
# 第一屏 **0 条**。
#
# 只收"一看就知道是同一个意思"的写法。近义词不猜（`schools` 就不收 ——
# 它和 `school`（本科院校）哪个更可能，取决于用户在想什么，猜错比不猜更糟）。
ALIASES = {
    "intended_schools": "preferred_schools",
    "target_schools": "preferred_schools",
    "preferred_school": "preferred_schools",
    "major": "majors",
    "interest_keyword": "interest_keywords",
    "target_level": "target_levels",
    "wanted_mode": "wanted_modes",
}

# 我们**认、但不在最小集里**的字段：`/api/match` / 套磁信会吃，原样保存。
# 它们**不是**未知字段 —— 分不清"我们不用"和"我们不认识"，
# 就会把 `gpa_rank` 报成未知字段，然后用户去把它删掉。
KNOWN_EXTRA_FIELDS = ("name", "grade", "target_entry_year", "gpa_rank",
                      "english", "research", "recommenders", "exclude_expired",
                      # 套磁信落款会用到（B24）。不列进来的话，用户填了 `contact`
                      # 会收到「我们不认识 contact」，而信里明明在用 —— 自相矛盾。
                      "contact")


def _as_str_list(v):
    """把一个值宽容成 `list[str]`。

    用户传 `"物理学"` 而不是 `["物理学"]` 是**最常见的一种填法**，
    为此 400 只会让人骂产品。传 `""` / `null` / `[]` → 空列表。
    """
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    return None                # 类型完全不对 → 让调用方知道


def normalize(raw):
    """校验并规范化一份画像。返回一个字典（**不用元组** —— 顺序靠记必错）：

    - `profile`：可以直接存的 dict（已知字段规范化 + 其它字段原样保留）
    - `dropped`：`[{"field", "reason"}]` —— **丢掉了什么必须说出来**，
      否则用户会以为自己填上了（而推送永远匹配不到）。
    - `mapped`：`[{"from", "to"}]` —— 近似字段被自动映射到了哪个真字段。
      **映射也要说出来**：不说的话，"我填的字段怎么不见了"会变成下一个投诉。
    - `unknown_fields`：`[name]` —— 我们完全不认识的字段。
      它们**仍然原样保存**（今天用不到不等于以后不用），但要说清楚"不参与匹配"。
    - `field_note`：把 `mapped` / `unknown_fields` 合成一句人话（可给前端直接渲染）。
    """
    if not isinstance(raw, dict):
        raise ValueError("画像必须是一个 JSON 对象")

    # ---- ① 先改名，再校验 ----
    #
    # 顺序很重要：如果先校验、后改名，`intended_schools` 会同时出现在
    # "未知字段"和"映射结果"里 —— 一个值两个来源，之后一定会漂。
    renamed, dropped, mapped = {}, [], []
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        canon = ALIASES.get(k)
        if canon is None:
            renamed[k] = v
            continue
        if _non_empty(raw.get(canon)):
            # 真字段已经填了：**以真字段为准**，近似字段说一声就好。
            dropped.append({"field": k,
                            "reason": "和 %s 重复，已按 %s 处理" % (canon, canon)})
            continue
        cur = renamed.get(canon)
        if cur is None:
            renamed[canon] = v
        else:
            # 两个近似字段指向同一个真字段（`intended_schools` + `target_schools`）：
            # 合并而不是让后一个覆盖前一个 —— 覆盖是静默丢数据。
            left = _as_str_list(cur) or []
            right = _as_str_list(v) or []
            renamed[canon] = left + [x for x in right if x not in left]
        mapped.append({"from": k, "to": canon})

    clean = {}

    # 已知的字符串字段
    for k in STR_FIELDS:
        if k not in renamed:
            continue
        v = renamed.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if not isinstance(v, str):
            dropped.append({"field": k, "reason": f"应该是字符串，收到 {type(v).__name__}"})
            continue
        clean[k] = v.strip()[:MAX_STR_LEN]

    # 已知的列表字段
    for k in LIST_FIELDS:
        if k not in renamed:
            continue
        v = renamed.get(k)
        got = _as_str_list(v)
        if got is None:
            dropped.append({"field": k,
                            "reason": f"应该是字符串数组，收到 {type(v).__name__}"})
            continue
        if got:
            clean[k] = [x[:MAX_LIST_ITEM_LEN] for x in got[:MAX_LIST_ITEMS]]

    # 其余字段：**原样保留**（/api/match 会吃它们）。
    #
    # 但保留 ≠ 认识：`intended_schools` 这种拼错/改名的字段也被保留过，
    # 于是用户看到 200、以为填上了，推送却完全无视它（QA 的 "静默忽略"）。
    # 现在把"不认识"单独列出来，**既保留、又告知**。
    known = set(STR_FIELDS) | set(LIST_FIELDS) | set(KNOWN_EXTRA_FIELDS)
    unknown = []
    for k, v in renamed.items():
        if k in known:
            continue
        if isinstance(k, str) and k.startswith("_"):
            continue           # `_` 开头是标注，不是数据（契约 §9 第 1 条）
        clean[k] = v
        unknown.append(k)

    return {"profile": clean, "dropped": dropped, "mapped": mapped,
            "unknown_fields": unknown,
            "field_note": _field_note(mapped, unknown)}


def _field_note(mapped, unknown):
    """把"映射了什么 / 不认识什么"写成一句人话。没有要说的就返回 `None`。

    **必须给出下一步**（"下次直接填 preferred_schools"）：
    只说"没识别"等于把问题丢回给用户，他还是不知道该填什么。
    """
    parts = []
    for m in mapped:
        parts.append("「%s」不是我们的字段名，已按「%s（%s）」处理 —— "
                     "下次直接填「%s」就行"
                     % (m["from"], m["to"], _label(m["to"]), m["to"]))
    if unknown:
        parts.append("以下字段我们没识别：%s。它们会原样保存，但**不参与匹配** "
                     "（匹配只看 %s）"
                     % ("、".join(unknown), " / ".join(MATCH_FIELDS)))
    return "；".join(parts) if parts else None


def completeness(profile):
    """`完整度` 块。**别让前端自己判"填够了没"** —— 判据写两处就会漂。

    `can_push` 只看**参与匹配**的字段：只填了本科院校的用户还不是"能推"，
    因为本科院校不参与匹配（见模块 docstring）。
    """
    if not profile:
        return {
            "filled": False,
            "has": [],
            "missing": list(CORE_FIELDS),
            "can_push": False,
            "message": "还没有画像：填一下「意向学校 / 意向方向」，我们才知道推什么给你。",
        }

    has = [k for k in CORE_FIELDS if _non_empty(profile.get(k))]
    # `preferred_units` 不在 CORE_FIELDS 里，但它是能参与匹配的
    if _non_empty(profile.get("preferred_units")):
        has.append("preferred_units")

    matchable = [k for k in MATCH_FIELDS if _non_empty(profile.get(k))]
    missing = [k for k in MATCH_FIELDS[:3] if not _non_empty(profile.get(k))]

    can_push = bool(matchable)
    if can_push:
        message = "画像已填写，按 " + " / ".join(_label(k) for k in matchable) + " 匹配。"
    elif has:
        message = ("填了本科院校，但它不参与匹配 —— 还需要「意向学校」或「意向方向」，"
                   "否则任何事件都不会被算成跟你相关。")
    else:
        message = "画像里还没有能参与匹配的字段。"

    return {
        "filled": bool(has),
        "has": has,
        "missing": missing,
        "can_push": can_push,
        "message": message,
    }


def _non_empty(v):
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple)):
        return any(isinstance(x, str) and x.strip() for x in v)
    return v is not None


_LABELS = {
    "school": "本科院校",
    "majors": "本科专业",
    "preferred_schools": "意向学校",
    "interest_keywords": "意向方向",
    "preferred_units": "意向院系",
}


def _label(k):
    return _LABELS.get(k, k)

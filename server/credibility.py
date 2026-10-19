# -*- coding: utf-8 -*-
"""字段可信度：值 / null / unknown 三态。契约见 `contracts/api.md` 第 14 节。

## 要解决的问题

现在 `deadline: null` **一个值同时表示两件完全不同的事**：

- 「公告里就没有截止日」—— 我读了全文，确实没有 → **用户该放心**；
- 「我们没读到」—— 正文没抓到 / 是扫描件 / 页面是 JS 空壳 → **用户必须自己去看原文**。

**对用户这是天差地别的两句话，而现在的接口分不出来。**
评审问"这个还能报吗"，答成"没有截止日"而实际是我们没读到，
是**最伤信任**的一种错：用户按"没有截止日"做了决定，而那个信息我们根本没拿到。

## 三态

| 态 | 含义 | 依据 |
|---|---|---|
| `found` | 抽到了值 | 字段里有值 |
| `absent` | **正文读到了、确实没有** | 需要数据层明确说，不许推测 |
| `unknown` | **我们判不了** | 默认态，也是老数据的态 |

**`absent` 是唯一需要证据的态。** 没有 `field_state` 的旧记录
一律 `unknown` —— 因为"没有值"既可能是不存在、也可能是没读到，
**这两种在旧数据里长得一模一样**，我们没有依据区分。

（一个例外，见 `state_of`：值**在**的时候就是 `found`。
"有值"这件事本身就是证据，不需要 `field_state` 再确认一遍 ——
把一条 `deadline: "2026-11-20"` 说成 `unknown` 是另一种不诚实。）

## `body` 用的是另一套词

`body` 不是 found/absent/unknown，是 `ok`（站点选择器取到真正文）/
`generic`（兜底选择器）/ `missing`（没取到）。

**我给这套词加了第四个取值 `unknown`**，只用于"老数据没记这一项"。
A 的产出器**永远不需要发 `unknown`** —— 它是给历史数据留的。
不加的话，老数据必须在这三个里挑一个，而挑任何一个都是**编造**。
"""
from __future__ import annotations

# 三态
FOUND = "found"
ABSENT = "absent"
UNKNOWN = "unknown"
FIELD_STATES = (FOUND, ABSENT, UNKNOWN)

# 正文抽取方式。`unknown` 只为老数据存在（见模块 docstring）
BODY_OK = "ok"
BODY_GENERIC = "generic"
BODY_MISSING = "missing"
BODY_UNKNOWN = "unknown"
BODY_STATES = (BODY_OK, BODY_GENERIC, BODY_MISSING, BODY_UNKNOWN)

# 适用三态的字段。**这就是"哪些字段适用"的答案**，别处不要再各写一份。
# 选这些的理由：它们是用户会**据此做决定**的结构化字段
# （什么时候截止 / 招什么专业 / 要交什么材料 / 怎么联系）。
# `title` / `url` 不适用 —— 它们**必然**有值，三态没有意义。
#
# **2026-10-17 从 3 个扩到 5 个**（加 `materials` / `contact`）。
# 这一条**必须晚于数据层落盘**：如果数据里没有这两个键，`state_of()` 会全部返回
# `unknown` → 每条记录的 `untrusted_count` 从 2 变 4 → 前端全线显示
# "4 个字段不可信"、`/api/quality` 显示 100% unknown。
# 而真相是"数据还没落盘"，不是"数据烂" —— 那正是 §7.5"0% 和没数据是两个数"的翻版。
# **顺序：数据先落盘 → 这里才加。**
TRACKED_FIELDS = ("deadline", "majors", "materials", "contact", "attachments")

# 摘要里"不可信"只算 unknown。
# `absent` **不算不可信** —— 它是一个**有依据的确定回答**，
# 和 `unknown` 混在一起会让这个数字失去意义（全是红等于没有红）。
UNTRUSTED_STATE = UNKNOWN

# 比 unknown 轻一档：值得提示，但不是"我们不知道"。
# 目前只有 `body: generic`（正文是兜底选择器抓的，可能有噪声）。
WEAK_STATES = (BODY_GENERIC,)


def _has_value(rec, name):
    """字段里到底有没有值。**空列表 / 空串都算没有。**

    **`contact` 是复合字段，要单独判**（2026-10-17 实测出来的坑）：
    470 条里 **470 条都有 `contact` 键，但其中 232 条是
    `{"email": null, "phone": null}`** —— 一个"空壳"。

    只判 `bool(dict)` 会把这 232 条报成 `found`（"有联系方式"），
    而事实是**公告里根本没写**。那正是这个项目一路在防的那种错：
    **一个看起来有值的字段，掩盖了"确实没有"这件事。**
    """
    v = rec.get(name)
    if v is None:
        return False
    if isinstance(v, (list, tuple, dict, str)) and len(v) == 0:
        return False
    if name == "contact" and isinstance(v, dict):
        return bool(v.get("email") or v.get("phone"))
    return True


def state_of(rec, name):
    """某个字段的三态。**这是唯一的判定入口**，排序/统计/聚合都走它。

    规则按优先级：

    1. `field_state` 里明确给了合法值 → **用它**（只有它能给出 `absent`）；
    2. 没给 / 给了个不认识的词 → **有值就是 `found`，没值就是 `unknown`**。

    第 2 条为什么"有值算 found"：值在就是证据。旧记录说 `unknown`
    是为了**不凭空说 absent**，不是要否认我们已经拿到的值 ——
    把 `deadline: "2026-11-20"` 报成 `unknown` 会让用户白跑一趟原文。
    """
    fs = rec.get("field_state")
    if isinstance(fs, dict):
        s = fs.get(name)
        if s in FIELD_STATES:
            return s
    return FOUND if _has_value(rec, name) else UNKNOWN


def body_state_of(rec):
    """正文抽取状态。老数据 → `unknown`（见模块 docstring）。"""
    fs = rec.get("field_state")
    if isinstance(fs, dict):
        s = fs.get("body")
        if s in BODY_STATES:
            return s
    # 没有记录就**不猜** ok/generic/missing 中的任何一个
    return BODY_UNKNOWN


def field_state_of(rec, normalize=True):
    """完整的 `field_state`。详情接口给这个。

    `normalize=False` 时**原样透传数据层给的那份**（保留它的额外键）——
    数据层以后加字段不会被我这里吃掉。详情用 normalize=True 保证四个键都在，
    因为前端要照着渲染，**缺键比错值更难查**。
    """
    fs = rec.get("field_state")
    out = dict(fs) if isinstance(fs, dict) else {}
    if not normalize:
        return out
    for name in TRACKED_FIELDS:
        out[name] = state_of(rec, name)
    out["body"] = body_state_of(rec)
    out["_source"] = ("data" if isinstance(fs, dict) and fs else "inferred")
    return out


def is_untrusted(rec, name):
    return state_of(rec, name) == UNTRUSTED_STATE


def untrusted_summary(rec):
    """这条**有几个字段不可信**，以及分别是哪些。契约第 14 节：列表给摘要。

    `untrusted` 只数 `unknown`（`absent` 是有依据的回答，不算）。
    `weak` 数那些"能用但要提示"的（目前只有 `body: generic`）。
    """
    untrusted = [n for n in TRACKED_FIELDS if is_untrusted(rec, n)]
    weak = []
    if body_state_of(rec) in WEAK_STATES:
        weak.append("body")
    return {
        "untrusted_count": len(untrusted),
        "untrusted_fields": untrusted,
        "weak_fields": weak,
        "has_untrusted": bool(untrusted),
        "states": {n: state_of(rec, n) for n in TRACKED_FIELDS},
        "body": body_state_of(rec),
    }


# ---------------------------------------------------------------------------
# 给 C 的措辞（契约第 14 节 + 交接单）
# ---------------------------------------------------------------------------
#
# **绝对禁止把 `unknown` 显示成"没有"。**
# 那正是这一节要消灭的那句话。
UNKNOWN_DEADLINE_TEXT = "未能读取截止日，请点原文核对"
ABSENT_DEADLINE_TEXT = "该公告未公布截止日"


def deadline_display(rec):
    """截止日的**现成中文**，按三态给三种说法。

    按本项目规矩"凡是前端渲染要用的字段，必须在同一个响应里给全"：
    让 C 自己按 `field_state` 拼三句话，三个页面会拼出三种写法，
    而且很容易漏掉 `unknown` 那一支（漏掉就等于说了"没有"）。

    返回 `{"state", "text", "value", "url", "must_check_original"}`。
    `must_check_original=True` 时**必须**带原文链接，这是契约要求的。
    """
    state = state_of(rec, "deadline")
    url = rec.get("url")
    if state == FOUND:
        val = rec.get("deadline")
        return {"state": FOUND, "text": str(val), "value": val, "url": url,
                "must_check_original": False}
    if state == ABSENT:
        return {"state": ABSENT, "text": ABSENT_DEADLINE_TEXT, "value": None,
                "url": url, "must_check_original": False}
    return {"state": UNKNOWN, "text": UNKNOWN_DEADLINE_TEXT, "value": None,
            "url": url, "must_check_original": True}


# ---------------------------------------------------------------------------
# 排序用的分桶（契约第 14 节的核心不变量）
# ---------------------------------------------------------------------------
#
# **`unknown` 不许被当成 `absent`。** 现在 `_sort_key_deadline_asc` 把
# `deadline is None` 一律 `or date.max` 沉底 —— 那等于把
# "我们没读到" 和 "确实没有截止日" 一起埋在最下面。
#
# 按截止日升序时，用户想看的是"哪个最快到期"。三态下有三个位置：
#
#   0  found   —— 按真实日期升序排
#   1  unknown —— **夹在中间**：我们不知道，所以它比"确定没有"紧急；
#                 又因为没有日期，插不进 found 的真实顺序里
#   2  absent  —— 确定没有截止日，对"按截止日排"这件事最不重要
#
# 为什么 unknown 不排第一：用户要的是"按截止日排"，
# 第一页全是没日期的条目会让这个排序看起来是坏的。
# 为什么 unknown 不能和 absent 一起沉底：那正是这一环要消灭的那个混淆 ——
# 一个 "unknown" 的公告可能下周就截止，埋在 196 条下面等于没提醒。
DEADLINE_BUCKET_FOUND = 0
DEADLINE_BUCKET_UNKNOWN = 1
DEADLINE_BUCKET_ABSENT = 2

DEADLINE_BUCKET_LABELS = {
    DEADLINE_BUCKET_FOUND: "found",
    DEADLINE_BUCKET_UNKNOWN: "unknown",
    DEADLINE_BUCKET_ABSENT: "absent",
}


def deadline_bucket(rec):
    """截止日排序用的分桶序号。**排序必须走它，不许直接看 `deadline is None`。**"""
    s = state_of(rec, "deadline")
    if s == FOUND:
        return DEADLINE_BUCKET_FOUND
    if s == ABSENT:
        return DEADLINE_BUCKET_ABSENT
    return DEADLINE_BUCKET_UNKNOWN


# ---------------------------------------------------------------------------
# 统计（`GET /api/quality`）
# ---------------------------------------------------------------------------

def tally(records, name):
    """某个字段在给定记录集上的三态计数 + 比例。"""
    c = {FOUND: 0, ABSENT: 0, UNKNOWN: 0}
    for r in records:
        c[state_of(r, name)] += 1
    n = sum(c.values())
    return {
        "found": c[FOUND], "absent": c[ABSENT], "unknown": c[UNKNOWN],
        "total": n,
        # 比例用 4 位小数：演示时说"11.2%"够用，也不需要浮点比较的容差
        "found_rate": round(c[FOUND] / n, 4) if n else None,
        "absent_rate": round(c[ABSENT] / n, 4) if n else None,
        "unknown_rate": round(c[UNKNOWN] / n, 4) if n else None,
    }


def body_tally(records):
    """正文抽取方式的分布。

    **比率的分母是"记了这一项的那些"，不是全部。**
    真实数据里 196/196 都是 `unknown`（老数据没记），
    如果按全部算，`generic_rate` 会是 `0.0` —— 读起来像
    "测过了，没有一条用兜底选择器"，而真相是**一条都没记录**。
    这违反本项目自己那条"0% 和'没数据'是两回事"。

    所以：一个都没记 → 比率是 `None`（`summary` 里说"无"）；
    记了一部分 → 比率按**已知的那些**算，同时把 `unknown` 的条数摆在旁边。
    """
    c = {s: 0 for s in BODY_STATES}
    for r in records:
        c[body_state_of(r)] += 1
    n = sum(c.values())
    known = n - c[BODY_UNKNOWN]
    out = {"total": n, "known": known}
    for s in BODY_STATES:
        out[s] = c[s]
    for s in BODY_STATES:
        if s == BODY_UNKNOWN:
            continue
        out[f"{s}_rate"] = round(c[s] / known, 4) if known else None
    out["unknown_rate"] = round(c[BODY_UNKNOWN] / n, 4) if n else None
    out["_rate_base"] = ("按「记了正文抽取方式的那些」算（known）；"
                         "known=0 时比率是 null，不是 0")
    return out

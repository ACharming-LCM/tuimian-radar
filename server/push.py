# -*- coding: utf-8 -*-
"""推送：事件 × 画像 → 你会收到的那条（契约 api.md §16）。

## 这一层不做三件事

- **不判断"这条公告重要不重要"。** 那是数据层的 `relevance`，而且它
  **只许排序、不许用来决定不推** —— 过滤错了用户会错过报名，排序错了只是低几行。
- **不编造截止日。** 三态原样透传，`unknown` 就是 `unknown`。
- **不发通知。** 只出"你会收到的那条"（预览），渠道适配位置在订阅接口里。

## 三件必须做对的事

1. **`unknown` 和 `absent` 是两句不同的话。** 推送文案天然短，最容易把
   "该公告未公布截止日"和"未能读取截止日"写成一模一样 —— 而前者让用户放心、
   后者让用户去看原文。写错一句，用户就照着"没有截止日"做决定了。
2. **压掉的必须说出来。** 匹配到 4 条只给 3 条，剩下那条去哪了？
   不解释的沉默会被理解成漏推，而"漏推"是订阅产品最致命的信任问题。
3. **去重按 `event_id`，不是 `notice_id`。** 同一条公告会先后产生
   `added` / `deadline_changed` / `attachment_added` 三种事件 ——
   它们是**三件不同的事**，都值得各说一次。按 notice_id 去重会把后两件吞掉。
"""
from __future__ import annotations

import hashlib
from datetime import date

from server import config, credibility, quality

# 三种事件的标题前缀。**不是营销文案** —— 它要说清"发生了什么"，
# 用户判断要不要点开，靠的就是这一句。
_TYPE_LABEL = {
    "added": "新公告",
    "deadline_changed": "截止日变了",
    "attachment_added": "补了附件",
}

# `suppressed[].reason` 的取值与给人看的话（契约 §16.7）。
#
# ⚠️ **没有 `expired` 了。** 已截止的公告不压掉，只**排序沉底**（见 `_sort_key`
# 的 docstring：截止日可能抽错、学校会延期、补发的附件截止后照样有人要）。
# 压掉等于"我们替你决定这条没用"，而排序错了只是低几行，压错了是永久看不到。
_SUPPRESSED_TEXT = {
    "duplicate": "这条你今天已经收到过了（同一事件不重复推送）",
    "rate_limited": "今天的推送数已经到上限了，这条明天会再出现",
    "backfill": ("这条是我们**补抓**时翻出来的历史公告，不是今天新发的"
                 "（不计入推送，但可以在公告列表里翻到）"),
    "merged": ("同一条公告在这批里还有另一条事件（比如既算新公告、截止日又变了），"
               "已经合并成一条，没有丢"),
    "junk": ("这条记录本身是脏的（标题是站点名，或正文只剩发布时间/点击数），"
             "推送出去等于给你一张空卡；它在公告列表里也默认不显示，"
             "要用 `include_junk=true` 复查"),
    "not_apply": ("这条命中了你的学校或方向，但**不是招生公告**（讲座 / 会议 / 名单公示 / "
                  "事务通知）。首屏只放招生类 —— 宁可空，不拿讲座凑数"),
    "mode_mismatch": ("这条的招生方式跟你要的不一样（例如你要推免、它是统考）。"
                       "推送和匹配用同一套过滤，两边答案才会一致"),
    "topic_unknown": ("这条命中了你的学校或方向，但**我们不知道它是不是招生公告**"
                      "（老事件没有 topic 字段）。不确定的东西不进首屏"),
    # ---- `notice_kind` 分层（B22，契约 §18.9）----
    "catalog": ("这条是招生目录 / 专业目录 / 简章一类的**参考资料**，不是今天要办的事 —— "
                "已经挪到「参考资料」区，不进首屏"),
    "emba": ("这条是 EMBA / MBA / 在职 / 非全日制一类的**在职项目**，"
             "一般要求工作经验，推免和直博报不了 —— 对你这个画像不出现"),
    "out_of_window": ("这条发布已经超过窗口（近 60 天），不在「这周该看」的范围里。"
                      "仍然留在公告库里，可以在列表里翻到"),
    "outdated_year": ("这是往届（招生年份早于你的目标入学年）的通知 —— "
                      "比如「2025 级培养项目选拔」对 2027 入学的你没有意义。"
                      "仍然留在公告库里，可以在列表里翻到"),
}

# 事务性公告：标题就能判定的、只和统考流程有关的公告。
# 为什么要在 `apply_mode` 之外再判一次：实测有 119 条统考公告被抽成 `apply_mode=推免`
# （标题是「…报考点…网上报名公告」，正文里另有「推免」字样）。抽错了，
# 光靠 apply_mode 过滤就会把它们推进推免学生的首屏。
_EXAM_TRANSACTIONAL = ("报考点", "考点", "考场", "准考证", "网上确认",
                       "现场确认", "考试安排", "考试须知")
_EXAM_SIGNALS = ("网上报名", "全国硕士研究生招生考试", "统考", "初试")
_TUIMIAN_SIGNALS = ("推免", "预推免", "夏令营", "直博")


def _is_exam_transactional(event) -> bool:
    title = _norm(event.get("title"))
    if any(w in title for w in _TUIMIAN_SIGNALS):
        return False
    if any(w in title for w in _EXAM_TRANSACTIONAL):
        return True
    if any(w in title for w in _EXAM_SIGNALS):
        return True
    # 实测：「报考北航 2027 年硕士研究生温馨提示」—— 没有"报考点"字样，
    # 但"报考 + 硕士研究生"已经说明它是统考流程的事务通知。
    return "报考" in title and ("硕士研究生" in title or "硕士" in title)

# 同一批里同一条公告只留一条时，留哪个：**信息增量大的赢**。
#
# `deadline_changed` 带"从 X 改成 Y"，`attachment_added` 带附件名，
# 而 `added` 只说"有新公告" —— 同批里既有"新公告"又有"截止日变了"时，
# 后者对用户更有用（他关心的就是"什么时候截止"）。
_TYPE_PRIORITY = {"deadline_changed": 3, "attachment_added": 2, "added": 1}

# 负向维度（契约 §18.8，B20）：命中就**降权**，并把命中了什么说出来。
#
# 只从**标题**里找：事件的正文不在事件流里，而"项目类型/公告类型"在标题里
# 几乎总会写明（「…（EMBA）专业学位…」「…报考点公告」）。从 `matched[]` 里
# 找会误伤 —— 那是方向词，不是项目类型。
#
# ⚠️ 顺序有意义：`emba` 要排在 `mba` 前面，否则"EMBA"会先命中 `mba`，
# `text` 里就把一个 EMBA 项目说成 MBA。
_MISFIT_RULES = (
    ("degree_type", ("emba", "高级管理人员工商管理", "mba", "工商管理硕士",
                     "mpacc", "会计硕士", "mpa", "公共管理硕士",
                     "mem", "工程管理硕士", "在职攻读", "非全日制")),
    ("transactional", ("报考点", "考点", "考场", "准考证", "网上确认",
                       "现场确认", "考试安排", "考试须知", "复试录取工作办法")),
)

# 画像自己就在找专业学位 / 管理类项目时**不许降权** ——
# 否则会把"想读 MBA 的人"的简章全压下去，那叫纠正过度。
# 判据只看用户**明确说的**：目标层次写了专硕/MBA，或专业/方向本来就是管理类。
_PROFESSIONAL_HINTS = ("专硕", "专业学位", "mba", "emba", "mpacc", "非全日制")
_MGMT_MAJOR_HINTS = ("工商管理", "管理科学", "会计", "金融", "经济",
                     "mba", "emba", "mpacc", "mpa", "人力资源", "市场营销")

# ---------- `notice_kind`：这条公告是"哪一种"（A29，契约 §18.9） ----------
#
# `topic` 只回答"是不是招生相关"，把两类完全不同的东西混在一起：
# 「接收推免生工作通知」（有时限、要动作）和「2027 年招生专业目录」（随时可查）。
# 实测 apply 且非 junk 的 1193 条里，目录/简章 296 条，而首屏 10 条里有 6 条是它们。
#
# 四档：`announcement` / `catalog` / `emba` / `other`（A 的 `collector/kind.py`）。
KIND_ANNOUNCEMENT = "announcement"
KIND_CATALOG = "catalog"
KIND_EMBA = "emba"
KIND_OTHER = "other"



# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def parse_day(s):
    """`YYYY-MM-DD` → date。解析不了返回 `None`（**不猜、不当成今天**）。"""
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def days_left_of(deadline, today):
    """距截止还有几天。解析不了 → `None`。

    `None` 和 `0` 的区别是"不知道"和"今天截止" —— 混起来会让人今天白跑一趟。
    """
    d = parse_day(deadline)
    return None if d is None else (d - today).days


def within_window(event, today, days=None):
    """发布日是否落在"近 N 天"窗口内（B22/B23 共用一个口径）。

    **发布日读不到 → False**（不进窗口）。窗口是要对外说的（"近 60 天"），
    把不知道发布日的算进去，就等于替它编了一个发布日。
    """
    days = config.PUSH_POOL_FRESH_DAYS if days is None else days
    d = parse_day(event.get("publish_date"))
    if d is None:
        return False
    age = (today - d).days
    return 0 <= age <= days


# ---------------------------------------------------------------------------
# 画像 × 事件
# ---------------------------------------------------------------------------

def _norm(s):
    return (s or "").strip().lower()


def _event_hay(event):
    """画像词要往里找的那段文本。

    **子串方向固定为 `画像词 in 这段`** —— 反方向（`这段 in 画像词`）
    会让"物理"命中"物理化学"，那不叫匹配，叫联想。
    """
    return " ".join([
        _norm(event.get("title")),
        _norm(event.get("unit")),
        " ".join(_norm(x) for x in (event.get("matched") or [])),
    ])


def match_because(event, profile):
    """匹配依据（人话）。空列表 = 没匹配上。

    **给句话，不给分数。** 用户没法核对 `score: 0.83`，但能核对
    "我确实关注大连理工"。推送说"这条跟你有关"，就得说出凭什么。

    规则（契约 §16.5）：画像四个字段里任意一个**子串命中**就算匹配。
    子串方向见 `_event_hay`。

    > 这是**匹配判据**（空 = 不推），不是完整解释。给人看的完整理由在
    > `why` 里（它还带上新鲜度和截止日）—— 两个键都在响应里，
    > `matched_because` 保留是因为 C 的 `pushes.js` 在读它，
    > 删掉会让推送页的「为什么推给你」整块消失。
    """
    if not profile:
        return []
    reasons = []
    school = _norm(event.get("school"))
    unit = _norm(event.get("unit"))
    hay = _event_hay(event)

    for s in profile.get("preferred_schools") or []:
        if _norm(s) and _norm(s) in school:
            reasons.append(f"意向学校：{s}")
    for m in profile.get("majors") or []:
        if _norm(m) and _norm(m) in hay:
            reasons.append(f"本科专业：{m}")
    for k in profile.get("interest_keywords") or []:
        if _norm(k) and _norm(k) in hay:
            reasons.append(f"意向方向：{k}")
    for u in profile.get("preferred_units") or []:
        if _norm(u) and _norm(u) in unit:
            reasons.append(f"意向院系：{u}")

    seen, out = set(), []
    for r in reasons:          # 去重但保序
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out



# ---------------------------------------------------------------------------
# 四维打分（契约 §18）：对口程度
# ---------------------------------------------------------------------------

def freshness_of(event, today):
    """发布日的新鲜度档。返回 `(value, state, age_days)`。

    **四个状态必须分开说**：`fresh` / `mid` / `old` / `unknown`。

    最容易犯的错是把 `unknown` 归进 `old`：`publish_date` 是空串的事件
    在 1131 条里有一批（数据层抽不到），把它们当成"旧公告"是**编造一个我们
    没有的事实** —— 和 §14 的三态是同一条原则。

    发布日在**未来**（数据错）也归 `unknown`，但 `age_days` 会是负数，
    调用方要能看出来 —— 不猜、不截断成 0。
    """
    pub = parse_day(event.get("publish_date"))
    if pub is None:
        return config.PUSH_FRESH_UNKNOWN, "unknown", None
    age = (today - pub).days
    if age < 0:
        return config.PUSH_FRESH_UNKNOWN, "unknown", age
    if age <= config.PUSH_FRESH_FULL_DAYS:
        return config.PUSH_FRESH_FULL, "fresh", age
    if age <= config.PUSH_FRESH_MID_DAYS:
        return config.PUSH_FRESH_MID, "mid", age
    return config.PUSH_FRESH_OLD, "old", age


def score_dims(event, profile, today):
    """四维打分（每维 0~1）。返回 `(dims, detail)`。

    `dims` 进响应（`score_dims`）；`detail` 只用于拼 `why`，**不进响应**。

    四维和 `matched_because` 的关系：不重复、不互相替代。
    `matched_because` 回答"够不够格推给你"（空 = 不推），
    四个维度回答"对你的对口程度"，用来**排序**。
    """
    school = _norm(event.get("school"))
    hay = _event_hay(event)
    profile = profile or {}

    school_hits = [s for s in (profile.get("preferred_schools") or [])
                   if _norm(s) and _norm(s) in school]
    major_hits = [m for m in (profile.get("majors") or [])
                  if _norm(m) and _norm(m) in hay]
    kws = [k for k in (profile.get("interest_keywords") or []) if _norm(k)]
    direction_hits = [k for k in kws if _norm(k) in hay]

    if not kws:
        # 画像里没填方向 —— "没得比"，不是"比了没中"。
        direction, direction_state = config.PUSH_DIRECTION_UNKNOWN, "no_profile"
    elif not (event.get("matched") or []) and not direction_hits:
        # 事件里没有方向信号（`matched[]` 空、标题也没命中）——
        # 这是我们没拿到数据，不能判成 0。
        direction, direction_state = config.PUSH_DIRECTION_UNKNOWN, "no_data"
    else:
        direction = 1.0 if direction_hits else 0.0
        direction_state = "hit" if direction_hits else "miss"

    fresh, fresh_state, age = freshness_of(event, today)

    dims = {
        "school_hit": 1.0 if school_hits else 0.0,
        "major_hit": 1.0 if major_hits else 0.0,
        "direction_hit": direction,
        "freshness": fresh,
    }
    detail = {
        "school": school_hits, "major": major_hits, "direction": direction_hits,
        "direction_state": direction_state,
        "fresh_state": fresh_state, "age_days": age,
    }
    return dims, detail


def wants_professional_degree(profile):
    """这个画像本身是不是在找专业学位 / 管理类项目？

    只认用户**明确写了的**：`target_levels` 里有专硕/MBA/非全日制，
    或者 `majors` / `interest_keywords` 本来就是管理类。

    **空画像返回 False** —— 推免产品面对的是应届本科生，而 EMBA/MBA
    **要求工作经验**，结构上报不了。所以"不知道你的层次"时，EMBA 简章
    也不该排第一；这不是猜他"要学术"，是这条项目类型对目标人群不成立。
    """
    profile = profile or {}

    def as_list(v):
        # 契约里的示例画像可能把数组写成字符串（`"majors": "物理学"`）。
        # 直接拿字符串去 `+` 列表会 TypeError（2026-10-20 门禁抓到：
        # 匹配页新增 EMBA 过滤后，这条路径第一次被示例画像走到）。
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v) if isinstance(v, (list, tuple, set)) else [v]

    levels = " ".join(_norm(x) for x in as_list(profile.get("target_levels")))
    if any(t in levels for t in _PROFESSIONAL_HINTS):
        return True
    hay = " ".join(_norm(x) for x in
                   as_list(profile.get("majors")) + as_list(profile.get("interest_keywords")))
    return any(t in hay for t in _MGMT_MAJOR_HINTS)


def misfit_of(event, profile):
    """负向维度（契约 §18.8）。返回 `{"weight", "keys", "text"}`。

    - `weight`：乘数，1.0 = 不降权；命中几条就乘几次。
    - `keys`：命中了哪些维度（`degree_type` / `transactional`）——
      **给机器用**，前端可以据此打标签。
    - `text`：一句人话，直接进 `why`；没命中时是 `None`。

    **只降权、不隐藏。** 项目类型同样可能判错（标题写「非全日制」的公告
    也可能同时招全日制），降权错了只是低几行，藏起来就是用户永远看不到 ——
    和"已截止沉底不压掉""relevance 只排不滤"是同一条原则。
    """
    title = _norm(event.get("title"))
    skip_degree = wants_professional_degree(profile)
    weight, keys, texts = 1.0, [], []
    for key, words in _MISFIT_RULES:
        if key == "degree_type" and skip_degree:
            continue
        hit = next((w for w in words if _norm(w) in title), None)
        if hit is None:
            continue
        weight *= config.PUSH_MISFIT_WEIGHTS.get(key, 1.0)
        keys.append(key)
        if key == "degree_type":
            texts.append("命中「%s」：这是在职/专业学位项目（一般要求工作经验），"
                         "推免报不了 —— 降权" % hit.upper())
        else:
            texts.append("命中「%s」：这是报考点/考场一类的事务性公告，"
                         "不是招生简章 —— 降权" % hit)
    return {"weight": round(weight, 3), "keys": keys,
            "text": "；".join(texts) if texts else None}


def weighted_score(dims):
    """`w1*school + w2*major + w3*direction + w4*freshness`，范围 0~1。

    **不暴露给前端**（和 `sort_score` 一个道理：把权重公式变成对外承诺，
    以后调口味就会改掉屏幕上的数）。前端能看的是四维本身 + `why`。
    """
    return sum(config.PUSH_SCORE_WEIGHTS[k] * (dims.get(k) or 0.0)
               for k in config.PUSH_SCORE_WEIGHTS)


def explain(item, detail):
    """`why` —— 给人看的理由数组，**至少一条**。

    和 `matched_because` 的区别：这里除了"命中了什么"，还带上
    **新鲜度**和**截止日** —— 用户问"为什么是这条"时，答案经常不是
    "因为你关注这个学校"，而是"因为它昨天发的而且下周就截止"。
    """
    why = []
    if detail["school"]:
        why.append("意向学校命中：%s" % "、".join(detail["school"][:2]))
    if detail["major"]:
        why.append("专业命中：%s" % "、".join(detail["major"][:2]))
    if detail["direction"]:
        why.append("方向命中：%s" % "、".join(detail["direction"][:2]))
    elif detail["direction_state"] == "miss":
        why.append("方向未命中（已填方向词，但这条公告里没有）")

    age = detail["age_days"]
    if detail["fresh_state"] == "fresh":
        why.append("%d 天前发布（30 天内）" % age)
    elif detail["fresh_state"] == "mid":
        why.append("%d 天前发布（30~90 天，不是本周新增）" % age)
    elif detail["fresh_state"] == "old":
        why.append("%d 天前发布（90 天以上，历史参考）" % age)
    elif age is not None and age < 0:
        why.append("发布日期不在过去（数据异常，%d 天），以原文为准" % age)
    else:
        why.append("发布日期未读到，新旧无法判断")

    # 截止日那句：三态三种说法，**直接复用 deadline 的三态**，不再拼第四种。
    block = item.get("deadline") or {}
    days = block.get("days_left")
    if block.get("state") == credibility.FOUND and days is not None:
        if days < 0:
            why.append("已于 %s 截止（%d 天前）" % (block.get("text"), -days))
        elif days == 0:
            why.append("今天截止")
        elif days <= config.REMINDER_WINDOWS[0]:
            why.append("%d 天内截止（高优）" % days)
        else:
            why.append("%d 天后截止" % days)
    elif block.get("state") == credibility.ABSENT:
        why.append("公告未公布截止日")
    else:
        why.append("截止日未读到，请点原文核对")

    # 负向维度那句（契约 §18.8）：**命中了什么要写出来**，
    # 否则用户只看到"这条莫名其妙沉下去了"，那叫黑箱排序。
    misfit = item.get("misfit") or {}
    if misfit.get("text"):
        why.append(misfit["text"])

    return why


def tier_of(dims, topic=None, kind=None):
    """一级 / 二级（契约 §18.6、§18.9）。

    **2026-10-19 起按 `notice_kind` 分层**（B22）：

    - `announcement`（推免/夏令营/报名/考核…）→ **1**，首屏主力；
    - `other`（招生相关但不是通告）→ **2**，只补位；
    - `catalog` / `emba` **不进首屏**（前者去「参考资料」区，后者按画像硬排除），
      所以这里对它们返回 2，但调用方根本不会拿它们来问。

    ## `kind` 缺失时退回旧规则（**不知道 ≠ 不是**）

    老事件没有 `notice_kind`（A29 之前的产出）。那时仍按 2026-10-18 的判据：
    **命中学校或专业、且 `topic == "apply"` → 1**，其余 2。

    为什么允许两套规则并存：字段一旦缺失就整批降到二级，
    会让**旧数据把首屏清空**；而"我们还没标"不等于"它不是通告"。
    `tally.kind_unknown` 会把这类条数报出来 —— **降级要可见**。
    """
    d = dims or {}
    if kind in (KIND_ANNOUNCEMENT,):
        return 1
    if kind in (KIND_OTHER, KIND_CATALOG, KIND_EMBA):
        return 2
    hit = (d.get("school_hit") or 0) >= 1.0 or (d.get("major_hit") or 0) >= 1.0
    if not hit:
        return 2
    if topic is not None and topic != "apply":
        return 2
    return 1


def tier_reason(dims, topic=None, kind=None):
    """给前端的一句人话："为什么它在这一层"。

    `kind` 有值时按类型说（"招生通知" / "其他招生相关"）；
    没有时退回旧说法（"命中你的意向学校"）。
    """
    d = dims or {}
    if kind == KIND_ANNOUNCEMENT:
        return "招生通知：推免 / 报名 / 考核一类，今天该看"
    if kind == KIND_OTHER:
        return "其他招生相关内容（不是通告，放在后面补位）"
    if kind == KIND_CATALOG:
        return "参考资料：招生目录 / 简章一类，不进首屏"
    if kind == KIND_EMBA:
        return "在职项目（EMBA / MBA / 非全日制）"
    hit_school = (d.get("school_hit") or 0) >= 1.0
    hit_major = (d.get("major_hit") or 0) >= 1.0
    if tier_of(d, topic) == 1:
        if hit_school and hit_major:
            return "命中你的意向学校和专业"
        if hit_school:
            return "命中你的意向学校"
        return "命中你的本科专业"
    if (hit_school or hit_major) and topic is not None and topic != "apply":
        return "命中了你的学校/专业，但这条不是招生公告（讲座/名单/事务通知）"
    if hit_school or hit_major:
        return "命中了你的学校/专业，但这条不是招生公告"
    return "仅方向命中（未命中你的意向学校或专业）"


def push_id_of(user_id, event_id):
    """稳定去重键。**不含当天日期、也不含生成时间。**

    含了时间就每次都是新 id，去重立刻失效 —— 而"同一条变更不重复推送"
    是用户唯一能直接感知到的推送质量。
    """
    raw = f"{user_id}|{event_id}".encode("utf-8")
    return "push_" + hashlib.sha1(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 截止日 / 提醒
# ---------------------------------------------------------------------------

def deadline_block(event, today):
    """复用 §14 的 `deadline_display`，只多一个 `days_left`。

    **前端只写一套渲染逻辑**：公告卡片和推送卡片同一个形状，三句话由服务端给全。
    让前端拼一次，三个页面就会拼出三种写法，而且最容易漏掉 `unknown` 那一支 ——
    漏掉就等于说了"没有"。
    """
    block = credibility.deadline_display(event)
    block["days_left"] = (
        days_left_of(event.get("deadline"), today)
        if block["state"] == credibility.FOUND else None
    )
    return block


def reminder_block(event, today, plan_active):
    """截止提醒 T-7 / T-3 / T-1。**窗口语义，不是精确某一天。**

    只对 `found` 给：

    - `absent` 不给 —— 本来就没有截止日，提醒什么？
    - `unknown` 也不给 —— 它已经带着"请点原文核对"和链接了。
      再挂一个提醒等于**假装我们知道什么时候截止**，那是最坏的一种自信。

    返回 `None` 的四种情形（没订阅 / 不是 found / 还早 / 已过期）**不在此处区分** ——
    调用方只看"有没有"，理由由 `deadline` 的三态和 `plan` 说明。
    """
    if not plan_active:
        return None
    if credibility.state_of(event, "deadline") != credibility.FOUND:
        return None

    dl = days_left_of(event.get("deadline"), today)
    if dl is None or dl < 0 or dl > max(config.REMINDER_WINDOWS):
        return None

    # **取最紧的一档**：4 天在 7 天窗口内、3 天窗口外 → T-7（不是两个都挂）。
    if dl <= 1:
        kind, window = "T-1", 1
    elif dl <= 3:
        kind, window = "T-3", 3
    else:
        kind, window = "T-7", 7

    # T-1 不写"还剩 1 天"：读起来像还有富余，而它其实是"明天就截止"。
    if dl == 0:
        text = f"今天截止（{event['deadline']}）"
    elif dl == 1:
        text = f"明天截止（{event['deadline']}）"
    else:
        text = f"还剩 {dl} 天（{event['deadline']} 截止），已进入 {window} 天提醒窗口"

    return {"kind": kind, "window_days": window, "days_left": dl, "text": text}


# ---------------------------------------------------------------------------
# 单条推送
# ---------------------------------------------------------------------------

def _change_text(event):
    """变更说清"变成了什么"。`added` 没有 `change`。"""
    change = event.get("change") or {}
    kind = event.get("type")

    if kind == "deadline_changed":
        frm, to = change.get("from"), change.get("to")
        if to and frm:
            return f"截止日从 {frm} 改成 {to}"
        if to:
            return f"之前没读到截止日，现在读到了 {to}"
        if frm:
            return f"截止日从 {frm} 变成读不到了（可能公告改了，以原文为准）"
        return "截止日变了，以原文为准"

    if kind == "attachment_added":
        names = [a.get("name") for a in (change.get("attachments") or [])
                 if a.get("name")]
        if not names:
            return "补了附件"
        head = "、".join(names[:2])
        more = f" 等 {len(names)} 个" if len(names) > 2 else ""
        return f"新增附件：{head}{more}"
    return None


def _deadline_sentence(block):
    """body 里那句话。**三态三种说法，一句都不许混。**

    `deadline_display` 对 `found` 的 `text` 只是日期本身（`"2026-10-21"`），
    单看它读不出"还剩几天"，所以这里拼完整。
    **只拼 `found`** —— 另外两态的句子已经由 §14 写好，再拼一次就会拼出第四种说法。
    """
    state = block.get("state")
    if state != credibility.FOUND:
        return block["text"]

    days = block.get("days_left")
    if days is None:
        return f"截止 {block['text']}"
    if days == 0:
        return f"今天截止（{block['text']}）"
    if days == 1:
        return f"明天截止（{block['text']}）"
    if days < 0:
        return f"已于 {block['text']} 截止（已过 {-days} 天）"
    return f"截止 {block['text']}（还剩 {days} 天）"


def _q_of(event, today):
    """四条质量/时效字段，**平铺**进推送项（和列表项同名字、同含义）。

    平铺而不是嵌在 `quality: {...}` 里：C 的卡片渲染在列表和推送两处共用一份
    逻辑，两处形状不同就得写两套 —— 而这个项目已经因为"同一个字段两个出口"
    栽过（`field_trust` 在列表是摘要、详情是完整三态）。
    """
    return quality.of(event, today)


def build_item(event, user_id, profile, today, plan_active):
    """一条推送。**字段顺序就是 fixture 的顺序**（门禁逐字段比对，别乱动）。"""
    block = deadline_block(event, today)
    change_text = _change_text(event)
    because = match_because(event, profile)
    # 四维打分：`dims` 进响应（排序依据的口径），`detail` 只用来拼 `why`。
    dims, detail = score_dims(event, profile, today)
    # 负向维度只在排序里乘，不改 `score_dims`（那是"对口程度"，是正向的）。
    misfit = misfit_of(event, profile)
    kind = event.get("notice_kind")

    label = _TYPE_LABEL.get(event.get("type"), "公告变化")
    school = event.get("school") or ""
    title = event.get("title") or ""
    headline = f"【{label}】{school}：{title}" if school else f"【{label}】{title}"

    parts = []
    if event.get("unit"):
        parts.append(event["unit"])
    if event.get("publish_date"):
        parts.append(f"{event['publish_date']} 发布")
    parts.append(_deadline_sentence(block))
    body_text = " · ".join(parts)
    if change_text:
        body_text = change_text + " · " + body_text

    item = {
        "push_id": push_id_of(user_id, event.get("event_id") or ""),
        "event_id": event.get("event_id"),
        "type": event.get("type"),
        "notice_id": event.get("notice_id"),
        "title": title,
        "school": school,
        "unit": event.get("unit"),
        "url": event.get("url"),
        "at": event.get("at"),
        "detected_on": event.get("detected_on"),
        # **公告的发布日**，和 `detected_on`（我们检测到的日子）是两件事。
        # 加它是因为验收要能机器判断"这条是不是旧公告"（issue 008 的验收标准是
        # "items 里没有早于 30 天前的"）—— 只靠 body 里那句"2026-10-13 发布"
        # 是字符串，验不了。C 目前不读它，加了不破坏渲染。
        "publish_date": event.get("publish_date"),
        "relevance": event.get("relevance"),
        # **这条是补抓到的吗**（B22）：60 天窗口内的 announcement 即使
        # `is_backfill=true` 也会进首屏（9 月发布、10 月底截止的推免办法，
        # 今天仍然有效）。前端据此可以说"补录"，而不是硬说"今天新发"。
        "is_backfill": event.get("is_backfill"),
        # ---- `notice_kind`（A29，契约 §18.9）----
        # 四档：announcement / catalog / emba / other；**老数据是 null**。
        # 前端可以据此打标签；`tier` 已经按它分了层，这里给原始值是为了可核对
        # （"为什么这条在二级"要能点回字段，而不是只有一句 tier_reason）。
        "notice_kind": event.get("notice_kind"),
        # ---- 质量 / 主题 / 时效（契约 §19）----
        # `record_quality` / `topic` 由 `app._push_preview` 按 notice_id 关联进来
        # （事件流里没有这两项：它们是**公告**的属性，不是"发生了什么变化"的属性）。
        # `is_fresh` / `age_days` 走 `quality.of` 兜底 —— 事件里没有时效快照，
        # 所以按 `publish_date` 现算。**两者口径相同**（30 天 / 180 天）,
        # 区别只是"管道运行时刻"和"这次请求时刻"。
        **_q_of(event, today),
        "matched_because": because,
        # ---- 四维打分（契约 §18）----
        # 每维 0~1，**各自有独立含义**，前端可以分别显示/筛选。
        # 加权总分**不给** —— 理由同下面 `urgency_weight` 那段注释。
        "score_dims": dims,
        # ---- 层级（契约 §18.6）----
        # 1 = 命中学校或专业 **且是招生类**（全部进首屏）；2 = 其余（补位）。
        # 前端据此写标签，也可以只渲染 tier=1。
        "tier": tier_of(dims, event.get("topic"), kind),
        "tier_reason": tier_reason(dims, event.get("topic"), kind),
        # ---- 负向维度（契约 §18.8，B20）----
        # `weight` 是排序乘数，`keys` 是命中了哪几维，`text` 进 `why`。
        # **每条都有这三个键**（没命中时 weight=1.0 / keys=[] / text=null），
        # 前端不用先判断字段在不在。
        "misfit": misfit,
        # **`why` —— 给人看的完整理由**。和 `matched_because` 的分工：
        # 后者是"够不够格推"的判据（空 = 不推），这里是"为什么排在这儿"。
        # 一条推送至少有一条理由（`explain` 保证），门禁盯着。
        "why": None,          # 占位，下面填（它要用到 `item["deadline"]`）
        "deadline": block,
        # **紧急度权重**（2026-10-17）：1.0~2.0，已截止 0。
        #
        # 为什么给这个数而不给 `sort_score`：
        # `sort_score`（`relevance × urgency_weight`）是**排序的实现细节** ——
        # 把它暴露出去，前端就会有人拿它当"这条有多重要"来显示，
        # 然后我在后端调权重（比如把 T-14 从 1.5 改成 1.4）时，屏幕上那个数就变了。
        # 那是把内部排序公式变成对外承诺。
        #
        # `urgency_weight` 不同：它有**独立的产品含义**（"离截止还有多急"），
        # 前端可以据此显示"7 天内""还早"，而不依赖具体公式。
        "urgency_weight": urgency_weight({"deadline": block}),
        "reminder": reminder_block(event, today, plan_active),
        "change": event.get("change"),
        "channel": "preview",
        "headline": headline,
        "body": body_text,
    }
    item["why"] = explain(item, detail)
    return item


def is_expired(item):
    """这条推送的公告**已经截止**了吗？

    只有 `found` 且 `days_left < 0` 才算。`unknown` / `absent` **一律不算**：
    `absent` 是"公告没公布截止日"，`unknown` 是"我们没读到" ——
    把它们归进"已截止"是**编造一个我们没有的事实**（和 §14 那条不变量同一类）。
    """
    d = item.get("deadline") or {}
    dl = d.get("days_left")
    return d.get("state") == credibility.FOUND and dl is not None and dl < 0


def urgency_weight(item):
    """截止日的**紧急度权重**：`1.0 ~ 2.0`，已截止是 `0`。

    | 距截止 | 权重 | 意思 |
    |---|---|---|
    | 已截止（`days_left < 0`） | **0.0** | **但不用来过滤**（见下） |
    | 今天截止（`0`） | **2.0** | 最紧急 |
    | `1~7` 天 | 2.0 | T-7 高优 |
    | `8~14` 天 | 1.5 | T-14 中优 |
    | `15~30` 天 | 1.2 | T-30 略优 |
    | `> 30` 天 | 1.0 | 正常 |
    | `absent` / `unknown` | **1.0** | **不惩罚** —— 我们不知道，不代表它不急 |

    ## 两个刻意的决定

    **① 已截止的权重是 0，但"0"只参与排序，不参与过滤。**

    派单要求"过滤掉已过期的（urgency=0）"。**没照做**，理由是上一轮刚定的：
    已截止的**沉底不压掉** —— 截止日可能抽错、学校会延期、补发的附件截止后照样有人要。
    **排序错了只是某条低几行，压错了用户永远不知道它存在。**

    所以这里的 `0` 是"排序上排最后"，不是"不返回"。要真的不返回，那是产品决定，
    得先把 `is_expired` 的沉底逻辑和契约 §16.5 一起改回来 —— 不能悄悄改一处。

    **② `absent` / `unknown` 给 1.0，不给 0。**

    `unknown` 是"我们没读到截止日" —— **它可能下周就截止**。
    给它 0 等于把"我们不知道"当成"不用急"，那正是这套三态要消灭的混淆。
    """
    d = item.get("deadline") or {}
    if d.get("state") != credibility.FOUND:
        return 1.0                      # absent / unknown：不惩罚
    dl = d.get("days_left")
    if dl is None:
        return 1.0                      # 读到了状态但没有距今天数，不猜
    if dl < 0:
        return 0.0                      # 已截止（排序沉底，不压掉）
    if dl <= 7:
        return 2.0                      # 含"今天截止"
    if dl <= 14:
        return 1.5
    if dl <= 30:
        return 1.2
    return 1.0


def _sort_key(item):
    """排序：`tier` → 未截止在前（已截止沉底）→
    `weighted_score(score_dims) × urgency_weight × admit(relevance) × misfit.weight`
    降序 → 发布日降序 → `at` 降序。

    ## 2026-10-19：`tier` 提到 `已截止` 前面（B22）

    原来第一层是"未截止/已截止"，`tier` 在第二层。于是**一条已截止的一级公告
    会沉到二级（其他相关）后面**，屏幕上出现"其他相关"夹在招生通知中间 ——
    和"一级整层在前"的承诺矛盾。

    现在 `tier` 最前：一级整层在前，已截止**只在它自己那一层里**沉底。
    这没有推翻"已截止沉底不压掉"（契约 §16.5）：它仍然排在同样一级的
    未截止公告之后，也仍然一条都不删。

    ## 2026-10-19 加了 `misfit`（B20，契约 §18.8）

    QA 实测：物理 + 直博的画像，首屏第 1 条是北大的 **EMBA** 简章、
    第 2 条是**报考点公告**，真正的招生简章排到第 3。
    四维全是正向的（学校命中就加分），"这个项目你根本报不了"在分数里
    没有任何体现 —— 所以加一个**负向乘数**，而不是去调四维权重
    （调权重只会让"学校和专业都命中"的招生简章也一起被压低）。

    ## 2026-10-18 换掉了 `relevance × urgency_weight`

    原来乘的是数据层的 `relevance`（0/1/2，"这条像不像招生信息"）。
    问题是它衡量的是**公告本身**，不是**这条公告对这个人**：
    实测 u_num_demo 的 20 条推送里，`relevance=2` 的占了绝大多数，
    于是排序实际上只剩 `urgency_weight` 一个变量在起作用，
    "对口"这件事根本没进排序。改成四维加权后，
    学校/专业/方向命中真的会改变名次，`why` 里也能看见是哪一维。

    ## 为什么乘而不是加

    `weighted_score` 是 0~1 的**对口程度**，`urgency_weight` 是
    1.0~2.0 的**紧急程度**。两者是不同维度：乘法表示"越对口、越紧急的越靠前"，
    而加法会让"极不对口但极紧急"的排到"很对口但还早"的前面 —— 那不是用户要的。

    实测这个乘法的效果（占位示意，权重见 `config.PUSH_SCORE_WEIGHTS`）：

    ```
    1.00 × 2.0 = 2.00   三校都对口 + 7 天内截止
    0.80 × 2.0 = 1.60   对口但少一维
    1.00 × 1.0 = 1.00   完全对口但还早
    0.35 × 2.0 = 0.70   只命中学校却马上截止   ← 不会因为紧急插到最前面
    ```

    **低对口的不会因为紧急插队** —— 这是乘法在这里唯一的作用。

    ## 为什么是"沉底"而不是"压掉"

    已截止的公告对"今天值得打扰你"这件事价值很低 —— 但它**不是零**：

    1. **截止日可能抽错。** 这个项目有过"把正文里的日期当成发布日"的实例，
       也有过噪声闸误杀正确截止日。抽错一条，压掉就是**让用户彻底看不到它**，
       而那条其实还能报名。
    2. **学校会延长。** 延期、春季批次、补报名都存在过 ——
       一条"已截止"的公告改了截止日，就是最有价值的推送。
    3. **附件可能还有用。** 补发的模板、表格，截止之后照样有人要。

    而项目的头号原则是 **"只排序、不隐藏"** ——
    已截止是同样的问题：**排序错了只是某条低几行，压错了用户永远不知道它存在**。

    ## 为什么 `unknown` / `absent` 不沉底

    见 `is_expired`：我们不知道它们截止了没。把它们放进"已截止"组是编造，
    放进"未截止"组也是编造 —— 所以**位置不动**（它们本来就在中间，
    对口程度该多高就多高）。

    `days_left == 0`（今天截止）**不沉底**：它是最紧急的一条，沉底就反了。
    """
    score = weighted_score(item.get("score_dims") or {})
    w = item.get("urgency_weight")
    w = 1.0 if w is None else w
    # `relevance == 0` 是**准入**判定，不是排序维度 —— 在这里继续当乘数用，
    # 语义是"数据层判定这条根本不是招生信息"，所以四维再高也不该冒头。
    #
    # 为什么不让它参与四维打分：四维衡量的是"这条公告对你有多对口"，
    # 而 `relevance` 衡量的是"这条公告像不像招生信息"。前者是**这个人**的问题，
    # 后者是**这条公告**的属性。把两件事塞进同一个加权和，结果是
    # "选课 FAQ 因为发得新、学校对，排到了招生简章前面"。
    #
    # `relevance is None`（老事件没有这个键）**不惩罚**：不知道 ≠ 不相关。
    rel = item.get("relevance")
    admit = 0.0 if rel == 0 else 1.0
    # **负向维度**（契约 §18.8）：EMBA/在职/非全日制、报考点/考场这类
    # 明显不对口的，乘一个 <1 的系数。它不是"对口程度"的一维 ——
    # 对口程度回答"这条和你多相关"，负向维度回答"这条你根本报不了/不需要"，
    # 两者分开，`score_dims` 才不会变成一个含义混杂的和。
    misfit = (item.get("misfit") or {}).get("weight")
    misfit = 1.0 if misfit is None else misfit
    # **`tier` 排在分数之前**（契约 §18.6）：
    # 一级记录（命中学校或专业）必须**全部**在一屏之内，二级只补位。
    # 只靠权重做不到这一点 —— `direction_hit × 紧急度` 可以压过
    # `school_hit × 不紧急`，于是二级会插到一级前面。
    # 把 tier 提到 score 之前，是**结构上保证**"一级不会因为不够急而被挤掉"，
    # 而不是靠调参数碰运气。
    tier = item.get("tier")
    tier = 2 if tier is None else tier
    # 同分时的先后：**先看发布日，再看事件时间**（2026-10-19 B18）。
    # 为什么改：/api/match 排的是公告（只有发布日），/api/push/preview 排的是事件
    # （有 t）。同分时一个按发布日、一个按事件时间，两条路径就会给出不同的前 5 条 ——
    # 而用户看到的是同一个画像，两个页面说不一样的答案。
    # 发布日**用户看得懂**（页面上就显示它），事件时间他看不到。
    # **`tier` 排在最前，`已截止` 排第二**（2026-10-19 换位，B22）：
    # 一级（招生通知）必须**整层**在二级（其他相关）之前 —— 否则一条
    # "已截止的一级"会沉到二级后面，屏幕上就会出现"其他相关"夹在
    # 招生通知中间。已截止仍然沉底，但只在**它自己那一层里**沉底。
    return (tier,
            1 if is_expired(item) else 0,
            -(score * w * admit * misfit),
            item.get("publish_date") or "",
            item.get("at") or "", item.get("event_id") or "")


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def _suppressed(item, reason):
    """被压掉的一条。**只给够核对的字段**：用户看不出"哪条被压了"，
    只给一个数字是等于没解释。"""
    return {
        "event_id": item.get("event_id"),
        "notice_id": item.get("notice_id"),
        "title": item.get("title"),
        "reason": reason,
        "text": _SUPPRESSED_TEXT.get(reason, "这条没有推给你"),
    }


# `empty_reason` 的取值（契约 §16.4）+ 给人看的话。
# **六态**：`no_profile` / `no_events_file` / `stale_events` / `no_match` /
# `only_backfill` / `null`。
EMPTY_TEXT = {
    "no_profile": "你还没填能参与匹配的画像 —— 填了「意向学校 / 意向方向」才会开始推。",
    "no_events_file": ("今天没有新公告：事件文件还没被写出来。"
                       "这不是坏了，也不是「没有变化」 —— 我们没观测到任何东西。"),
    # **数据太旧、且什么都没匹配到。** 这时"没有跟你相关的"也说不出口 ——
    # 我们根本不知道今天有什么，因为管道几天没跑。
    "stale_events": ("抓取管道几天没跑了，所以「今天有没有新公告」我们**不知道**。"
                     "这不是「没有」—— 是「我们还没去看」。"),
    "no_match": "今天有事件，但没有一条跟你的画像有交集。画像填窄了就不会有推送。",
    # **补抓的旧公告不算新闻**（issue 008）。这一态要单独说出来：
    # 说成 `no_match` 会让用户去改画像（而画像没问题），说成"没有变化"是撒谎。
    "only_backfill": ("今天匹配到的都是我们**补抓**时翻出来的历史公告，不是新发的。"
                      "它们在你的公告列表里能翻到，但不算「今天发生了什么」。"),
    # **今天没有招生类的新东西**（B15）。这一态必须独立，不能混进 `no_match`：
    # `no_match` 是"跟你没交集"（用户该去改画像），而这里是"有交集、但都不是招生公告"
    # （用户改画像也没用）。文案由 `no_apply_text()` 现算，带上"最近一条是几号的"。
    "no_apply_today": None,
}


def no_apply_text(newest):
    """`no_apply_today` 的那句话。**带上"最近一条是几号的"** —— 空的页面也要有信息量。

    `newest` 是 `{"publish_date": "2026-10-16", "title": "…"}`（命中了画像的最新 apply 事件），
    没有就是 `None`。
    """
    head = "今天你关注的学院没有新的招生公告。"
    if not newest:
        return head + "往前翻也没有匹配到招生类公告 —— 可以把意向学校放宽一两所试试。"
    d = str(newest.get("publish_date") or "")[:10]
    title = (newest.get("title") or "").strip()
    if len(title) > 26:
        title = title[:26] + "…"
    try:
        from datetime import date as _date
        days = (_date.today() - _date.fromisoformat(d)).days
        ago = "%d 天" % days if days >= 1 else "今天"
    except ValueError:
        ago = "之前"
    return "%s最近一条是 %s 的《%s》，已经过去 %s。" % (head, d, title, ago)


def empty_text_for(events_empty_reason):
    """给前端的一句人话。**六态六句，别统一写「暂无推送」**。"""
    return EMPTY_TEXT.get(events_empty_reason)


def assemble(*, events, profile, completeness, user_id, plan, plan_active,
             today, limit, source_meta, state=None, commit=False,
             suppressed_mode="capped"):
    """组装 `/api/push/preview` 的响应。

    `commit=True` 时才**记录**（去重 + 限频）；
    `commit=False`（GET）纯粹是"给你看会是什么"，幂等 —— 刷新十次都一样。

    ## 为什么 GET 不许有副作用

    预览是**看一眼**。如果看一眼就消耗掉一条推送，用户刷新一次页面
    就会看到"你已经收到过了"，那是把一个展示行为当成了消费行为。
    所以 GET 只读，`POST /api/push/preview` 才是"接收这批推送"。
    """
    limit = max(1, min(int(limit or config.MAX_PUSH_LIMIT), config.MAX_PUSH_LIMIT))

    # ---- ① 匹配 ----
    #
    # ⚠️ **`is_backfill` 只对 `added` 会有值**（数据层的契约：`deadline_changed` /
    # `attachment_added` 永远不标）。所以这里**不能按 `type` 一刀切**，
    # 必须看事件自己的字段 —— 再老的公告改了截止日，也该报。
    #
    # 判据是"事件说自己是补抓的"，不是"发布日早于 30 天"：
    # 30 天那个口径是**数据层判定 is_backfill 用的**，接口层不要自己再判一遍。
    # 两处各判一次，迟早一处说真一处说假，而那时没人知道该信哪个。
    matched, unmatched, backfilled = [], 0, []
    junk, junk_dropped = [], 0
    not_apply, topic_unknown = [], 0
    mode_mismatch = []
    outdated_year = []
    # ---- `notice_kind` 分层（B22）----
    #
    # `catalog`（目录/简章）**不进首屏**：它们随时可查，占着首屏等于把
    # "今天该办的事"挤掉。事件流里的这条要进 `suppressed`（reason=catalog），
    # 说清它去哪了 —— 参考资料区是按公告库现算的，不是从事件流来的。
    #
    # `emba`（EMBA/MBA/在职/非全日制）：对**学术型/直博**画像是硬排除；
    # 对明确要专硕/管理类的画像，留在二级（不是今天该办的，但人家真在找）。
    catalog_events, emba_excluded, out_of_window = [], [], []
    newest_apply = None            # 命中了画像的、最新的一条 apply（给空态文案用）
    for e in events:
        # ---- ①' 拒收脏记录（契约 §19.2）----
        #
        # `record_quality` 由数据层给（`collector/quality.py`），接口层只读不判。
        # **必须在 `build_item` 之前拦**：junk 记录推送出去是一张没有标题、
        # 正文只有"发布时间/点击数"的卡 —— 用户会当成我们抽不出内容。
        #
        # 但**不能悄悄丢**：它们进 `suppressed`（reason=`junk`）并计入 `tally`，
        # 否则"今天怎么只有 13 条"就没法解释了（§7.4：把"被拒绝"说成"没有数据"）。
        if quality.is_junk(e):
            item = build_item(e, user_id, profile, today, plan_active)
            junk.append(item)
            junk_dropped += 1
            continue
        item = build_item(e, user_id, profile, today, plan_active)
        if not item["matched_because"]:
            unmatched += 1
            continue
        # ---- ①'' **招生类是硬门槛**（契约 §18.7，B15）----
        #
        # 2026-10-19 实测：浙大物理 + 北大/清华/中科大 的画像，`/api/push/preview`
        # 返回 3 条 —— 博士后奖通知、TeV 研讨会、物院论坛，**一条招生公告都没有**，
        # 因为 tier2 兜底时没有 `topic` 门槛，于是拿讲座/会议凑数。
        #
        # 现在：`topic == "apply"` 是**必要条件**，tier1 / tier2 都必须满足。
        # **宁可空，不许凑** —— 空的时候用 `no_apply_today` 说实话，而不是给三条讲座。
        topic = e.get("topic")
        if topic is None:
            # 老事件没有这个字段。**不能当成"不是招生"**（不知道 ≠ 不是），
            # 但也不能放进首屏 —— 所以单独计数，见 `tally.topic_unknown`。
            topic_unknown += 1
            not_apply.append(_suppressed(item, "topic_unknown"))
            continue
        if topic != "apply":
            not_apply.append(_suppressed(item, "not_apply"))
            continue
        # ---- ①''' 招生方式必须对得上（B18）----
        #
        # `wanted_modes=["推免"]` 的人，**不该收到"统考"的公告**。
        # 这条匹配那边一直有，推送这边漏了 —— 于是同一个画像，
        # `/api/match` 把它过滤掉、`/api/push/preview` 照推（实测第 2 条就是）。
        # 两条路径的硬过滤必须同一套，否则用户不知道该信谁。
        #
        # `apply_mode` 为 None（抽不到）**不丢** —— 和匹配那边同一条规矩：
        # 不知道就不拦，宁可多推一条。
        mode = e.get("apply_mode")
        # 标题证据优先：正文里只要出现"推免"二字，抽取就会把统考公告标成推免。
        # 标题写着"报考点/网上报名/全国硕士研究生招生考试"的，按统考处理。
        if _is_exam_transactional(e):
            mode = "统考"
        wanted_modes = (profile or {}).get("wanted_modes") or []
        if mode and wanted_modes and mode not in wanted_modes:
            mode_mismatch.append(_suppressed(item, "mode_mismatch"))
            continue
        # 往届通知：招生年份早于目标入学年 → 对这个人没有意义（「2025 级选拔」给 2027 考生）。
        # 只在**两边都明确**时判；抽不到年份就不拦（不知道 ≠ 不适合）。
        entry_year = (profile or {}).get("target_entry_year")
        ev_year = e.get("target_year")
        if isinstance(entry_year, int) and isinstance(ev_year, int) and ev_year < entry_year:
            outdated_year.append(_suppressed(item, "outdated_year"))
            continue
        # 标题明写"报考点/网上报名"的统考事务公告：抽出的 apply_mode 可能错，
        # 但标题不会 —— 用标题修正后再走上面同一套 mode_mismatch 判断。
        if wanted_modes and "统考" not in wanted_modes and _is_exam_transactional(e) \
                and (not mode or mode == "统考"):
            mode_mismatch.append(_suppressed(item, "mode_mismatch"))
            continue
        # 命中了画像的 apply 事件：记下最新的那条（空态要说"最近一条是几号的"）
        pd = (e.get("publish_date") or "").strip()
        if pd and (newest_apply is None or pd > newest_apply["publish_date"]):
            newest_apply = {"publish_date": pd, "title": e.get("title") or ""}
        # ---- ①'''' 分层：目录/简章 ≠ 通告（B22，契约 §18.9）----
        kind = e.get("notice_kind")
        if kind == KIND_CATALOG:
            catalog_events.append(_suppressed(item, "catalog"))
            continue
        if kind == KIND_EMBA and not wants_professional_degree(profile):
            emba_excluded.append(_suppressed(item, "emba"))
            continue
        # ---- ①''''' 首屏只看窗口内（近 60 天）的公告（B22）----
        #
        # 不加这一条，一条 **178 天前**的《2026 年推免简章》会出现在首屏 ——
        # 实测真实数据里就有（`added` 事件不一定是补抓，数据层只按 30 天标
        # `is_backfill`，而"什么时候入库的"和"这条还值不值得看"是两件事）。
        #
        # 对外说的是"近 60 天"，首屏就必须按同一个窗口筛；否则页头的数
        # 和屏幕上的条目永远对不上，而且用户会看到去年的简章。
        # **所有类型都过这道门**：目录/在职已在上面被移走，剩下的一级/二级
        # 也不该出现去年的东西。
        if not within_window(e, today):
            out_of_window.append(_suppressed(item, "out_of_window"))
            continue
        # ---- ①''''' 窗口内的招生通知**不受 is_backfill 拦截**（B22）----
        #
        # `is_backfill` 的口径是"发布超过 30 天"（数据层给的，用来防止
        # 2018 年的旧公告被当成「新公告」推出去）。但推免季的公告 9 月发、
        # 10 月底才截止 —— 45 天前发布的推免办法**今天仍然有效**。
        #
        # 用户 2026-10-19 拍板：把窗口放到 60 天，窗口内的 announcement
        # 一律进首屏（**只放开 announcement，`other` 仍按原规则**）——
        # 否则"放宽窗口"只会改页头那个数，首屏一条也不会多。
        if e.get("is_backfill") and not (
                kind == KIND_ANNOUNCEMENT and within_window(e, today)):
            backfilled.append(item)
            continue
        matched.append(item)
    matched.sort(key=_sort_key)
    matched_hits = len(matched) + len(backfilled)      # 命中画像的总数（含补抓）

    suppressed = [_suppressed(i, "backfill") for i in backfilled]
    suppressed.extend(_suppressed(i, "junk") for i in junk)
    suppressed.extend(not_apply)
    suppressed.extend(mode_mismatch)
    suppressed.extend(outdated_year)
    suppressed.extend(catalog_events)
    suppressed.extend(emba_excluded)
    suppressed.extend(out_of_window)

    # ---- ①' 同一批里按 notice_id 合并 ----
    #
    # 同一条公告会先后产生 `added` / `deadline_changed` / `attachment_added` 三种事件。
    # **跨批次**它们是三件事，各推一次是对的（时钟往前走，每次都带来新信息）；
    # 但**同一批里**两条并列出现在界面上，看起来就是"重复推送"。
    #
    # 留信息量大的那条（`_TYPE_PRIORITY`），被合并掉的**进 suppressed（merged）**——
    # 直接丢掉的话，`tally.matched` 和 `sent` 会对不上，用户会以为漏推了。
    by_notice, merged_away = {}, []
    for item in matched:
        nid = item.get("notice_id")
        if not nid:
            continue
        cur = by_notice.get(nid)
        if cur is None:
            by_notice[nid] = item
        elif _TYPE_PRIORITY.get(item.get("type"), 0) > _TYPE_PRIORITY.get(cur.get("type"), 0):
            merged_away.append(cur)
            by_notice[nid] = item
        else:
            merged_away.append(item)
    if merged_away:
        keep = {id(v) for v in by_notice.values()}
        merged_away = [i for i in merged_away if id(i) not in keep]
        matched = [i for i in matched if id(i) in keep]
        suppressed.extend(_suppressed(i, "merged") for i in merged_away)

    # ---- ② 去重 ----
    #
    # **这里不再有"过期就压掉"那一步。** 已截止的公告改为**排序沉底**（见 `_sort_key`），
    # 理由写在那里：压掉等于"我们替你决定这条没用"，而截止日抽错、学校延期、
    # 补发附件这三种情形都真实存在过。**排序错了只是低几行，压错了是永久看不到。**
    seen = state.push_seen_ids(user_id) if state is not None else set()
    fresh = []
    for item in matched:
        if item["push_id"] in seen:
            suppressed.append(_suppressed(item, "duplicate"))
            continue
        fresh.append(item)

    # ---- ③ 免费层截断（在校验预算之前）----
    #
    # **免费层故意给"前 3 条"而不是 0 条**：一个只有付费才能看到的推送，
    # 用户在购买前无法判断它值不值 —— 那不是付费墙，是赌博。
    truncated_reason = None
    if not plan_active and len(fresh) > config.PUSH_FREE_LIMIT:
        fresh = fresh[:config.PUSH_FREE_LIMIT]
        truncated_reason = "free"

    # ---- ④ 限频 ----
    used_today = state.push_count_today(user_id) if state is not None else 0
    budget = max(0, config.PUSH_DAILY_LIMIT - used_today)
    if len(fresh) > budget:
        for item in fresh[budget:]:
            suppressed.append(_suppressed(item, "rate_limited"))
        fresh = fresh[:budget]
        truncated_reason = truncated_reason or "limit"

    # ---- ⑤ limit（`?limit=` 是页面一次要几条，不是"总共几条"）----
    if len(fresh) > limit:
        fresh = fresh[:limit]
        truncated_reason = truncated_reason or "limit"

    # ---- ⑥ 提交（只有 POST 会走到这里）----
    if commit and state is not None and fresh:
        state.record_pushes(user_id, fresh)

    # ---- ⑦ 空的原因 ----
    #
    # **只给一个主因**（`empty_reason`），但把所有成立的原因按优先级收进
    # `empty_reasons`（详情字段）。为什么两个都要：
    #
    # - 前端只要一句话，平铺三条它没法渲染，用户也不知道该先做什么；
    # - 但排查时要知道"还有哪些原因同时也成立" —— 例如数据过期 + 没匹配，
    #   两个都真，只报一个会让人以为另一个不存在。
    #
    # **优先级 = 用户能解决的优先**：
    #   no_profile（他能填）> no_events_file（我们能修）> stale_events（我们能修）
    #   > no_match（他能放宽画像）> only_backfill
    # 一个用户看到"没有推送"，必须能立刻分清"该我动手"还是"该等我们"。
    reasons = []
    if not completeness.get("can_push"):
        reasons.append("no_profile")
    if source_meta.get("kind") == "none":
        reasons.append("no_events_file")
    if not matched_hits and source_meta.get("kind") != "none":
        # 数据太旧 + 什么都没匹配到：这时既不能说"没有跟你相关的"（我们不知道
        # 今天有什么），也不能说"数据还没跑"（它跑过，只是几天前）。只能说实话。
        if source_meta.get("stale"):
            reasons.append("stale_events")
        elif not_apply:
            # **有交集、但都不是招生公告**（B15）。用户改画像也没用 ——
            # 所以不能说 `no_match`（那会让他去改画像，而画像没问题）。
            reasons.append("no_apply_today")
        else:
            reasons.append("no_match")
    if matched_hits and not fresh and backfilled:
        # 匹配上了但全是补抓的旧公告：不能说"今天没有跟你相关的变化"，
        # 也不能不说（用户会以为画像填窄了）。
        reasons.append("only_backfill")
    reasons = list(dict.fromkeys(reasons))     # 去重保序
    empty_reason = reasons[0] if (not fresh) and reasons else None

    # ---- 被压掉的：按原因分组取样（契约 §16.8）----
    #
    # **`suppressed` 只留每个原因的前 N 条**（默认 3）。顺序仍按原来的产生顺序，
    # 这样前端那句"压掉了却不说为什么"的检查不用改。
    per_reason = config.PUSH_SUPPRESSED_SAMPLE_PER_REASON
    suppressed_tally = {}
    suppressed_sample = {}
    seen_per_reason = {}
    suppressed_capped = []
    for x in suppressed:
        r = x["reason"]
        suppressed_tally[r] = suppressed_tally.get(r, 0) + 1
        bucket = suppressed_sample.setdefault(r, [])
        if len(bucket) < per_reason:
            bucket.append(x)
        n = seen_per_reason.get(r, 0)
        if n < per_reason:
            suppressed_capped.append(x)
            seen_per_reason[r] = n + 1

    return {
        "user_id": user_id,
        "generated_at": _now_iso(),
        "plan": plan,
        "profile": {
            "can_push": bool(completeness.get("can_push")),
            "filled": bool(completeness.get("filled")),
            "missing": completeness.get("missing") or [],
            "text": completeness.get("message") or "",
        },
        "source": source_meta,
        "empty_reason": empty_reason,
        "empty_text": (no_apply_text(newest_apply) if empty_reason == "no_apply_today"
                       else empty_text_for(empty_reason)),
        # 所有成立的原因，按 "用户能解决的优先" 排序（契约 §16.4）。
        # **`empty_reason` 只给第一个**：前端渲染一句话，不渲染三条。
        "empty_reasons": reasons,
        "items": fresh,
        # ---- 被压掉的那些：**计数 + 每种原因前 3 条样例**（契约 §16.8）----
        #
        # 2026-10-18 之前这里直接给全量 `suppressed`：2751 条数据下
        # **523 KB / 2739 条**，占整个响应的 98%，首屏要 0.5~3 秒。
        # 那是"可解释性"换来的，但**没人会去读 2739 条**，代价白付。
        #
        # 现在四个字段一起给，既保住可解释性、又把体积压到 4 KB 级：
        #   `suppressed`          —— **只含样例**（≤3/原因），保持数组形状，前端不用改
        #   `suppressed_total`    —— 真实总条数（说"另有 2739 条"用它）
        #   `suppressed_tally`    —— 按原因计数（说"其中 2103 条是补抓"用它）
        #   `suppressed_sample`   —— 按原因分组的前 3 条（"给我看看是哪几条"用它）
        # 要看全部：`GET /api/push/suppressed?reason=&offset=`
        "suppressed": suppressed if suppressed_mode == "full" else suppressed_capped,
        "suppressed_total": len(suppressed),
        "suppressed_tally": suppressed_tally,
        "suppressed_sample": suppressed_sample,
        "suppressed_truncated": (suppressed_mode != "full"
                                 and len(suppressed_capped) < len(suppressed)),
        "tally": {
            "events": len(events),
            "matched": matched_hits,
            "unmatched": unmatched,
            "backfill": len(backfilled),
            # 被 `record_quality=junk` 拒收的条数（契约 §19.2）。
            # 单独给一个数而不是只塞进 suppressed：前端要说"另有 N 条脏记录已排除"，
            # 而 `suppressed` 里还有 duplicate / merged / rate_limited，
            # 混在一条里数不出来。
            "junk_dropped": junk_dropped,
            # 命中了画像但**不是招生公告**（讲座/会议/名单/事务通知），被硬门槛挡在首屏外。
            # 单独给数：用户看到"今天 0 条"时，能分清是"没有招生类"还是"什么都没命中"。
            "not_apply": len(not_apply),
            # 老事件没有 `topic` 字段的条数 —— **不确定的东西不进首屏**，但要能被看见。
            "topic_unknown": topic_unknown,
            # 招生方式不符（画像要推免、这条是统考）被拦下的条数
            "mode_mismatch": len(mode_mismatch),
            # 招生年份早于目标入学年（往届通知）被挡在首屏外的条数
            "outdated_year": len(outdated_year),
            # ---- `notice_kind` 分层（B22，契约 §18.9）----
            # 目录/简章被挪去「参考资料」的条数（**不是丢掉**，响应里有 reference 块）；
            # 在职项目对学术型画像硬排除的条数；
            # 老事件没有 notice_kind 的条数（这些仍按旧规则分层，**降级要可见**）。
            "catalog": len(catalog_events),
            "emba_excluded": len(emba_excluded),
            # 发布超过窗口（60 天）的招生通知：不进首屏，但仍留在库里。
            "out_of_window": len(out_of_window),
            "kind_unknown": sum(1 for i in matched if i.get("notice_kind") is None),
            # 分级计数（契约 §18.6）。**首屏条数不写死**：一级有几条就是几条，
            # 上限才是 `daily_limit`。前端用 `tier1` 说"今天 N 条对口"，
            # 用 `tier2` 说"另有 M 条仅方向命中"。
            "tier1": sum(1 for i in fresh if i.get("tier") == 1),
            "tier2": sum(1 for i in fresh if i.get("tier") == 2),
            "sent": len(fresh),
            # items 里**已截止**的条数（它们在列表最后，但**没有被压掉**）。
            "expired": sum(1 for i in fresh if is_expired(i)),
            # **匹配到的**已截止条数（不管有没有进 items）。
            #
            # 为什么要这两个数（2026-10-17 补，C 报的）：
            # 已截止的**沉底**，而 `?limit=` / 每日上限会截断 ——
            # 真实数据上匹配 100 条、只给 20 条，**已截止的根本进不了那 20 条**，
            # 于是 `tally.expired` 几乎恒为 0。C 按它做的"最后 N 条已截止"
            # 永远不触发，成了死代码。
            #
            # 有这一个数，前端才能说清两种不同的沉默：
            # 「最后 0 条已截止」和「另有 8 条已截止，被每日上限截掉了」。
            "expired_matched": sum(1 for i in matched if is_expired(i)),
            "suppressed": len(suppressed),
            "limit": limit,
            "daily_limit": config.PUSH_DAILY_LIMIT,
            "used_today": used_today,
            "free_limit": config.PUSH_FREE_LIMIT,
            "truncated": bool(truncated_reason),
            "truncated_reason": truncated_reason,
            "committed": bool(commit),
        },
        "_note": ("推送只做**站内预览**，未接邮件/微信通道；"
                  "每条都带原文链接；没有支付通道，订阅用手动开通码。"),
    }


def apply_fresh_announcement_matched_count(records, profile, today, days=None):
    """同一个池子里，**命中这个画像**的有多少条（页头「其中 N 条对口你」，B23）。

    过滤条件和首屏一级同一套：announcement + 非 junk + 窗口内 + 招生方式对得上
    + 命中画像（学校/专业/方向/院系任意一个）+ 同校同标题去重。

    和首屏条数**可能不相等**：首屏是从事件流来的（今天有变化的那批），
    这里数的是公告库里的池子。两个数都如实给，不互相凑。
    """
    days = config.PUSH_POOL_FRESH_DAYS if days is None else days
    wanted_modes = (profile or {}).get("wanted_modes") or []
    seen, n = set(), 0
    for rec in records:
        if rec.get("notice_kind") != KIND_ANNOUNCEMENT:
            continue
        if rec.get("topic") != "apply":
            continue
        if quality.is_junk(rec):
            continue
        d = parse_day(rec.get("publish_date"))
        if d is None or not (0 <= (today - d).days <= days):
            continue
        mode = rec.get("apply_mode")
        if mode and wanted_modes and mode not in wanted_modes:
            continue
        if not match_because(notice_view(rec), profile):
            continue
        key = dedupe_key(rec)
        if key in seen:
            continue
        seen.add(key)
        n += 1
    return n


def _now_iso():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 第三层：参考资料（B22）+ 池子有多大（B23）
# ---------------------------------------------------------------------------

def dedupe_key(rec):
    """同标题去重的键：`(学校, 归一化标题)`。

    **只在同一学校内去重。** 不同学校发同名通知是正常的（"关于推免工作的通知"
    每校都有），跨校去重会把不同学校的内容合成一条 —— 那是丢信息，不是去重。
    """
    title = " ".join((rec.get("title") or "").split())
    return ((rec.get("school") or "").strip(), title)


def notice_view(rec):
    """公告记录 → `match_because` 认识的事件形状（和 `match.event_view` 同一思路）。"""
    return {"title": rec.get("title"), "school": rec.get("school"),
            "unit": rec.get("unit"), "matched": list(rec.get("majors") or [])}


def reference_of(records, profile, today, limit=None):
    """折叠的「参考资料」区（契约 §18.9 / B22 第三层）。

    **从公告库现算，不从事件流来。** 目录/简章的价值不是"今天变了"，
    而是"随时可查" —— 从事件流取会漏掉 90% 的目录（它们今年没变过）。

    过滤条件和首屏同一套：命中了画像、非 junk、招生方式对得上；
    再按 `(学校, 标题)` 去重（同校同名目录只留一条）。
    """
    limit = config.PUSH_REFERENCE_LIMIT if limit is None else limit
    wanted_modes = (profile or {}).get("wanted_modes") or []
    picked, seen = [], set()
    for rec in records:
        if rec.get("notice_kind") != KIND_CATALOG:
            continue
        if quality.is_junk(rec):
            continue
        mode = rec.get("apply_mode")
        if mode and wanted_modes and mode not in wanted_modes:
            continue
        because = match_because(notice_view(rec), profile)
        if not because:
            continue
        key = dedupe_key(rec)
        if key in seen:
            continue
        seen.add(key)
        picked.append({
            "notice_id": rec.get("id"),
            "title": rec.get("title"),
            "school": rec.get("school"),
            "unit": rec.get("unit"),
            "publish_date": rec.get("publish_date"),
            "url": rec.get("url"),
            "notice_kind": KIND_CATALOG,
            "matched_because": because,
        })
    # 新的在前；同一天的按标题，保证顺序确定。
    picked.sort(key=lambda x: (x.get("publish_date") or "", x.get("title") or ""),
                reverse=True)
    return {
        "total": len(picked),
        "items": picked[:limit],
        "truncated": len(picked) > limit,
        "_note": ("「参考资料」：招生目录 / 简章一类，随时可查，"
                  "和首屏的『今天该办的事』分开。不占推送额度。"),
    }


def apply_fresh_announcement_count(records, today, days=None):
    """近 N 天 + `announcement` + 非 junk + **同校同标题去重**后有多少条（B23）。

    这就是演示时那句：**"我们从 2751 条里筛出 15 条这周值得看的招生通知。"**
    —— 分母是库里的全部公告，不是推送条数；用户看到的 10 条只是"你的"。

    **发布日读不到的记录不算。** 这条数是要对外说的（"近 30 天"），
    把不知道发布日的算进去，就变成"我们猜它在 30 天内"。
    """
    days = config.PUSH_POOL_FRESH_DAYS if days is None else days
    seen, n = set(), 0
    for rec in records:
        if rec.get("notice_kind") != KIND_ANNOUNCEMENT:
            continue
        if rec.get("topic") != "apply":
            continue
        if quality.is_junk(rec):
            continue
        d = parse_day(rec.get("publish_date"))
        if d is None:
            continue
        age = (today - d).days
        if age < 0 or age > days:
            continue
        key = dedupe_key(rec)
        if key in seen:
            continue
        seen.add(key)
        n += 1
    return n

# -*- coding: utf-8 -*-
"""用真实数据生成契约样例（fixtures），让前端和后端可以**同时开工、互不等待**。

为什么要它：C 如果等 B 把接口写完再动手，两个人就串行了，四天变两天。
有了 fixtures，C 可以对着固定形状的 JSON 写完整前端，B 上线后只换一个 base url。
而 fixtures 由真实数据生成、可随时重跑，不会像手写的 mock 那样很快和现实脱节。

用法（在 collector 目录下）：
    python tools/make_fixtures.py

归属：A。产出 contracts/fixtures/ 下的三个文件。
契约以 contracts/api.md 为准；本工具只负责"按契约把真实数据切出来"。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import common              # noqa: E402
import match_preview as MP  # noqa: E402

# **直接调 B 的模块，不重实现一遍**（2026-10-17）。
#
# `field_trust` 是 `/api/match` 的 item 字段，它的唯一来源是
# `server/credibility.py` 的 `untrusted_summary()`。
# 如果在生成器里再写一份，就是"同一个形状写在两个地方"——这个项目栽过 4 次，
# 每次都是改了副本没改原件。
#
# **能这么做的原因**：`credibility.py` 只依赖标准库（没有 import 任何东西），
# 所以 collector 的工具 import 它不需要 fastapi、也不引入循环依赖。
sys.path.insert(0, common.ROOT)
try:
    from server import credibility as _cred            # noqa: E402
except ImportError:                                     # 纯数据环境里跑时退化
    _cred = None

FIX = os.path.join(common.ROOT, "contracts", "fixtures")
NOTICES = os.path.join(common.OUT_DIR, "notices.jsonl")


def _field_trust(rec):
    """调 B 的实现；拿不到就**明确报错**，不静默产出少一个键的 fixture。"""
    if _cred is None:
        raise RuntimeError(
            "make_fixtures 需要 server/credibility.py 来算 field_trust，"
            "但它 import 不到 —— 别在这里重写一份，去修 sys.path")
    return _cred.untrusted_summary(rec)

# 与 server/billing.py 的 DEMO_NOTE 保持一致。
# **响应 fixture 里的 `_note` 必须和 B 的服务实际返回的一字不差**，
# 因为他的断言是 got["_note"] == fx["_note"] —— 这是"fixture 即响应"的直接体现。
DEMO_NOTE = "D4 演示用模拟支付，不接真实支付通道（学生拿不到微信支付商户号）"


def load_profile():
    """画像文件是人手改的，Windows 记事本会加 BOM —— 必须用 utf-8-sig。"""
    with open(os.path.join(HERE, "profile-example.json"), encoding="utf-8-sig") as f:
        return json.load(f)


def clean_profile(p):
    """画像里的 `_` 前缀键是**给人看的注释，不是接口字段**，不进响应。

    坑（B 报的）：`profile-example.json` 原来第一个键是 `_comment`，
    而匹配响应会把 profile 原样回显 —— 于是 100 多字的说明文字进了契约样例，
    每个响应都白扛一遍，而且 C 照 fixture 写 TS 类型时会把它当成画像的正式字段。

    统一约定：**任何 `_` 开头的键都非契约**（见 api.md）。
    这样以后再加 `_note` / `_todo` 也不会漏进响应。
    """
    return {k: v for k, v in p.items() if not k.startswith("_")}


# 哪些字段的值依赖"今天是几号" —— 必须标出来给 B，否则他会拿它当永久基准去逐值断言。
TIME_SENSITIVE = [
    "stats.dropped_expired", "stats.scored",
    "items[].score", "items[].raw_score",
    "items[].bucket", "items[].days_left",
]

# 列表接口**必须**保留的字段（见 api.md 第 1 节）。body_text 在列表里裁掉，省带宽。
# **前端渲染要用的字段必须在同一个响应里给全** —— 让 C 为了 confidence 再拉一次列表接口
# 是错的：实测那样只能按 id 关联上 24/30 条，剩下 6 条前端不敢标「疑似」。
LIST_FIELDS = ("id", "school", "school_slug", "unit", "board", "title", "publish_date",
               "url", "deadline", "apply_mode", "target_year", "degree_types",
               "majors", "tags", "extract",
               # 2026-10-18（B1 向 A 这边对表）：质量 / 主题 / 时效。
               # **必须和 server/config.py 的 LIST_FIELDS 逐字一致、同序** ——
               # 这两个地方各有一份，而它们共同钉住同一个 fixture。
               "record_quality", "topic", "is_fresh", "age_days",
               # 2026-10-18（B9 向 A 这边对表）：招生年的唯一权威。
               "admission_year",
               # 2026-10-18（A24，同日改名）：时效分层 live/recent/archive。
               # **不能叫 `tier`** —— 那个名字已经被推送层占了（tier=1/2 的匹配档），
               # 同一个字段两个含义迟早出事，所以这里叫 `age_tier`。
               "age_tier")
# 注意：**`relevance` 故意不在上面这张表里** —— 服务端拿它排序，但不把它放进列表载荷。
# 这是有意为之，而且是条好设计：**不暴露，前端就没法拿它当筛选器**，
# 于是"`relevance` 只用于排序、绝不用于隐藏"这条规矩就被**结构性地**锁死了，
# 不依赖谁记得住规矩。所以这里排序用它、投影时扔掉它，两边行为和接口完全一致。

FACULTY_FIELDS = ("id", "name", "title", "school", "unit", "group", "group_kind",
                  "is_phd_supervisor", "research_areas", "email", "homepage")

# 套磁信模板。
#
# **必须说清楚这不是 LLM 生成的。** 它是一份**契约样例**：
# 形状来自 api.md 第 3 节，引用的每一句都从真实公告原文里抠出来、可以点回原文。
# C 拿它就能把「生成套磁信 + citations 可点回原文」这一页做出来；
# 真正接 LLM 是 B 的活（且见 issues/005：要不要接、key 谁出还没定）。
LETTER_NOTE = ("演示样例，非真实生成。正文是模板，citations 全部取自真实公告原文。"
               "接 LLM 后 body 由模型生成，但 citations 必须仍然逐条可回链 —— "
               "这是防止模型编造导师信息的唯一手段。")

BAD_QUOTE_HINTS = ("截止", "申请", "报名", "材料", "提交", "寄", "发送")

# 挑引用句时的取舍。
#
# 坑（已踩）：第一版只按"含申请/报名/材料"挑句子，结果选中了
# 「高校学生应征入伍退出现役…」和「少数民族高层次骨干计划…」——
# 那是**专项政策**，放进套磁信里驴唇不对马嘴。
# 所以既要正向偏好，也要明确排除专项政策类句子。
QUOTE_GOOD = ("研究方向", "研究领域", "材料", "英语", "六级", "四级", "托福", "雅思",
              "推荐", "成绩", "联系", "邮箱", "申请", "提交", "截止", "考核", "面试")
QUOTE_BAD = ("入伍", "退役", "少数民族", "骨干", "专项计划", "加分", "定向就业",
             "政审", "同等学力", "非全日制", "退役大学生")


def _pick_quotes(body, want=2, max_len=86):
    """从正文里挑最像"申请要求"的原句，按出现顺序返回。"""
    cands = []
    for i, sent in enumerate(body.replace("\n", "。").split("。")):
        s = sent.strip(" 　")
        if not (14 <= len(s) <= 200):
            continue
        if any(b in s for b in QUOTE_BAD):
            continue
        score = sum(1 for g in QUOTE_GOOD if g in s)
        if score == 0:
            continue
        cands.append((score, -i, s))          # 分数高的优先；同分取靠前的
    cands.sort(reverse=True)
    picked = sorted(cands[:want], key=lambda x: -x[1])   # 回到原文顺序
    out = []
    for _, _, s in picked:
        out.append(s if len(s) <= max_len else s[:max_len].rstrip("，,、；; ") + "…")
    return out


def _short_title(t, n=24):
    """截标题时别切在括号中间 —— 「…赴美国西北大学（Nort的申请咨询」这种读起来很糟。"""
    if len(t) <= n:
        return t
    cut = t[:n]
    # 如果截断点在某个左括号之后，就近退到括号前
    for op, cl in (("（", "）"), ("(", ")")):
        if cut.count(op) > cut.count(cl):
            cut = cut[:cut.rfind(op)]
    return cut.rstrip("（(、，, ") + "…"


def make_letter(records):
    """从一条真实公告里抠出可回链的引用，拼一份套磁信样例。

    优先挑**推免/夏令营相关、有邮箱、有截止日**的那条 —— 演示要贴题。
    退而求其次才用任意一条有邮箱和材料清单的。
    """
    def score(r):
        s = 0
        if r.get("apply_mode") in ("推免", "预推免", "夏令营"):
            s += 4
        if r["contact"].get("email"):
            s += 3
        if r.get("deadline"):
            s += 2
        if r.get("materials"):
            s += 1
        if len(r.get("body_text") or "") > 200:
            s += 1
        return s

    cand = max(records, key=score)
    body = cand["body_text"]
    quotes = []

    # 引用 1：标题
    quotes.append({"label": "公告标题", "quote": cand["title"],
                   "source": "title", "url": cand["url"]})
    # 引用 2/3：正文里和"申请要求"有关的真实原句
    for s in _pick_quotes(body, want=2):
        quotes.append({"label": "公告原文", "quote": s, "source": "body_text",
                       "url": cand["url"]})
    # 引用 4：材料清单（结构化字段，也是公告原文里的词）
    if cand["materials"]:
        quotes.append({"label": "材料要求",
                       "quote": "、".join(cand["materials"][:6]),
                       "source": "materials", "url": cand["url"]})

    prof_fallback = "老师"
    # **用完整标题，不截断**（契约 api.md 第 3 节写明了）。
    # 坑（已踩）：这里原来调 `_short_title()` 截断，而 B 的实现用完整标题 ——
    # 于是两边拼出的 subject 不一样，**测试红在了两边都没做错的地方**。
    # 根因是契约例子里那个 `…` 没写明是"举例省略"，被读成了规格。
    subj = f"【推免咨询】关于{cand['title']}的申请咨询"
    lines = [
        f"{prof_fallback}您好：",
        "",
        f"我是____大学____专业的本科生，计划申请{cand['target_year'] or ''}年度的推免。"
        f"我在《{_short_title(cand['title'], 30)}》中看到相关安排，想向您请教。",
        "",
        "我注意到公告中提到：",
    ]
    for q in quotes[1:3]:
        lines.append(f"  · {q['quote']}")
    if cand["deadline"]:
        lines.append(f"  · 申请截止 {cand['deadline']}")
    lines += [
        "",
        "我的情况：GPA 排名 ____，英语 ____，科研经历 ____。"
        "附上简历与成绩单，期待您的指点。",
        "",
        "此致",
        "敬礼",
        "",
        "____（姓名）　____（联系方式）",
        "",
        f"—— 以上引文均来自：{cand['url']}",
    ]
    return {
        "notice_id": cand["id"],
        "subject": subj,
        "body": "\n".join(lines),
        "citations": quotes,
    }


def _list_item(r):
    d = {k: r.get(k) for k in LIST_FIELDS}
    atts = r.get("attachments") or []
    d["has_attachments"] = bool(atts)
    d["attachments_count"] = len(atts)
    return d


def _check_unique(items, key="id", what=""):
    """断言没有重复 id。

    为什么要有这条：浙大物理学院的同一篇文章 http/https 都能打开，
    曾经让 8 组记录进了产出、`id` 一模一样只有 `url` 协议不同，
    前端列表就出现成对一模一样的卡片。这类 bug 在数据里看不出来，
    只在**界面**上看得出来 —— 所以必须在生成产物这一层挡住。
    """
    seen = {}
    dups = []
    for it in items:
        k = it.get(key)
        if k in seen:
            dups.append(k)
        seen[k] = it
    if dups:
        raise SystemExit(f"❌ {what} 里有 {len(dups)} 个重复 {key}：{dups[:5]}\n"
                         f"   先去 collector 修数据（crawl.py 按归一化 url 去重），"
                         f"不要在接口层遮这个问题。")
    return len(items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None,
                    help="把'今天'固定成某一天（YYYY-MM-DD）。"
                         "freshness 分档和 days_left 都依赖日期，"
                         "固定之后同一份 fixture 任何时候都能复现 —— "
                         "否则它只能当当天快照，不能当基准。")
    args = ap.parse_args()

    from datetime import date, datetime
    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
    else:
        as_of = date.today()

    os.makedirs(FIX, exist_ok=True)
    with open(NOTICES, encoding="utf-8") as f:
        records = [json.loads(ln) for ln in f if ln.strip()]

    # ---- 1) GET /api/notices ----
    # **先按服务端默认排序排整条记录，再投影**（顺序反了就会像下面那个坑一样）。
    # 排序键必须和 `server/store.py` 的 `_sort_key_publish_desc` 一一对应：
    # `(relevance, publish_date)` 两级降序，`relevance` 缺失按 1（"说不清"那档）。
    #
    # 坑（已踩）：这里原来**只按 publish_date 排**。我给契约和 crawl.py 都加了 `relevance`
    # 用来排序，**却忘了同步这个生成器** —— 于是 fixture 的顺序和接口只重合 26/50，
    # B 的「列表响应默认与 fixture 完全一致」红了，而他的覆盖率断言正好卡在门槛下，
    # 报出来是"只比对了 26 条，覆盖太少"，看着像他的测试太严。
    # **同一个排序策略写在两个地方，迟早会分叉。**
    # ---- 默认可见集合：排除 `record_quality=junk`（契约 §19.2）----
    #
    # **必须和 `server/store.py` 的 `list()` 用同一条判据**，否则 fixture 的 total
    # （1120）和接口的 total（1005）对不上，B 的门禁会红，而红的原因是
    # "生成器没跟接口改" —— 这个坑在 `LIST_FIELDS` / `field_trust` 上各栽过一次。
    #
    # 只排 `junk`，**不排 `thin`**：`thin` 是"正文 100~300 字"，多半是"见附件"型公告，
    # 附件里才是正文 —— 那不是脏，是形态。
    visible = [r for r in records if (r.get("record_quality") or "") != "junk"]
    ordered = sorted(
        visible,
        key=lambda r: ((r.get("relevance") if r.get("relevance") is not None else 1),
                       r.get("publish_date") or ""),
        reverse=True)
    items = [_list_item(r) for r in ordered]
    _check_unique(items, what="notices.list.json")
    listing = {"total": len(items), "page": 1, "size": 50, "items": items[:50]}
    # 刻意保留一条首页示例，说明"分页时 total 是全量、items 是当页"
    json.dump(listing, open(os.path.join(FIX, "notices.list.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 2) GET /api/notices/{id} ----
    detail = next((r for r in records if len(r.get("body_text") or "") > 400), records[0])
    json.dump(detail, open(os.path.join(FIX, "notices.detail.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 3) POST /api/match（item 里必须带 confidence 和 has_attachments）----
    prof = clean_profile(load_profile())
    matched, stats = MP.match(records, prof, as_of)
    by_id = {r["id"]: r for r in records}
    match_items = []
    for it in matched[:30]:
        src = by_id.get(it["id"], {})
        atts = src.get("attachments") or []
        match_items.append({
            "id": it["id"], "score": it["score"], "raw_score": it["raw_score"],
            "bucket": it["bucket"], "days_left": it["days_left"],
            "title": it["title"], "school": it["school"], "unit": it["unit"],
            "publish_date": it["publish_date"], "deadline": it["deadline"],
            "url": it["url"], "reasons": it["reasons"], "warnings": it["warnings"],
            "has_attachments": bool(atts), "attachments_count": len(atts),
            "extract": src.get("extract"),
            # **2026-10-17 补（B 的接口加的字段，生成器必须跟上）**。
            #
            # 坑：B 在 `/api/match` 的 item 里加了 `field_trust`（C 的匹配页只认它，
            # 用来标"这条有几个字段不可信"），但**这个生成器没跟上** ——
            # 于是每次重出 fixture 都会把这个字段**删掉**，B 的门禁红在
            # "接口的键集合和 fixture 不一致"，而红的原因是"生成器少了一个键"。
            #
            # **同一个形状写在两个地方，迟早分叉** —— 这个项目已经栽过 4 次。
            # 加字段时两边都要动：`server/match.py` 和这里。
            "field_trust": _field_trust(src),
            # **2026-10-18（B1）**：质量 / 主题 / 时效四件套。
            # 又一次"同一个形状写在两个地方"：`server/match.py` 的 item 也要加同样的键，
            # 这里漏了就会被下一次重出 fixture 静默删掉，
            # 而 B 的门禁红在"键集合不一致"、红的原因是"生成器少了一个键"。
            "record_quality": src.get("record_quality") or "unknown",
            "topic": src.get("topic"),
            "is_fresh": src.get("is_fresh"),
            "age_days": src.get("age_days"),
        })
    # 注意：**响应 fixture 里不放 `_` 开头的注释键。**
    # 坑（已踩，被 B 的契约检查抓到）：我一度把 `_as_of` / `_time_sensitive` / `_note`
    # 塞进 match.response.json，而 B 的断言是"响应的键集合 == fixture 的键集合" ——
    # 那是个**合理**的断言，被我这种塞法弄红了。
    # **一个代表"响应"的 fixture，必须恰好等于响应的形状。**
    # 给人看的标注一律挪到同级目录的 META.json。
    json.dump({"profile": prof, "stats": stats, "items": match_items},
              open(os.path.join(FIX, "match.response.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 4) GET /api/me（订阅态）----
    json.dump({
        "user": {"id": "u_demo", "nickname": "示例同学", "subscribed": False,
                 "expires_at": None, "daily_free_quota": 3, "daily_used": 1},
        "plans": [
            {"id": "monthly", "name": "月度订阅", "price": 29, "period": "月",
             "features": ["截止日提醒", "套磁信生成", "论坛全文", "变更推送"]},
            {"id": "season", "name": "推免季畅读", "price": 99, "period": "5 个月",
             "features": ["以上全部", "背景匹配报告", "面试题库"]}
        ],
        # `_note` 是**真实的响应字段**（B 的服务确实返回它，见 server/billing.py 的 DEMO_NOTE），
        # 所以它必须出现在 fixture 里 —— 一个代表"响应"的 fixture 必须恰好等于响应形状。
        # 我此前定的"`_` 开头非契约"只适用于 fixture 的说明性标注（那些放 META.json）。
        "_note": DEMO_NOTE,
    }, open(os.path.join(FIX, "me.response.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)

    # ---- 5) POST /api/subscribe（模拟支付）----
    json.dump({
        "user": {"id": "u_demo", "nickname": "示例同学", "subscribed": True,
                 "expires_at": "2026-11-17T00:00:00", "daily_free_quota": 3,
                 "daily_used": 1},
        "order": {"order_id": "o_20261017_0001", "plan_id": "monthly", "amount": 29,
                  "paid_at": "2026-10-17T01:30:00", "status": "paid",
                  "_note": DEMO_NOTE},
        "plans": [
            {"id": "monthly", "name": "月度订阅", "price": 29, "period": "月",
             "features": ["截止日提醒", "套磁信生成", "论坛全文", "变更推送"]},
            {"id": "season", "name": "推免季畅读", "price": 99, "period": "5 个月",
             "features": ["以上全部", "背景匹配报告", "面试题库"]}
        ],
        "_note": DEMO_NOTE,
    }, open(os.path.join(FIX, "subscribe.response.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)

    # ---- 6) GET /api/posts（匿名论坛列表）----
    # 形状按 api.md 第 4 节：公开字段一个不多一个不少，
    # **绝不出现 author_real_id / created_ip / review_status**。
    json.dump({
        "total": 3, "page": 1, "size": 50,
        "items": [
            {"id": "p_20261017_0001", "unit": "物理学院", "tag": "推免经验",
             "title": "夏令营套磁什么时间发比较合适？",
             "body": "我按公告里的时间线提前两周发的，导师回复挺快。"
                     "建议先把个人陈述写好，别只发一句「老师您好」。",
             "author_display": "匿名·紫金港的松鼠", "created_at": "2026-10-17T01:20:00",
             "reply_count": 2, "report_count": 0},
            {"id": "p_20261016_0007", "unit": "材料科学与工程学院", "tag": "实验室锐评",
             "title": "组会频率和毕业要求，问清楚再决定",
             "body": "有些组一周两次组会，有些一个月一次。这不是好坏的差别，"
                     "是适不适合你的差别，报名前一定问师兄师姐。",
             "author_display": "匿名·玉泉的猫", "created_at": "2026-10-16T22:05:00",
             "reply_count": 5, "report_count": 1},
            {"id": "p_20261016_0003", "unit": "研究生院", "tag": "材料准备",
             "title": "六级没过还有机会吗",
             "body": "看公告怎么写。有的写「或进修过本科英语课程成绩合格」，"
                     "那就是留了口子，别自己先放弃。",
             "author_display": "匿名·西溪的树", "created_at": "2026-10-16T19:40:00",
             "reply_count": 3, "report_count": 0},
        ],
    }, open(os.path.join(FIX, "posts.list.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)

    # ---- 7) GET /api/faculty ----
    fac_path = os.path.join(common.OUT_DIR, "faculty.jsonl")
    if os.path.exists(fac_path):
        with open(fac_path, encoding="utf-8") as f:
            fac = [json.loads(ln) for ln in f if ln.strip()]
        # `matchable` 是**派生**字段（2026-10-17 加）：有研究方向才能进匹配池。
        # 判据和 `server/store.py` 的 `FacultyStore.project` 必须一致 ——
        # 又是一处"同一个判据两个地方"，所以这里**照抄那一行**并在门禁里对表
        # （`faculty_列表形状与_fixture_一致` 会逐字段比，漂了会红）。
        fitems = [dict({k: r.get(k) for k in FACULTY_FIELDS},
                       matchable=bool(r.get("research_areas"))) for r in fac]
        _check_unique(fitems, what="faculty.list.json")
        json.dump({"total": len(fitems), "page": 1, "size": 50, "items": fitems[:50]},
                  open(os.path.join(FIX, "faculty.list.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

        # ---- 5) POST /api/faculty/match ----
        # **只取可匹配的**（2026-10-17）：接口只返回 `matchable=true` 的导师，
        # 而参考实现按"全部导师"算分 —— 这里照接口的实际行为过滤。
        # 两者口径不同是刻意的（参考实现验公式，接口多加一层分层），见 api.md §8。
        fm = [x for x in MP.match_faculty(fac, prof)
              if (x.get("research_areas") or [])]
        fmitems = [{
            "id": it["id"], "name": it["name"], "title": it["title"],
            "school": it["school"], "unit": it["unit"], "group": it["group"],
            "email": it["email"], "homepage": it["homepage"],
            "research_areas": it["research_areas"], "hits": it["hits"],
            "score": it["score"], "reasons": it["reasons"], "warnings": it["warnings"],
        } for it in fm[:30]]
        json.dump({"profile": prof, "total": len(fm), "items": fmitems},
                  open(os.path.join(FIX, "faculty.match.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

    # ---- 6) POST /api/letter（模板样例，citations 可回链）----
    json.dump(make_letter(records),
              open(os.path.join(FIX, "letter.response.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 7) META.json：给人看的标注，**不混进任何响应 fixture** ----
    # 为什么不塞进响应里：B 的契约断言是"响应的键集合 == fixture 的键集合"，
    # 那是个合理断言。响应 fixture 必须恰好等于响应形状，注释单独放这里。
    json.dump({
        "_note": "本文件不是接口响应，是 fixtures 的说明。"
                 "所有响应 fixture 里都不含 `_` 开头的键。",
        "_generated_on": as_of.isoformat(),
        "_as_of": as_of.isoformat(),
        "_how_to_reproduce": "cd collector && python tools/make_fixtures.py --as-of "
                             + as_of.isoformat(),
        "_time_sensitive": {
            "match.response.json": TIME_SENSITIVE,
            "_why": "这些值依赖 _as_of 这一天（score 含 freshness 维度，days_left 与 "
                    "stats.dropped_expired 直接由当天算出）。换日期同样的代码给出不同数字。",
            "_rule": "形状永久有效、逐字段比对；数值只对 _as_of 那天有效，禁止跨日期比对。",
        },
        "_contract": {
            "match.response.json": "响应形状必须恰好是 {profile, stats, items}",
            "me.response.json": "模拟支付，不接真实通道",
            "letter.response.json": "演示样例，非 LLM 生成；citations 全取自真实公告原文",
        },
    }, open(os.path.join(FIX, "META.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)

    for name in sorted(os.listdir(FIX)):
        size = os.path.getsize(os.path.join(FIX, name))
        print(f"  {name:26s} {size:>9,} B")
    print(f"\n契约样例 -> {FIX}")
    print("C 可以只靠这些文件写完整前端；B 上线后把 base url 换掉即可。")


if __name__ == "__main__":
    main()

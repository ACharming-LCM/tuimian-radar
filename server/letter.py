# -*- coding: utf-8 -*-
"""套磁信（`POST /api/letter`，契约 `contracts/api.md` 第 3 节）。

## 这一版是**模板版**，不是模型生成的

原计划是接 LLM 来写正文，但"用哪个模型、key 谁出"一直没拍板（`issues/005`），
而它是演示第 4 步 —— 没有实现就是一个死点。

**不需要 LLM 也能做，而且做出来的是更有意思的那一半：**

演示时最有价值的**不是信写得好不好，是 `citations` 逐条能点回原文**。
那是防"模型编造导师信息"的手段。所以这一版：

- `body` 是模板拼的（引用标题 + 1~2 句真实要求）；
  **画像里有的字段（学校 / 专业 / GPA / 英语 / 科研经历）直接填进去**，
  没有的留 `____（请补充…）` 并说清缺什么（2026-10-19 B24 修：
  改之前画像填了学校也还是「我是____大学____专业的本科生」）；
- `citations` **全部逐字取自真实公告原文**，每条带 `source` 和 `url`，可回链；
- 响应带 `method: "template"` 和一句明确的说明：**这不是模型生成的**。

接上 LLM 之后 `body` 换成模型产出，**`citations` 必须仍然逐条可回链** ——
这套回链机制现在就该建好，因为它是这不变量。

## `citations` 的形状

契约第 124 行明确要求是**对象数组**（不是字符串数组），因为前端要：
`quote` 显示、`url` 跳转、`label` 分类、`source` 说明出处字段。

`source` 的取值范围和含义：

| source | 指回哪里 |
|---|---|
| `title` | 公告标题 |
| `body_text` | 正文里的原句 |
| `materials` | 材料清单（结构化字段，不是原句） |
| `deadline` | 截止日期（结构化字段） |

**`materials` / `deadline` 的 `quote` 是拼出来的，不是原句** ——
所以 `label` 必须写"材料要求"而不是"公告原文"，不能让用户以为那是原文引用。
这一条是诚信问题：混在一起会让人把"我们整理过的"当成"学校写的"。
"""
from __future__ import annotations

import re
from datetime import datetime

# 正文里"像申请要求"的句子优先被引用。**_POSITIVE 加分、_NEGATIVE 减分。**
#
# 为什么要减分项：第一版按"关键词命中数"排序，结果挑中的是
# 「退役人员可申请免初试」和「专项计划初试加分」两句 —— 对推免本科生最不相关的两条。
# **一封引用"退役士兵政策"的套磁信会让老师觉得学生根本没读公告。**
# 命中数并不能代表相关性，所以这里显式区分"哪些词说明这句和推免生有关"。
REQUIREMENT_HINTS_POSITIVE = (
    "推免", "免试", "推荐免试", "夏令营", "预推免", "报名", "申请",
    "成绩单", "四级", "六级", "英语", "推荐信", "承诺书", "材料",
    "截止", "考核", "复试", "体检", "学位", "GPA",
)
# 命中这些词的句子多半在讲"某一类特殊考生"，不是这位用户的通用要求
REQUIREMENT_HINTS_NEGATIVE = (
    "退役", "士兵", "战功", "二等功", "三等功", "表彰", "加分",
    "少数民族", "照顾政策", "专项计划", "强军", "援藏", "支教", "入伍",
)

# 命中这些词的句子多半是**校方开场白/宣传语**（"坚持立德树人…拟招收…"），
# 不是对申请人的要求。用户想在信里引用的是"要我做什么"，不是"学校多好"。
REQUIREMENT_HINTS_PREAMBLE = (
    "坚持", "原则", "拟招收", "欢迎", "我校", "招生规模", "共计",
    "立德树人", "服务战略", "择优录取",
)

# 表格行的特征。**这是 C 走查 #6 报的**：引文挑到了
# `19 | 材料科学与工程学院 | 李某某 | 1120220000 | 材料物理与化学 | 陈某`
# 这种表格行 —— 引在套磁信里毫无意义，而且看起来像学生没读公告。
#
# A 已经把 PDF 表格从"一个单元格一行"改成"按行拼接"（extract v4），
# 但那只是让表格**可读**了，拼接出来的行本身仍然是表格，不该当"要求"引用。
_TABLE_MARKERS = ("|", "\t")


def _looks_like_table(p):
    """判断一段是不是表格行。

    判据取"拼接后的形状"，不是内容 —— 表格行的特征是**短单元格用分隔符连起来**：
    多个 `|`、里面带裸编号、或者整段没有一个句读。
    """
    if p.count("|") >= 2:
        return True
    if "\t" in p and p.count("\t") >= 2:
        return True
    # 拼接后的表格行常常整段没有句号/逗号式的自然语句
    if p.count("|") >= 1 and not any(ch in p for ch in "。；;"):
        return True
    # 「序 号 | 申请编号 姓名 性别 护照号码」这类表头
    if p.startswith(("序 号", "序号", "志愿ID", "申请编号")):
        return True
    return False

# 引文长度上限。**按句读截断，不做省略号截半句** ——
# 截半句会让引文与原文对不上，那就破坏了"可回链"的意义。
QUOTE_MAX = 160

# 正文里最多引用几句
MAX_BODY_CITATIONS = 2

# ---- 引文质检（环 3：判不准就不给）----
#
# 这套机制的全部卖点是"引文逐条能点回原文"。**一条碎片引文同时毁掉三件事**：
# 可信度、可回链性、以及"我们没编造"这个承诺。所以宁可只有 3 条干净的，
# 不要 4 条里混 1 条垃圾。
#
# 实测过的三种垃圾（DEMO-CHECKLIST §8.2 + 我自己全量扫 196 条）：
#   - 表格数据行：`105586192600017 | 高恒 | 260 心理学系 | 040200 心理学 | 69 | …`
#   - 残缺标题（页面自己把标题截断了，尾巴带省略号）
#   - 两个字的名词（`论文` 当引文，读不出任何意思）
QUOTE_MIN_BODY = 20      # 正文引文下限：和门禁 `letter_引文短于20字的没有` 同一个数
QUOTE_MIN_TITLE = 6      # 标题下限：再短就是栏目标签，不是公告标题
# 残缺的形态：面板把它截断了，尾巴带省略号。**不猜它本来是什么**，直接不给。
ELLIPSIS_TAILS = ("…", "...", "⋯")

_SENT_SPLIT = re.compile(r"(?<=[。；;!?！？])")


def _split_paragraphs(text):
    return [p.strip() for p in (text or "").split("\n") if p.strip()]


def _clip(quote):
    """超长引文按**句读**截断；截不了就整段返回（宁可长，不可半句）。

    返回 `(引文, 是否被截断)`。

    **只认句号/分号，不认逗号。** 踩过：一开始连逗号一起认，
    于是"…逾期不得补办，请以原文为准。"被截成"…逾期不得补办，"——
    句子的落点没了，引文读起来像是在说另一件事。截在逗号上是**半句话**，
    比不截更糟。
    """
    q = quote.strip()
    if len(q) <= QUOTE_MAX:
        return q, False
    head = q[:QUOTE_MAX]
    idx = max(head.rfind("。"), head.rfind("；"), head.rfind(";"),
              head.rfind("！"), head.rfind("？"))
    if idx >= 40:                      # 至少留够半句
        return head[:idx + 1], True
    return q, False                    # 截不动就整段给，绝不半句


def _score_paragraph(p, profile=None):
    """给一段正文打分：和"这位推免申请人真正要看的"有多相关。

    返回 `(分数, 是否表格行)`。规则简单、可解释，理由都写在上面的几组词表里。
    **不用模型。**
    """
    table = _looks_like_table(p)
    s = 0
    for w in REQUIREMENT_HINTS_POSITIVE:
        if w in p:
            s += 2
    for w in REQUIREMENT_HINTS_NEGATIVE:
        if w in p:
            s -= 4
    for w in REQUIREMENT_HINTS_PREAMBLE:
        if w in p:
            s -= 2
    # 用户画像里写了的方向/学校出现在句子里，说明这句和他有关
    for m in ((profile or {}).get("majors") or []):
        if m and m in p:
            s += 1
    # 太短的（"一、报考条件"）和太长的（一大段法律条款）都往下压
    if len(p) < 25:
        s -= 3
    if len(p) > 400:
        s -= 2
    return s, table


def pick_requirement_quotes(rec, profile=None, limit=MAX_BODY_CITATIONS):
    """从正文里挑出**和这位申请人最相关**的要求句子。返回 `(引文列表, 是否降级)`。

    **只做挑选和必要的句读截断，不改字** —— 引文必须能逐字在原文里找到，
    否则"点回原文"就成了摆设。所以这里没有任何同义改写。

    **表格行只在完全没有正文句子时才用**（`degraded=True`）。

    为什么不是"表格行排最后但还能占位"：我第一版就是这么做的，结果凑不够 limit 时
    表格行照样进来了 —— 实测引到的是
    `材料学院 | 赵某（硕士教学秘书） | 80000000` 和
    `19 | 材料科学与工程学院 | 李某某 | 1120220000 | 材料物理与化学 | 陈某`。
    **引这种内容比不引更糟**：老师看到一封套磁信里引着教学秘书的电话，
    只会觉得学生根本没读公告（C 走查 #6）。
    宁可只引标题 + 材料要求 + 截止日（那三条都是真实信息），也不引表格行。
    """
    paras = _split_paragraphs(rec.get("body_text"))
    scored = []
    for i, p in enumerate(paras):
        s, table = _score_paragraph(p, profile)
        if s <= 0:
            continue
        scored.append((s, table, -i, p))
    scored.sort(reverse=True)

    out, used = [], set()
    for _, table, _, p in scored:
        if len(out) >= limit:
            break
        if table:
            continue                      # **跳过**，不是"排后面"
        quote, _ = _clip(p)
        if quote in used:
            continue
        used.add(quote)
        out.append(quote)
    return out, False


# **这里原来有一个 `_table_quotes()`**：整篇正文都是表格时，兜底引一行表格行，
# 并给一条警告。实测这样做不成立 —— 警告说"引文里含表格内容"，但没有人会去读那行
# 警告，能看见的只有引文本身（`105586192600017 | 高恒 | 260 心理学系 | …`）。
# 那正是本项目反复栽的病：**"静默降级"**，把"我判不准"说成"我给你一条引文"。
# D 在环 3 里定的规矩是「判不准就不给」，所以它被删了。**不要恢复。**


def quote_problem(quote, source="body_text"):
    """引文质检。`None` = 合格；否则返回**不合格的原因**（给 warnings 和结构字段用）。

    这是"判不准就不给"的唯一判据 —— 别在别处再写一套阈值，
    两份阈值迟早漂移，而漂移的表现是"这条引文今天合格明天不合格"。
    """
    q = (quote or "").strip()
    if not q:
        return "空引文"
    if q.endswith(ELLIPSIS_TAILS):
        # 残缺：页面把标题/句子截断了，尾巴带省略号。**不补全、不猜** ——
        # 补全就是编造，猜错比不引更糟。
        return "残缺（以省略号结尾，原文在这里就被截断了）"
    if source == "title":
        if len(q) < QUOTE_MIN_TITLE:
            return f"标题只有 {len(q)} 个字，像栏目标签不像公告标题"
        # **标题不用正文那套表格判据。**
        #
        # `_looks_like_table` 里有一条"一个 `|` 且没有句读 → 表格"，
        # 对正文合理（正文句子本该有句读），对标题是**误杀**：
        # 复旦的「公告 | 服务中心学期中开放时间公告」就是
        # "栏目名 | 标题"的常见写法，右侧 15 个字，不是表格。
        #
        # 实测：470 条里就它零引文 —— 而它的标题其实是**能引的**。
        # 标题只认**多列**（`|` 两次以上）才是抽到了表头：
        # 「序 号 | 申请编号 | 姓名」那种。
        if q.count("|") >= 2:
            return "标题里有多个表格分隔符（抽到了表格表头而不是标题）"
        if "\t" in q:
            return "标题里有制表符（抽到了表格）"
    elif source == "body_text":
        if _looks_like_table(q):
            return "表格行（不是能读懂的句子）"
        if len(q) < QUOTE_MIN_BODY:
            return f"只有 {len(q)} 个字，读不出完整意思"
    return None


def build_citations(rec, profile=None):
    """组装 `citations`。返回 `(citations, degraded, dropped)`。

    ## 三条硬规矩（环 3）

    1. **表格行一条都不引**（不再有"兜底引一行表格"这条路）。
    2. **残缺/太短的不引**：`quote_problem()` 说不行，就不进 cites。
    3. **丢掉的引文要如实说**：`dropped` 里带 `source` 和 `reason`，
       `build_letter` 把它变成结构化字段 `quotes_dropped` + 一条人话警告 ——
       **不许静默降级成"看起来正常但少了一条"**。

    `degraded=True` 现在的含义是：**正文是表格式内容、一句正文都引不到**。
    调用方据此说清"信里到底引了什么"（措辞见 `warnings_for`）。
    """
    url = rec.get("url")
    quotes, _ = pick_requirement_quotes(rec, profile)
    # 正文是表格式 → 一句话都挑不出来。**这就是全部处理，没有兜底引表格行。**
    degraded = not quotes and any(
        _looks_like_table(p) for p in _split_paragraphs(rec.get("body_text")))

    raw = [{"label": "公告标题", "quote": rec.get("title") or "",
            "source": "title", "url": url}]
    for q in quotes:
        raw.append({"label": "公告原文", "quote": q, "source": "body_text", "url": url})

    mats = rec.get("materials") or []
    if mats:
        # **注意 label**：这条是结构化字段拼的，不是原句，所以不能叫"公告原文"
        raw.append({"label": "材料要求", "quote": "、".join(mats),
                    "source": "materials", "url": url})

    if rec.get("deadline"):
        raw.append({"label": "截止日期", "quote": rec["deadline"],
                    "source": "deadline", "url": url})

    cites, dropped = [], []
    for c in raw:
        why = quote_problem(c["quote"], c["source"])
        if why is None:
            c["quote"] = c["quote"].strip()
            cites.append(c)
        else:
            # **丢掉的引文要留名字**：只留 source + reason，不留原文 ——
            # 把表格行原样搬进响应，等于让它换个字段名继续出现在界面上。
            dropped.append({"source": c["source"], "reason": why})
    return cites, degraded, dropped


def build_subject(rec):
    """标题：`【推免咨询】关于…的申请咨询`（和 fixture 一致，标题**不加书名号**）。"""
    title = rec.get("title") or "招生公告"
    mode = rec.get("apply_mode")
    kind = {"推免": "推免咨询", "夏令营": "夏令营咨询",
            "预推免": "预推免咨询", "统考": "报考咨询",
            "申请考核": "申请考核咨询"}.get(mode, "招生咨询")
    return f"【{kind}】关于{title}的申请咨询"


def _gpa_text(v):
    """画像 `gpa_rank` → "5/120" 这种写法。认字典和字符串两种（都见过）。"""
    if isinstance(v, dict):
        rank, total = v.get("rank"), v.get("total")
        if rank and total:
            return f"{rank}/{total}"
        return str(rank or total or "").strip()
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return f"{v[0]}/{v[1]}"
    return ""


def _english_text(v):
    """画像 `english` → "CET-6 580、托福 100"。认字典和字符串两种。"""
    if isinstance(v, dict):
        labels = (("cet6", "CET-6"), ("cet4", "CET-4"),
                  ("toefl", "托福"), ("ielts", "雅思"))
        parts = [f"{label} {v[key]}" for key, label in labels if v.get(key)]
        return "、".join(parts)
    if isinstance(v, str):
        return v.strip()
    return ""


def _research_text(v):
    """画像 `research` → "CUPT 国赛一等奖、凝聚态实验一段"。"""
    if isinstance(v, (list, tuple)):
        return "、".join(str(x).strip() for x in v if str(x).strip())
    if isinstance(v, str):
        return v.strip()
    return ""


def _fill(value, label):
    """有值就填；没有就留占位符**并说清缺什么**（B24，2026-10-19）。

    改之前正文里是光秃秃的 `____`：画像明明填了学校/专业，生成出来还是
    「我是____大学____专业的本科生」—— 用户得自己去猜哪里该填什么，
    而"画像里明明有"的那部分也没被用上。现在：
    - 有值 → 直接用（`我是浙江大学的物理学专业本科生`）；
    - 没值 → `____（请补充你的学校）`，**占位符还在，但知道该填什么**。
    """
    s = str(value).strip() if value not in (None, "") else ""
    return s if s else f"____（请补充{label}）"


def _labeled(prefix, value, label):
    """`GPA 排名 5/120`；没有值时**连前缀一起换成提示**，避免
    「GPA 排名 ____（请补充GPA 排名）」这种重复。"""
    s = str(value).strip() if value not in (None, "") else ""
    return f"{prefix}{s}" if s else f"____（请补充{label}）"


def build_body(rec, profile=None, citations=None):
    """拼正文。**能填的都填，填不了的留占位符并标注缺什么。**

    这封信仍然是给用户改的草稿（GPA / 联系方式这些不一定在画像里），
    但**画像里已经有的学校 / 专业 / GPA / 英语 / 科研经历**不该再留空 ——
    留空会让人以为系统没读到他的画像（2026-10-19 QA 就是这么报的）。
    """
    p = profile or {}
    cites = citations if citations is not None else build_citations(rec, p)
    if isinstance(cites, tuple):          # 容错：允许传 build_citations 的原始返回值
        cites = cites[0]
    url = rec.get("url") or ""
    year = rec.get("target_year")
    year_txt = f"{year}年度" if year else "本年度"

    body_quotes = [c["quote"] for c in cites if c["source"] == "body_text"]
    mats = [c["quote"] for c in cites if c["source"] == "materials"]

    majors = [m for m in (p.get("majors") or []) if str(m).strip()]
    school_txt = _fill(p.get("school"), "你的学校")
    # 有值才加"专业"两个字（`物理学专业`）；没有时提示语自己是一句话，
    # 别拼成「____（请补充你的专业）专业」。
    major_part = f"{str(majors[0]).strip()}专业" if majors else "____（请补充你的专业）"

    lines = [
        "老师您好：",
        "",
        f"我是{school_txt}的{major_part}本科生，计划申请{year_txt}的"
        f"{rec.get('apply_mode') or '研究生'}。我在《{rec.get('title')}》"
        f"中看到相关安排，想向您请教。",
        "",
    ]
    if body_quotes:
        lines.append("我注意到公告中提到：")
        for q in body_quotes:
            lines.append(f"  · {q}")
    if mats:
        # 措辞和 fixture 对齐：不写"需要准备的材料："，直接列（放括号里说明来源）
        lines.append(f"  · {mats[0]}（材料要求）")
    if rec.get("deadline"):
        lines.append(f"  · 申请截止 {rec['deadline']}")
    if not body_quotes and not mats and not rec.get("deadline"):
        # 这条公告的正文没抽到、也没有结构化要求。**不能编**，就明说。
        # 把"以原文为准"放在**句首**：万一这段被 `_clip` 截断，落点也不会丢。
        lines.append("  · 以原文为准：这份公告的正文未抽取到可引用的要求")

    gpa_txt = _labeled("GPA 排名 ", _gpa_text(p.get("gpa_rank")), "GPA 排名")
    eng_txt = _labeled("英语 ", _english_text(p.get("english")), "英语成绩")
    research_txt = _labeled("科研经历 ", _research_text(p.get("research")), "科研经历")
    name_txt = _fill(p.get("name"), "姓名")
    contact_txt = _fill(p.get("contact"), "联系方式")
    lines += [
        "",
        f"我的情况：{gpa_txt}，{eng_txt}，{research_txt}。"
        "附上简历与成绩单，期待您的指点。",
        "",
        "此致",
        "敬礼",
        "",
        f"{name_txt}　{contact_txt}",
    ]
    # 结尾标出引文出处：用户复制这封信时，回链不能丢
    if url:
        lines += ["", f"—— 以上引文均来自：{url}"]
    return "\n".join(lines)


def _summarize_dropped(dropped):
    """把丢弃原因归并成一句人话：`表格行 2 条、只有 1 个字…`。

    归并到粗类（取 `（` 之前那截），否则 30 条候选会写出 30 种原因，
    读起来像日志而不像提醒。
    """
    counts = {}
    for d in dropped:
        key = d["reason"].split("（")[0]
        counts[key] = counts.get(key, 0) + 1
    parts = [f"{k} {v} 条" if v > 1 else k for k, v in counts.items()]
    return "、".join(parts)


def warnings_for(rec, profile=None, degraded=False, dropped=None, citations=None):
    """这封信的"别直接发"提醒。和 `/api/match` 的 warnings 同一套口径。

    **所有判断都基于"信里实际引了什么"（`citations`），不是基于"本来能引什么"。**
    这一点踩过：第一版这里重新跑一遍 `pick_requirement_quotes` 看有没有候选，
    而 `build_citations` 之后又多了一道质检 —— 于是"候选有、但被判不准丢了"
    那批公告，信里一句正文都没引、警告却一个字不说。
    **判据和结果必须是同一个东西**，所以这里只吃 `citations` + `dropped`。
    """
    w = []
    cites = citations or []
    dropped = dropped or []
    labels = {"title": "标题", "materials": "材料要求", "deadline": "截止日期"}
    have = [labels[c["source"]] for c in cites if c["source"] in labels]
    has_body = any(c["source"] == "body_text" for c in cites)
    body = (rec.get("body_text") or "").strip()
    tail = f"信里只引了{'、'.join(have)}" if have else "信里一条引文都没有"

    dropped_body = [d for d in dropped if d["source"] == "body_text"]
    dropped_other = [d for d in dropped if d["source"] != "body_text"]

    if not body:
        w.append("这份公告的正文未抽取到，信里没有可引用的要求，请点开原文自行补充")
    elif not has_body:
        # 为什么一句正文都没引到 —— **三种原因三种说法**，不许笼统写成一句。
        if degraded:
            why = "正文是表格式内容"
        elif dropped_body:
            why = f"候选句子判不准，已丢弃（{_summarize_dropped(dropped_body)}）"
        else:
            why = "正文里没有像申请要求的句子"
        w.append(f"这份公告的正文没有可引用的要求句（{why}）；{tail}，请点开原文补充")

    # 只要有东西被丢，就必须说出来 —— 否则用户以为"信里只有标题"就是全部事实。
    # （`not has_body` 那一支已经把正文候选的丢弃原因说过了，这里只说剩下的。）
    rest = dropped_other if not has_body else dropped
    if rest:
        w.append(f"有 {len(rest)} 条候选引文判不准，已丢弃、没有写进信里"
                 f"（{_summarize_dropped(rest)}）—— 宁可少引，也不引一条读不懂的")

    if not rec.get("deadline"):
        w.append("未抽到截止日期，务必点开原文确认后再发信")
    conf = (rec.get("extract") or {}).get("confidence")
    conf = 0.5 if conf is None else float(conf)
    if conf < 0.5:
        w.append(f"该公告字段抽取置信度低（{conf:.2f}），引文请核对原文")
    w.append("这是**模板草稿**，不是模型生成的，请按自己的情况改写后再发送")
    return w


def build_letter(rec, profile=None, tone="正式"):
    """组装完整响应。形状 = `contracts/fixtures/letter.response.json`。

    比 fixture 多三个键（`method` / `cited_from` / `warnings` / `quotes_dropped`）——
    契约第 3 节允许"只增不减"，fixture 里那些键一个都不能少。
    """
    citations, degraded, dropped = build_citations(rec, profile)
    return {
        "notice_id": rec.get("id"),
        "subject": build_subject(rec),
        "body": build_body(rec, profile, citations),
        "citations": citations,
        # 契约要求（A 的指示）：必须说清这不是模型生成的
        "method": "template",
        "tone": tone,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        # **结构化地说清这封引用到了哪些来源。**
        # 为什么要给：警告文案是自然语言，前端（和测试）想判断"这封信到底引了什么"
        # 时不该去字符串里搜关键词 —— 我第一次写测试就是这么干的，
        # 结果被另一条警告里的"截止日期"四个字误判。
        # 给出这个字段，判断就变成读一个集合。
        "cited_from": sorted({c["source"] for c in citations}),
        # **丢掉了哪些引文、为什么**（环 3）。结构化，不靠文案搜索。
        # 只给 source + reason，**不给原文** —— 把表格行原样搬回来，
        # 等于让它换个字段名继续出现在界面上。
        "quotes_dropped": dropped,
        "warnings": warnings_for(rec, profile, degraded=degraded, dropped=dropped,
                                 citations=citations),
        "_note": "正文由固定模板拼出，**不是大模型生成的**；"
                 "citations 全部逐字取自公告原文，可逐条回链。"
                 "判不准的引文（表格行/残缺/太短）一律不进 citations，见 quotes_dropped。"
                 "接 LLM 后 body 会变，但 citations 仍须逐条可回链。",
    }

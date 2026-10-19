# -*- coding: utf-8 -*-
"""规则字段抽取：从正文里抠出截止日、材料清单、专业、联系方式等。

D1 用规则，理由：可离线、零成本、可解释、可回归测试。
D2 再接 LLM 做补充（简章长文、表格），产出写进 extract.method='llm' 并给出 confidence，
前端对低置信字段标"疑似"。

归属：B（匹配/后端）负责调参和扩展词表；A 只保证入口函数签名不变：
    extract_fields(title, body) -> dict
"""
import re

import common

# 旧的宽窗口关键词表已废弃，被下面的 DEADLINE_TIERS（分档 + 分句）取代。
# 保留这一行是为了告诉后来者：别再走"关键词附近找日期"那条路，实测会跨句错判。

MATERIAL_WORDS = [
    "个人陈述", "个人简历", "简历", "推荐信", "专家推荐", "成绩单", "成绩排名",
    "排名证明", "英语成绩", "四六级", "四级", "六级", "托福", "雅思", "获奖证书",
    "获奖证明", "论文", "科研成果", "研究计划", "申请表", "报名表", "身份证",
    "学生证", "承诺书", "政审", "体检", "作品集", "在读证明", "学历证明",
]

MAJOR_WORDS = [
    "物理学", "力学", "数学", "化学", "材料", "计算机", "软件", "电子", "信息",
    "通信", "控制", "机械", "土木", "建筑", "生物", "医学", "药学", "环境",
    "能源", "化工", "经济", "金融", "管理", "法学", "教育", "大气", "海洋",
    "地质", "地理", "统计", "心理", "农学", "食品", "航空", "航天", "船舶",
    "核科学", "光学", "仪器", "电气", "自动化", "人工智能",
]

TAG_RULES = [
    ("推免", "推免"), ("免试", "推免"), ("夏令营", "夏令营"), ("预推免", "预推免"),
    ("直博", "直博"), ("申请考核", "申请考核"), ("调剂", "调剂"), ("复试", "复试"),
    ("招生简章", "招生简章"), ("专业目录", "专业目录"), ("公示", "公示"),
    ("录取", "录取"), ("学科调整", "学科调整"), ("名额", "名额"),
    ("专项计划", "专项计划"), ("国优计划", "国优计划"),
]


# 无年份日期：9月15日 / 9.15 / 9-15 / 9月15号。中文公告里极常见。
MD_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?(?!\d)")

# 分句：截止日期的语境不会跨句。v1 的 ±45 字宽窗口会跨句 + 跨段，
# 这是它给 9 月发布的细则判出次年 3 月截止日的根本原因。
SENT_SPLIT = re.compile(r"[。；;\n\r]|(?<=\d)\s{2,}")

# 分档：**用正则，不用词表**。中文公告的截止日写法太多，词表永远追不完。
#
# 实测漏掉的真实截止日（这条是活的、10-10 发布 10-17 截止的报名通知）：
#   「（1）2026年10月17日24点前，将《…申请表》发送到 teacher02@zju.edu.cn」
# 旧词表里有「前将」「前发送」，但这句写的是「点前，将…发送」——
# 中间隔着「，」和「24点」，两个词表项全都没命中。
# 所以档位判断改成"日期 + 前 + 动作动词"的正则，容忍标点、量词和时点。
DEADLINE_TIERS = (
    (3, [re.compile(r"(?:截止|截至)(?:时间|日期|至)?")]),
    (2, [re.compile(r"(?:报名|申请|受理|提交|报送|寄送|接收)(?:时间|日期)"),
         re.compile(r"材料提交|报送时间|寄送时间|开放时间|申请平台开放|开放第[一二三四五六七八九十0-9]+轮报名")]),
    (1, [re.compile(r"前\s*[，,、]?\s*(?:将|把)?[^。；\n]{0,12}?"
                    r"(?:提交|发送|寄出|寄达|寄回|邮寄|上传|报送|上交|递交|完成|"
                    r"汇入|汇款|缴纳|回复|确认|登记|报名|申请)"),
         re.compile(r"(?:前提交|前发送|前完成|前将|前寄|前送达|前报送|前上交|前上传)")]),
)

# 区间连接词，用于判断两个日期之间是不是"至"这种区间关系。
RANGE_GAP = re.compile(r"^[至到~～—–\-－]+$")

# 时分 / 时点：只用来在比较日期前抹掉，不参与"这是不是一个日期"的判断。
TIME_RE = re.compile(r"\d{1,2}\s*(?:[:：]\s*\d{2}|[时點点]\s*\d{0,2}\s*分?)")

# 录取后义务，不是报名截止。产品要的是"别错过报名"，
# 给已经拟录取的人判一个"体检表寄达日"，对用户是纯噪声。
# 真实案例：浙大物理 2027 推免细则里唯一的截止句是
# "我院拟录取的推免生请于2027年3月…于2027年3月31日前将体检表寄达我院"。
POST_ADMISSION = ("体检", "报到", "注册", "新生", "答辩", "毕业", "学位授予",
                  "党组织关系", "录取通知书", "宿舍", "选房", "人事档案",
                  # 2026-10-18 扩：合并软换行后，一批**拟录取之后的行政义务**也被
                  # 抽出了日期（见 journal/2026-10-18-截止日抽取提升验证.md）。
                  # 它们是"调档函寄回日""党团关系转接日""新生入学系统开放日""选课截止日"，
                  # 抽到也不能当报名截止 —— 产品要答的是"我还能不能报名"。
                  "调档", "拟录取", "党团组织关系", "团组织关系", "户口迁移",
                  "入学须知", "新生入学", "选课", "组织关系", "户口")
# 出现这些词说明这句确实在讲"申请/报名"，则上面那条豁免不生效
APPLY_WORDS = ("报名", "申请", "报考", "提交材料", "材料提交", "资格审查",
               "推荐免试", "寄送材料", "上传材料", "材料寄送")

# **整篇**的主题就是录取后事务 —— 标题命中这些词时，正文里的日期一个都不算报名截止。
# 实测漏网的三类（合并软换行后才冒出来的）：新生入学须知里的"迎新系统开放时间"、
# 研究生选课通知里的"系统开放时间"、拟录取调档/党团关系转接里的寄达日。
POST_ADMISSION_TITLE_RE = re.compile(
    r"入学须知|新生入学|拟录取|调档|选课|组织关系|户口迁移|报到|注册通知")


def _resolve_md(mo, day, publish):
    """把"9月15日"补成年份。规则：先用发布年；若结果早于发布日，说明指的是下一年。"""
    mo, day = int(mo), int(day)
    if not (1 <= mo <= 12 and 1 <= day <= 31):
        return None
    if not publish:
        return None
    y = int(publish[:4])
    cand = f"{y}-{mo:02d}-{day:02d}"
    if cand < publish:
        cand = f"{y + 1}-{mo:02d}-{day:02d}"
    return cand


def _date_tokens(sent, publish):
    """句子里所有日期，带位置。带年份的优先，无年份的补年份。返回 [(start, end, iso)]。"""
    toks = []
    for m in common.DATE_RE.finditer(sent):
        d = common.iso_date(m.group(0))
        if d:
            toks.append((m.start(), m.end(), d))
    for m in MD_RE.finditer(sent):
        if any(s <= m.start() < e for s, e, _ in toks):
            continue                                    # 已被完整日期覆盖
        d = _resolve_md(m.group(1), m.group(2), publish)
        if d:
            toks.append((m.start(), m.end(), d))
    toks.sort()
    return toks


def _sentence_candidate(sent, publish, anchor=None):
    """一句里定一个候选截止日。

    规则（两条，都来自真实错判）：
    1. 有区间就取**区间末尾的最大值**。真实案例：东南大学招生章程
       "报名时间为2026年10月15日至10月24日（预报名时间为2026年10月9日至10月12日）"
       —— 不带年份的"10月24日"必须补上年份才能参与区间计算，否则会取到预报名的起始日。
       这里取两个区间末尾中的最大值 10-24（最后机会），而不是 10-12。
    2. 没有区间就取**离档位关键词最近**的日期。真实案例：
       "考试时间为10月20日，报名截止9月15日" —— 顺序颠倒时不能取第一个。
    """
    # 时分属于边界信息，不应阻断两端日期的区间识别：9月24日21:00-9月26日17:00。
    # 全角冒号（9：00）和"8时/8时30分"也得去掉 —— 四川大学博士招生章程就是这种写法，
    # 不去掉会让区间断掉、只取到起始日（11-27 而不是 12-17）。
    sent = TIME_RE.sub("", sent)
    toks = _date_tokens(sent, publish)
    if not toks:
        return None

    ends = []
    for (_, e1, _), (s2, _, d2) in zip(toks, toks[1:]):
        gap = sent[e1:s2].strip(" 　）)】]日号")
        if gap and RANGE_GAP.match(gap):
            ends.append(d2)
    if ends:
        return max(ends)

    if anchor is not None:
        return min(toks, key=lambda t: (abs(t[0] - anchor), t[0]))[2]
    return toks[0][2]


def _find_deadline(body, publish):
    """截止日：按**句子**扫，逐句定档，取最高档里最早的合法日期。

    坑（已踩）三个：
    1. v1 用"日期 ±45 字里出现'截止'"的宽窗口，跨句又跨段，
       给 2026-09 发布的推免细则判出 2027-03-31 的截止日。
    2. 只认 `20\\d{2}` 四位年份，而中文公告大量写作"9月15日前提交"，**完全不带年份**，
       这一类整批漏掉，是 deadline 召回上不去的主因。现在用发布日补年份。
    3. 补上年份后召回翻倍，但把"体检表寄达日"这类**录取后义务**也捞了回来，
       于是用 POST_ADMISSION 过滤掉 —— 判定标准是"这句在不在讲申请/报名"。
    """
    best_tier, best_date = 0, None
    for sent in SENT_SPLIT.split(body):
        if len(sent) > 400:            # 超长"句子"多半是没断开的表格，跳过
            continue
        if any(w in sent for w in POST_ADMISSION) and \
           not any(w in sent for w in APPLY_WORDS):
            continue                   # 录取后义务，不是报名截止

        tier, anchor = 0, None
        for t, rxs in DEADLINE_TIERS:
            hit = None
            for rx in rxs:
                hit = rx.search(sent)
                if hit:
                    break
            if hit:
                tier, anchor = t, hit.start()
                break
        if tier == 0:
            continue

        cand = _sentence_candidate(sent, publish, anchor=anchor)
        if not cand:
            continue
        # 必须在 [发布日, 发布日+400天] 内，否则是噪声
        if publish and not (publish <= cand <= _plus_days(publish, 400)):
            continue
        if tier > best_tier or (tier == best_tier and (best_date is None or cand < best_date)):
            best_tier, best_date = tier, cand
    return best_date


def _plus_days(iso, days):
    y, m, d = map(int, iso.split("-"))
    import datetime
    return (datetime.date(y, m, d) + datetime.timedelta(days=days)).isoformat()


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:0\d{2,3}[-\s]?\d{7,8}|1[3-9]\d{9})(?!\d)")

MAJOR_CONTEXT = re.compile(r"招收|招生|专业|学科|方向|报考|调剂|学位点|培养单位")


def _find_majors(title, body):
    """专业名：标题里出现直接采信；正文里必须落在"招生/专业/学科"语境附近。

    坑（已踩）：v1 全文匹配词表，"管理""教育""信息""软件"这类泛词几乎每篇必中，
    物理学院的推免细则被判出 ['物理学','材料','电子','信息','管理','教育','医学','经济']。
    """
    found = []
    for w in MAJOR_WORDS:
        if w in title:
            found.append(w)
            continue
        for m in re.finditer(re.escape(w), body):
            win = body[max(0, m.start() - 25): m.end() + 25]
            if MAJOR_CONTEXT.search(win):
                found.append(w)
                break
    return found


# ---------------------------------------------------------------------------
# 字段可信度三态（found / absent / unknown）
#
# 为什么要有它：`deadline: null` 一个值**同时表示两件完全不同的事** ——
#   「这公告里就没有截止日」（读了全文，确实没有）→ 用户该放心
#   「我们没读到」（正文是兜底抓的 / 太短 / 根本没取到）→ 用户必须自己去看原文
# 对用户这两句话是反的，而接口分不出来。评审问"这个还能报吗"，
# 答成"没有截止日"而实际是"我们没读到"，是最伤信任的一种错 ——
# **比直接说"不知道"差得多。**
#
# 关键性质：三态**只把结论往"不知道"那边退，永远不会往"有"那边走**。
# 它不参与任何"编一个日期出来"的逻辑；抽不到值永远是 null（硬规矩第 1 条）。
# 它决定的只是：该说"确实没有"，还是该说"我没读到"。
# ---------------------------------------------------------------------------

# `absent` 的正文长度门槛。
#
# **这个数不是拍脑袋取的整百数，是量出来的。** 实测 selector 命中的 167 条：
#   p5=17 字   p10=193 字   p25=359 字   p50=928 字
# 门槛落在"17 字那批"和"193 字那批"之间的空档上，所以 150 比 100/200 都稳。
#
# 为什么非要有长度这一条：selector 只说明"选择器命中了"，**不保证命中的是正文**。
# 实测有 9 条命中的是元信息框，于是"正文"长这样：
#     0 字  ×5   空的正文容器（内容全在附件 PDF 里）
#     17 字 ×4  「2026-10-08浏览次数：10」
# 这些页面上确实没有截止日 —— 但原因是**选择器框错了元素**，不是公告没写。
# 让它们进 `absent`，我们就会对用户宣布"该公告未公布截止日"，而真相是我们没读到正文。
ABSENT_MIN_BODY_CHARS = 150

# 判据不能只看字数：一段"退无可退只剩元信息"的正文，长度上未必短。
# 所以再补一条**内容型**判据 —— 把发布时间/浏览次数/发布者/审核这类元信息字段全剥掉，
# 所剩无几的，就不是正文。
# 注意正则只剥**标签词**，标签后面那个值（「发布者：研究生院」里的"研究生院"）会留下，
# 所以判据是"剥完剩得**够少**"，不是"剥完为空"。
META_ONLY_RE = re.compile(
    r"发布(?:时间|日期|者)|更新(?:时间|日期)|发表(?:时间|日期)|刊登(?:时间|日期)|"
    r"浏览(?:次数|量)|点击(?:次数|量)|阅读(?:次数|量)|来源|审核|编辑|责编|终审|"
    r"作者|访问量|字体|打印|关闭|分享|上一篇|下一篇|返回列表|"
    r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}|[：:\s]")

# 只看**短正文**，而且靠**句读**判，不靠残留字数。
#
# 试过、而且**失败**的判据（留在这里免得以后有人再试一遍）：
#   ①"剥掉元信息标签后所剩无几" —— 正则只剥标签词，标签后面的值会留下
#     （「发布者：研究生院」剩"研究生院"），实测元信息框残留 34 字；
#   ②那就把阈值抬到 34？ —— **不行**：真实短正文（"各位考生：欢迎报考我校…"）
#     剥离后残留 54 字，两者只差 20 字。**没有一条干净的分界线**，靠它判会误伤真正文。
# 所以换成一个真正的结构性差异：**元信息框是字段罗列，没有句读；正文是句子，有句读。**
# 实测 9 条问题正文（0～44 字）全都没有句号；真实短正文都带「。」或「：」。
# 窄，但它挡的是我量过的那个形态，而且**不会误伤有内容的短公告** —— 这才是重要的方向。
META_ONLY_MAX_CHARS = 200
SENTENCE_PUNCT_RE = re.compile(r"[。！？；!?;]|\n")


def _body_is_meta_only(body):
    """正文太短、没有句读、又全是元信息字段 → 这不是正文，别拿它下断言。

    覆盖范围**窄是故意的**：只挡"选择器命中了元信息框"这一个形态。
    长正文里零星出现「审核：」「来源：」是正常的，不在此列。
    """
    b = (body or "").strip()
    if len(b) > META_ONLY_MAX_CHARS:
        return False
    if SENTENCE_PUNCT_RE.search(b):
        return False                   # 有句读 → 是句子，不是字段罗列
    return bool(META_ONLY_RE.search(b))


def body_state(how, body_len):
    """把 detail 的 `body_how` + 正文长度映射成**我们有多敢判这个正文**。

    三态（与 field_state.body 是同一套取值，刻意统一，免得两处定义漂移）：
      · `ok`      —— 用**站点选择器**取到了真正文（`file` 是同义的：整条公告本身
                     就是一个 PDF/DOCX，正文就是从那份文件里抽出来的）
      · `generic` —— 走了通用兜底。形状不可信（实测 23 条里甚至有"统一身份认证平台"）
      · `missing` —— 没取到正文。**`incomplete`（整页看起来被截断）也归到这里**：
                     正文可能只到了一半，来源再"正规"也不能当证据。
                     实测：把一个 1400 字的正文截到 232 字，选择器照样命中、
                     长度照样过 150 的门槛 —— **只有这个标记能挡住它**。

    `generic` / `missing` / `incomplete` 都**不足以支撑"确实没有"这个结论** —— 见 field_state()。
    这正是"一个判据只在部分站点上出错时最容易瞒过去"那一类：兜底抓来的正文看起来
    和正常正文一样长，只有记着它的来源，才知道不能拿它下断言。
    """
    if how == "file":
        return "ok"                     # 正文就是从公告文件本身抽出来的，不是兜底
    if how != "selector":
        # generic / none / incomplete / 任何没见过的取值 → 一律不许当证据
        return "generic" if how == "generic" else "missing"
    if body_len < ABSENT_MIN_BODY_CHARS:
        return "missing"                # 太短，不足以当"我读全了"的证据
    return "ok"


def field_state(extracted, body="", body_len=None, body_how=None, attachments=None,
                pdf_stats=None):
    """算出每个字段的可信度三态。

    参数 `body_how` / `body_len` 省略时**从 `body` 现算** —— 这样同一个函数既能给
    crawl 用（有 detail 的来源信息），也能给离线回放用（只有存的正文），
    保证两条路算出来**逐字一致**。两处策略分叉这个坑本项目栽过 4 次，不再栽。

    `pdf_stats` 是 `pdf.enrich()` 的返回。**扫描版附件必须把结论退回 `unknown`**：
    那些 PDF 没有文本层（本机无 OCR 引擎），它们的正文我们是**真的没读到**，
    于是"公告里没有截止日"这句话就不成立 —— 而页面上看起来一切正常。
    这条和长度门槛是同一个道理：不让"我们没读到"伪装成"确实没有"。
    """
    if body_len is None:
        body_len = len(body or "")
    if body_how is None:
        body_how = "selector" if body else "none"

    bs = body_state(body_how, body_len)
    if bs == "ok" and _body_is_meta_only(body):
        # 长度够了、也确实来自选择器，但里面只有一个元信息框 —— 依然不构成证据
        bs = "missing"

    scanned = int((pdf_stats or {}).get("scanned") or 0)
    can_say_absent = (bs == "ok" and scanned == 0)   # **正文可信才敢说"确实没有"**

    def mark(has_value):
        if has_value:
            return "found"
        return "absent" if can_say_absent else "unknown"

    def _contact_has_value(c):
        """`contact` 是**复合字段**：有键不等于有值。

        实测 470 条里 **470 条都有 `contact` 键，但其中 232 条是
        `{"email": null, "phone": null}`** —— 一个"空壳"。
        只判 `bool(dict)` 会把这 232 条报成 `found`（"有联系方式"），
        而事实是公告里根本没写。

        这又是那个老病：**一个看起来有值的字段，掩盖了"确实没有"。**
        """
        if not isinstance(c, dict):
            return bool(c)
        return bool(c.get("email") or c.get("phone"))

    return {
        "deadline": mark(bool(extracted.get("deadline"))),
        "majors": mark(bool(extracted.get("majors"))),
        # ---- 2026-10-17 三态泛化：从 1 个字段扩到 6 个 ----
        #
        # 判据**复用同一个 `mark()`**，没有第二套阈值：
        #   有值 → found
        #   没值 且 正文可信（body_state=="ok" 且无扫描版附件）→ absent（"学校没写"）
        #   其余 → unknown（"我们没读到"）
        #
        # 为什么 `materials` 和 `contact` 值得标：
        # 用户据它们做决定的频次不比截止日低 —— "要交什么材料"决定他这周做什么，
        # "怎么联系"决定他敢不敢报。而这两个字段的"没有"和"没读到"
        # 在界面上长得一模一样，正是这一整套三态要消灭的东西。
        "materials": mark(bool(extracted.get("materials"))),
        "contact": mark(_contact_has_value(extracted.get("contact"))),
        "attachments": mark(bool(attachments)),
        "body": bs,
    }


def state_for_record(rec, body=None, body_len=None, pdf_stats=None):
    """从**一条整记录**算出 `field_state` —— crawl 和离线回填共用这一个入口。

    为什么要单独抽出来：`field_state` 依赖三样东西（抽取结果、正文来源与长度、
    扫描版附件），而这三样在"重新抓一遍"和"拿磁盘上的数据回填"两条路上
    是**从不同地方拿到的**。这个项目已经栽过 4 次"同一个策略写在两个地方、
    改了副本没改原件"，所以这里只留一个实现，两条路都调它。

    `body` / `body_len` 是**附件合并之前**的正文（即选择器取到的那些）。
    调用方必须传对：合并后的正文混进了附件正文，拿它量"选择器取到的正文有多长"
    会量错 —— 附件正文会把长度撑上去，而"选择器框住了元信息框"这种正是要靠长度挡掉的形态。

    `pdf_stats` 不给时从附件的 `note` 反推 `scanned`（见 `_scanned_from_record`）。
    """
    base = (rec.get("body_text") or "") if body is None else body
    return field_state(
        rec, body=base,
        body_len=len(base) if body_len is None else body_len,
        body_how=rec.get("body_how"),
        attachments=rec.get("attachments"),
        pdf_stats=pdf_stats if pdf_stats is not None else _scanned_from_record(rec),
    )


def _scanned_from_record(rec):
    """从附件的 `note` 反推 `pdf.enrich()` 的 `scanned` 计数。

    为什么不重跑一遍 `pdf.enrich`：那会把每个附件（含几 MB 的 PDF）重新解一遍，
    只为拿一个"有没有扫描版"的位。而 `pdf.enrich` 自己把这件事写在了 `note` 里
    （「扫描版，无文本层（需 OCR，本机无引擎）」是它唯一的措辞来源），
    所以从 note 读回来是等价的、而且是**同一个真相**，不是抄一份平行逻辑。
    """
    n = 0
    for a in rec.get("attachments") or []:
        if "扫描版" in (a.get("note") or ""):
            n += 1
    return {"scanned": n}


def extract_fields(title, body, publish=None):
    text = f"{title}\n{body}"
    deadline = _find_deadline(body, publish)
    # 整篇公告的**主题**就是录取后事务时，里面的日期一律不是报名截止。
    # 为什么不能只靠句子里的词：这些通知的日期句往往写得很干净
    # （「系统开放时间拟定于8月14日—8月17日」），一个"新生"都不带，
    # 但整篇的用途（入学须知/调档/选课）已经说明它对"还能不能报名"没有意义。
    if POST_ADMISSION_TITLE_RE.search(title or ""):
        deadline = None

    email = EMAIL_RE.search(body)
    phone = PHONE_RE.search(body)

    years = [int(y) for y in re.findall(r"20\d{2}", title)] or \
            [int(y) for y in re.findall(r"20\d{2}", body[:400])]

    if "直博" in text:
        degree = ["博士", "直博"]
    elif "博士" in text:
        degree = ["博士"]
    elif "硕士" in text or "研究生" in text:
        degree = ["硕士"]
    else:
        degree = []

    # **标题证据优先。** 正文里只要顺带出现"推免"两个字（比如"推免生也可…"），
    # 旧逻辑就会把一条《…报考点…网上报名公告》标成推免 —— 实测 119 条这类错标，
    # 它们于是绕过了"统考公告不推给推免学生"的过滤。标题写明的类型比正文可靠。
    title_l = title or ""
    if any(w in title_l for w in ("预推免", "夏令营", "推免", "免试", "直博", "申请考核")):
        if "预推免" in title_l:
            mode = "预推免"
        elif "夏令营" in title_l:
            mode = "夏令营"
        elif "申请考核" in title_l:
            mode = "申请考核"
        else:
            mode = "推免"
    elif any(w in title_l for w in ("报考点", "考点", "网上报名", "网上确认",
                                    "全国硕士研究生招生考试", "统考", "初试")):
        mode = "统考"
    elif "预推免" in text:
        mode = "预推免"
    elif "夏令营" in text:
        mode = "夏令营"
    elif "推免" in text or "免试" in text:
        mode = "推免"
    elif "申请考核" in text:
        mode = "申请考核"
    elif "统考" in text or "复试" in text:
        mode = "统考"
    else:
        mode = None

    materials = [w for w in MATERIAL_WORDS if w in body]
    majors = _find_majors(title, body)
    tags = [tag for kw, tag in TAG_RULES if kw in text]

    filled = sum(bool(x) for x in [deadline, email, degree, mode, materials, majors])
    return {
        "deadline": deadline,
        "target_year": max(years) if years else None,
        "degree_types": degree,
        "apply_mode": mode,
        "majors": majors[:8],
        "materials": materials[:12],
        "contact": {"email": email.group(0) if email else None,
                    "phone": phone.group(0) if phone else None},
        "tags": list(dict.fromkeys(tags)),
        "confidence": round(filled / 6, 2),
    }


# ---------------------------------------------------------------------------
# 「这条公告跟招生有关吗」—— **只用于排序，绝对不要用来隐藏内容**
#
# 为什么单独成一块：我实测过一个连续打分器，**两个方向都错**：
#     误杀：南开「面向海外一流高校优秀本科毕业生招收直接攻读博士学位研究生简章」
#     误杀：电子科大「接收2027年优秀应届本科毕业生免试攻读研究生拟录取名单」
#     误收：北大 EMBA 招生简章
# 所以**不能用它过滤**：过滤错了的代价是用户错过报名，那是不可接受的。
#
# 但**可以用它排序**：排序错了的代价只是某条公告低几行。代价差一个量级，
# 所以同一个不可靠的信号，能排不能滤。
#
# 实测要解决的问题：公告列表默认按日期排，于是最新的那几条行政/科研信息
# （「答辩评审名单」「论文送审双盲审名单」「Science 论文报道」）
# 会占据列表前几位 —— 评委点开列表看到的第一条不是招生信息。
#
# 输出是**三档整数**而不是连续分数，这是刻意的：
# 档位粗，误判的伤害就被限制在"掉一档"，而不是"掉到最底"。具体：
#   2 = 像招生信息   1 = 说不清   0 = 明显不是
# 排序建议：先按 relevance 降序，同一档内仍按日期降序 ——
# 这样一档内部的时效性不会被破坏。
# ---------------------------------------------------------------------------

ADMISSION_POS = re.compile(
    r"招生|推免|免试|夏令营|预推免|报考|录取|简章|专业目录|调剂|复试|"
    r"考核|选拔|报名|招生计划|学位点|接收推免|优秀大学生")

ADMISSION_NEG = re.compile(
    r"新生|报到|注册|选课|课程表|课程安排|答辩|开题|中期|"
    r"毕业审核|毕业典礼|毕业手续|毕业生就业|结业|学位授予|党建|党支部|"
    r"评优|导师资格|在职|同等学力|留学生|奖学金|助学|贷款|宿舍|"
    r"选房|体检|学费|教材|墙报|Poster|竞赛|大赛|公派|留学|学籍|"
    r"优秀导师|津贴|值班|放假|假期|安全|消防|档案|校友|招聘|拟聘")
# 「毕业」「课程」**不能裸用**：实测「优秀应届本科**毕业**生免试攻读研究生」被
# 判成非招生，一条真的推免公示被压到低档。改成行政含义的组合词。


# 「招生宣传材料」—— 和招生有关，但**不是一条可行动的公告**。
#
# 实测（2026-10-17，E 报的）：89 条 relevance=2 里**有 9 条是宣传品** ——
#   学院风采合集：…研究生招生宣传册（×4）
#   研究生招生直播宣讲会
#   北京大学物理学院电子信息专业硕士招生简介
#   【博士】转发：深圳河套学院"深瞳计划"第三期招募通知
#   电子科技大学…2025年研究生招生宣传册（×2）
# 它们的标题里有「招生」，于是命中 ADMISSION_POS 直接进 2 档，
# 结果**列表最前面是宣传册**，而那正是这个信号要解决的问题。
#
# 处理：**封顶到 1 档**，不往下踩到 0。
# 理由：它们是招生相关的（0 档是"明显不是招生信息"，说不过去），
# 只是**不该占据最前面**。1 档的语义"说不清"在这里正好 ——
# 宁可在"说不清"里多待几条，也不要把它判成"明显无关"。
#
# **试过、并且否决了别的做法（有数据支撑，别重复试）**：
#   "标题没有正向词就不许进 2 档"（E 的建议之一）——
#   实测会**误杀 4 条真招生页**：「化学与分子工程学院2026年申请」「北京大学经济学院」
#   「001信息与通信工程学院」「2026年北京大学未来技术学院"申请…」。
#   它们的标题里没有 ADMISSION_POS 的词（注意「申请」**不在**词表里），
#   但确实是招生公告。**误杀的代价是用户错过报名，和误收不是一个量级** ——
#   这就是这块注释一开始写的"能排不能滤"的原因，别用更硬的判据。
PROMO_NEG = re.compile(
    r"宣传册|风采合集|宣讲会|宣讲|直播|招生简介|招募|训练营|暑期学校|招生宣传")


def relevance_of(title, board=None, apply_mode=None):
    """返回 0|1|2，**仅供排序**。见上面那段注释：不要用它隐藏内容。

    阈值刻意取得偏"钝"：命中一个标题正向词就够进 2 档，
    但要扣到 -2 以下才判定成 0 档 —— 宁可多留在"说不清"，也不要误杀。
    """
    t, b = title or "", board or ""
    s = 0
    if ADMISSION_POS.search(t):
        s += 2
    if ADMISSION_POS.search(b):
        s += 1
    if apply_mode:
        s += 1
    if ADMISSION_NEG.search(t):
        s -= 3
    if ADMISSION_NEG.search(b):
        s -= 1
    if s >= 2:
        # 招生宣传材料封顶 1 档，见 PROMO_NEG 那段。
        return 1 if PROMO_NEG.search(t) else 2
    if s <= -2:
        return 0
    return 1

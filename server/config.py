# -*- coding: utf-8 -*-
"""server 配置。所有可调项集中在这里，方便测试注入。

归属：B（后端）。理由见 docs/B-KICKOFF.md。
"""
import os
import secrets

# server/ 的上一级即项目根
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_data_dir():
    """`data/out` 优先；没有就用仓库自带的脱敏样例 `data/sample/out`。

    公开仓库不带原始抓取数据（正文里有考生名单），但带一份经过
    `collector/tools/sanitize_for_deploy.py` 脱敏的完整副本 —— 新克隆的人
    不用先跑 16–60 分钟抓取就能打开页面。
    """
    explicit = os.environ.get("TUIMIAN_DATA_DIR")
    if explicit:
        return os.path.abspath(explicit)
    out = os.path.join(ROOT, "data", "out")
    if os.path.exists(os.path.join(out, "notices.jsonl")):
        return out
    sample = os.path.join(ROOT, "data", "sample", "out")
    if os.path.exists(os.path.join(sample, "notices.jsonl")):
        return sample
    return out


DATA_DIR = _resolve_data_dir()

# 数据源：A 的管道产出，**只读**。B 不重新抓数据、也不往里面塞字段。
NOTICES_PATH = os.environ.get(
    "TUIMIAN_NOTICES", os.path.join(DATA_DIR, "notices.jsonl")
)

# C 的前端目录，用于同源挂载（B-KICKOFF 第 2 步）。不存在就跳过，不报错。
WEB_DIR = os.environ.get("TUIMIAN_WEB", os.path.join(ROOT, "web"))

# 分页：契约写死了 size 上限 50，不要放宽 —— C 的无限滚动按这个写。
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50

# 契约 contracts/api.md 第 1 节与 fixtures 保持一致
ALLOWED_MODES = ["推免", "统考", "申请考核", "夏令营", "预推免"]
ALLOWED_LEVELS = ["硕士", "博士", "直博", "本科"]
ALLOWED_SORTS = ["publish_desc", "deadline_asc", "deadline_desc"]

# `notice_kind` 四档（A29，契约 §18.9）与 `age_tier` 三档（A24）。
# **这两个都是筛选参数的合法取值表**：传别的值给 400，不许静默当成"没筛"。
NOTICE_KINDS = ("announcement", "catalog", "emba", "other")
AGE_TIERS = ("live", "recent", "archive")
# 主题三档（A 的 `topic` 字段）。
TOPICS = ("apply", "non_apply", "other")
# 「N 天内截止」允许的窗口（B25）。**故意只给四档**：自由输入会让
# `deadline_within=0` / `9999` 这种边界各写一套语义，而用户要的是"这周/这个月"。
DEADLINE_WINDOWS = (1, 3, 7, 30)

# `/api/notices` 认识的**全部**查询参数（B24/B25，2026-10-19）。
#
# 为什么要把这份表写死在代码里：FastAPI **默认忽略未知查询参数** ——
# 传 `?notice_kind=announcement` 不会报错，也不会有任何效果，
# 前端照着文档做了筛选，实际是彻底的假功能（用户实测三个 total 一模一样）。
# 现在未知参数会在响应里返回 `unknown_params`，**不再静默**。
# 新增参数时**必须同步这张表**，门禁里有一条"每个参数都真的改变结果"盯着。
#
# ⚠️ `limit` / `offset` 是 `size` / 分页的**别名**（2026-10-19 用户点名）。
# 只把它们写进白名单而**不实现**，等于换一种方式静默失效 —— 现在都真的生效。
NOTICES_QUERY_PARAMS = (
    "school", "unit", "apply_mode", "level", "year", "q", "sort", "page", "size",
    "meta", "age_tier", "include_junk", "notice_kind", "topic",
    "limit", "offset", "deadline_within", "include_expired",
)

# 列表项保留哪些字段。**这份表不是我自己定的** ——
# 逐字抄自 `collector/tools/make_fixtures.py` 的 `LIST_FIELDS`（A 的投影定义），
# 因为 `contracts/fixtures/notices.list.json` 就是它生成的，
# 而 B-KICKOFF 要求"返回和 fixtures 完全一样形状的 JSON"。
# 少一个 → C 拿不到数据；多一个 → 和 fixture 的比对就红了。
# 注意它**不是**"完整记录减去 body_text"：source / body_len / materials /
# contact / crawled_at / content_hash 六个字段是有意不放列表的。
LIST_FIELDS = (
    "id", "school", "school_slug", "unit", "board", "title", "publish_date",
    "url", "deadline", "apply_mode", "target_year", "degree_types",
    "majors", "tags", "extract",
    # ---- 2026-10-18（B1）：质量 / 主题 / 时效 ----
    # 数据层产出（`collector/quality.py`、`topic.py`、`timeliness.py`）。
    # **必须和 `collector/tools/make_fixtures.py` 的 `LIST_FIELDS` 逐字一致、同序** ——
    # 那份表生成了 `contracts/fixtures/notices.list.json`，而契约要求
    # "列表响应默认形状 == fixture，连键顺序都比"。少一个 → C 拿不到；
    # 多一个 → 和 fixture 的比对立刻红，而红的原因会是"生成器少了一个键"
    # （这个坑在 `field_trust` / `matchable` 上各栽过一次）。
    "record_quality", "topic", "is_fresh", "age_days",
    # ---- 2026-10-18（B9）：招生年的唯一权威 ----
    # `admission_year` = "这条公告在招哪一届"（数据层只在 `topic=apply` 时给值）。
    # `target_year`（上一行那个）**已 deprecated**：它把"2027 年招生"和
    # "2026 年的日常事务"混在一起（2026 只是公告的年份）。
    # 两个都留在响应里，但契约 §1.5 写明**只以 `admission_year` 为准**。
    "admission_year",
    # ---- 2026-10-18（A24）：时效分层 live/recent/archive ----
    # **叫 `age_tier` 不叫 `tier`**：`tier` 在本项目里已经是推送层的匹配档（1/2），
    # 同一个名字存两种含义，读的人分不清（同一字段两个含义，将来必炸）。
    "age_tier",
)

# 派生字段：**JSONL 里没有这两个键**，是 A 的 `_list_item()` 现算的
# （`has_attachments = bool(attachments)`、`attachments_count = len(attachments)`）。
# 契约 api.md 第 1 节把它们列为"必须保留"，理由是"附件里常常才是正文"。
# 它们必须排在 LIST_FIELDS 之后 —— fixture 的键顺序就是这样。
DERIVED_LIST_FIELDS = ("has_attachments", "attachments_count")

# LIST_FIELDS 的出处，测试会 import 它来对表，防止两边悄悄漂移。
LIST_FIELDS_SOURCE = "collector/tools/make_fixtures.py:LIST_FIELDS"

# 导师：契约 api.md 第 8 节 + contracts/faculty.schema.json
FACULTY_PATH = os.environ.get(
    "TUIMIAN_FACULTY", os.path.join(DATA_DIR, "faculty.jsonl")
)
FACULTY_LIST_FIELDS = (
    "id", "name", "title", "school", "unit", "group", "group_kind",
    "is_phd_supervisor", "research_areas", "email", "homepage",
)

# `group_kind` 的判据。**逐字抄自 `collector/faculty.py` 的 `GROUP_KINDS`**（A 的参考实现），
# 因为 `faculty.schema.json` 把它列进了 required。
#
# 为什么要这个字段：浙大物理的师资导航把「中国科学院院士」「特聘教授」（**身份**）
# 和「凝聚态物理研究所」「浙江近代物理中心」（**机构**）挂在同一层，
# 混在一个 `group` 里界面上分不出组 —— C 做导师页时被这个绊住了。
# 身份用于徽标，机构用于分组。
#
# **顺序要紧**：「中国科学院院士」里同时含「学院」，先判身份才不会被机构抢走。
GROUP_KINDS = (
    ("身份", ("院士", "特聘", "教授", "学者", "人才", "计划", "杰青", "优青",
              "讲师", "研究员", "博导", "导师")),
    ("机构", ("研究所", "中心", "实验室", "学院", "学系", "系", "团队", "课题组")),
)
GROUP_KIND_FALLBACK = "其他"

# 论坛 facets 的初始种（A 产出）：10 个院系 + 公告/导师计数。
# unit 仍是自由文本，这只是**建议值** —— 不校正的话
# 「物理学院」和「物理系」会裂成两个版块。
UNITS_PATH = os.environ.get("TUIMIAN_UNITS", os.path.join(DATA_DIR, "units.json"))

# 论坛全文：非订阅者只给前 N 字 + truncated 标记（契约 api.md 第 5 节）
POST_BODY_FREE_LIMIT = 200

# 变更报告（`GET /api/changes`，契约 api.md 第 10 节）。
# 数据源是 A 的差分产物，**含完整记录、一度有 626 KB** —— 接口必须投影成精简字段
# （投影规则与取舍见 server/changes.py 的模块 docstring）。
CHANGES_PATH = os.environ.get(
    "TUIMIAN_CHANGES", os.path.join(DATA_DIR, "changes.json")
)

# 更新历史索引（`GET /api/changes/history`）。同目录下的 `<日期>.json`
# 是当天的完整变更报告 —— 这是"月度订阅"唯一的凭据来源。
HISTORY_INDEX_PATH = os.environ.get(
    "TUIMIAN_HISTORY", os.path.join(DATA_DIR, "history", "index.json")
)

# ---------- 第 6/7 步：论坛 + 付费墙的运行时状态 ----------

# 数据新鲜度（`GET /api/freshness`，契约 api.md 第 12 节）。
# A 产出的**事实**：每所学校上次抓取时间、条数、栏目数、失败码。
# **里面刻意不含"新鲜不新鲜"的判断** —— 阈值在 server/freshness.py 里，
# 免得数据层和接口层各定一套标准然后互相打脸。
FRESHNESS_PATH = os.environ.get(
    "TUIMIAN_FRESHNESS", os.path.join(DATA_DIR, "freshness.json")
)

# 抓取运行日志（`GET /api/admin/crawl`）。落在 server/var/ ——
# 它是**我们自己**的运行记录，不是 A 的产出。保留最近 20 次。
CRAWL_RUNS_PATH = os.environ.get(
    "TUIMIAN_CRAWL_RUNS", os.path.join(ROOT, "server", "var", "crawl_runs.json")
)

# **服务端自己的可变状态**（用户/订阅/帖子/额度）落在 server/var/，
# **绝不写进 data/out/** —— 那是 A 的管道产出目录，B 一个字节都不该往里写。
# 单文件 JSON，原子替换；`.gitignore` 里有 server/var/（用户帖子不该进版本库）。
STATE_PATH = os.environ.get(
    "TUIMIAN_STATE", os.path.join(ROOT, "server", "var", "state.json")
)

# ---------- 数据量突变守卫（B21，2026-10-19） ----------
#
# 2026-10-19 凌晨：`data/out/notices.jsonl` 被写残成 **69 条**（原 2751），
# 持续约 **3 小时**，而门禁**一条都没报** —— 因为所有断言都在验"形状"，
# 69 条合法记录和 2751 条合法记录在形状上没有任何区别。
#
# 这条守卫比较的是**变化**：当前条数比基线少超过这个比例就红。
# 它只抓"突然少一大截"；缓慢流失（每天少 1%）抓不到 —— 那种要靠每日快照做趋势，
# 这里不假装能做。20% 的含义是"一次事故级的缩水"。
#
# 基线在 `contracts/data_volume_baseline.json`（**提交进版本库**），
# 重出：`python server/tests/test_data_volume.py --write-baseline`。
DATA_VOLUME_DROP_LIMIT = float(
    os.environ.get("TUIMIAN_DATA_DROP_LIMIT", "0.20"))

# 审核后台 / 抓取控制的口令（单一共享口令，仍不是完整的权限系统：没有会话和角色）。
#
# 以前默认值是写死的 "demo-admin"，而且 /api/health 会把它原样返回 ——
# 服务一旦部署到公网，任何人都能下架帖子、触发抓取。现在：
#   · 设了 TUIMIAN_ADMIN_TOKEN 就用它（部署时务必设置）；
#   · 没设就每次启动随机生成一个，只打印在服务端控制台，不经任何接口返回。
# 部署版脱敏数据里，名单类公告的正文被整段替换成以这个前缀开头的说明
# （collector/tools/sanitize_for_deploy.py）。数据健康门禁据此跳过"正文太短"判据 ——
# 短是有意的，不是抓取出了空壳。
REDACTED_BODY_PREFIX = "【正文含公示名单"

ADMIN_TOKEN_FROM_ENV = bool(os.environ.get("TUIMIAN_ADMIN_TOKEN"))
ADMIN_TOKEN = os.environ.get("TUIMIAN_ADMIN_TOKEN") or secrets.token_urlsafe(18)

# ---------- 公网演示模式 ----------
#
# 部署到公网（魔搭创空间）时打开（Dockerfile 已设 TUIMIAN_PUBLIC=1）：
#   · 演示身份（u_demo / u_num_demo / u_demo_sub）**只读** —— 访客改不了画像、
#     订阅、关注，否则一个人点两下就把所有评委看到的演示弄坏了；
#   · 访客第一次写入时，前端会生成 `guest_xxx` 身份写到自己的名下（互不干扰）；
#   · 论坛写入关闭（公开页面不适合匿名发帖），公告 / 推送 / 匹配 / 套磁信照常只读可用。
PUBLIC_MODE = os.environ.get("TUIMIAN_PUBLIC", "").strip().lower() in (
    "1", "true", "yes", "on")
DEMO_USERS = frozenset({"u_demo", "u_num_demo", "u_demo_sub"})

# 请求体上限：写接口全是 JSON，正常画像/帖子远小于它；
# 没有上限的话一个超大 body 就能把单进程内存吃掉。
MAX_REQUEST_BYTES = int(os.environ.get("TUIMIAN_MAX_REQUEST_BYTES", str(256 * 1024)))

# 论坛分页上限，和公告一致
MAX_POST_PAGE_SIZE = 50

# ---------- 第 13 节：内容审核与合规 ----------
#
# **这里的数都是可调的**（演示时可能要放宽），**不要改代码** ——
# 改环境变量或者改这一行，并在 journal 里说一声。

# 处理时限（小时）。**写死一个数并说明理由**，不写"尽快"：
#
#   · 产品对举报人的承诺是"我们会看"，而"尽快"不是一个承诺，是一句好听话 ——
#     它无法被验证，也无法被违反，所以它不构成任何约束。
#   · 《民法典》第 1195 条要求"及时"采取必要措施，**没有给具体小时数**。
#     所以这个数是**我们自己的承诺**，不是法条抄来的 —— 说清这一点很重要，
#     免得有人以为 24 小时是法律要求。
#   · 选 24 小时的理由：一个人值班能覆盖的最小完整周期，且能跨过一个晚上。
#     更短（如 4 小时）夜里没人看会常态性违约，那这个指标就会立刻失去意义；
#     更长（如 72 小时）对"通知—删除"来说太慢，被侵权的一方等不起。
MODERATION_DUE_HOURS = float(os.environ.get("TUIMIAN_MODERATION_DUE_HOURS", "24"))

# 限速：**去重管"同一条"，限速管"广撒网"**。
# 去重（同一 reporter 对同一帖只计一次）拦不住一个人去举报 50 个不同的帖子，
# 也拦不住刷屏发帖 —— 那是两个不同的问题，需要两个机制。
#
# 滑动窗口，单位秒。**窗口在内存里，不跨重启**（见契约第 13 节的"最小实现"说明）。
POST_RATE_LIMIT = int(os.environ.get("TUIMIAN_POST_RATE_LIMIT", "5"))
POST_RATE_WINDOW_S = int(os.environ.get("TUIMIAN_POST_RATE_WINDOW_S", "3600"))
REPORT_RATE_LIMIT = int(os.environ.get("TUIMIAN_REPORT_RATE_LIMIT", "10"))
REPORT_RATE_WINDOW_S = int(os.environ.get("TUIMIAN_REPORT_RATE_WINDOW_S", "3600"))

# 申诉理由的长度上限（防有人拿申诉当第二个发帖框刷内容）
APPEAL_REASON_MAX = int(os.environ.get("TUIMIAN_APPEAL_REASON_MAX", "500"))

# ---------- 第 16 节：推送（事件 × 画像） ----------
#
# 事件流是 A 的产出（`append-only`，一行一个事件），**只读**。
# 它回答的是"历史上发生过哪些值得打扰用户的事"，和 `notices.jsonl`（现在库里有什么）
# 是两个问题 —— 见 contracts/api.md §16 开头那张表。
EVENTS_PATH = os.environ.get(
    "TUIMIAN_EVENTS", os.path.join(DATA_DIR, "events.jsonl")
)

# 演示样例事件。**只在 `?source=sample` 时读，绝不自动回退** ——
# 自动回退会让"今天没有新公告"被悄悄显示成"有一堆新公告"，
# 那正是本项目反复在防的"用一个假输入冒充真结果"。
EVENTS_SAMPLE_PATH = os.environ.get(
    "TUIMIAN_EVENTS_SAMPLE", os.path.join(ROOT, "contracts", "fixtures", "events.sample.json")
)

# 推送频率上限，按 (用户, 自然日) 计数。
# 为什么要上限：推送是**打扰**。没有上限的一天，用户第二天就会关掉它。
PUSH_DAILY_LIMIT = int(os.environ.get("TUIMIAN_PUSH_DAILY_LIMIT", "20"))

# 免费层能看到前几条推送。
# **故意不是 0**：一个只有付费才能看到的推送，用户在购买前无法判断值不值 ——
# 那不是付费墙，是赌博。给"看得见"的一部分，但截断必须说出来。
#
# **2026-10-19 从 3 改成 10**（B16，对标竞品后定的）：
# 新用户填完画像只看到 3 条，判断不了"这产品有没有用"—— 而他要判断的恰好是
# "推送靠不靠谱"。**信息本身免费，"帮你盯着"才收费**：
# 付费墙挪到「订阅提醒（T-7/T-3/T-1）」和「历史资料库」上。
# 10 条之后免费层能看见"今天对口的大半"，而"每天主动提醒你"仍是付费项 ——
# 那才是竞品的商业逻辑，也才是真差异。
PUSH_FREE_LIMIT = int(os.environ.get("TUIMIAN_PUSH_FREE_LIMIT", "10"))

# preview 的 limit 上限。**越界截断不报错**（理由见契约 §16.4：
# 为"给你看的东西"返回一个错误、让页面白掉，是拿用户体验换契约洁癖）。
MAX_PUSH_LIMIT = int(os.environ.get("TUIMIAN_MAX_PUSH_LIMIT", "50"))

# ---------- 第 18.9 节：首屏分层（B22） ----------
#
# 「参考资料」区（目录/简章）最多给几条。**故意不给全量**：
# 实测目录/简章 316 条，配上画像能命中几十条 —— 全给等于把折叠区变成第二个首屏，
# 而那正好是本轮要消灭的东西。10 条够让人"看到有这个区、点得进去"。
PUSH_REFERENCE_LIMIT = int(os.environ.get("TUIMIAN_PUSH_REFERENCE_LIMIT", "10"))

# 「本周值得看」这个数的时间窗口（天）。B23 的口径：近 N 天 + announcement
# + 非 junk + 去重。改这个数等于改对外那句话的意思，改之前先想清楚。
#
# **2026-10-19 从 30 改成 60**（用户拍板，B22）：推免季的公告 9 月发、
# 10 月底才截止 —— 只算 30 天，命中的公告自然稀缺（实测全库 30 天 17 条，
# 命中"浙大物理 + 北大/清华/中科大 + 凝聚态 + 直博"的只有 2 条）。
# 放到 60 天，命中的变 7 条，而且**对用户仍然有效**（报名期还没过）。
# 同一个窗口也用在首屏一级的判定上：窗口内的 announcement 即使被数据层标了
# `is_backfill`（发布超过 30 天），也照样进首屏 —— 否则"放宽窗口"只是改了页头的数。
PUSH_POOL_FRESH_DAYS = int(os.environ.get("TUIMIAN_POOL_FRESH_DAYS", "60"))

# 截止提醒窗口（天）。**窗口语义，不是精确某一天**：
# days_left <= N 就挂 T-N，取最紧一档（8 天不提醒，7 天 T-7，4 天也是 T-7，2 天 T-3）。
# 理由见契约 §16.6：推送的触发点是"用户打开看"，精确制会让他 4 天截止时收不到任何提醒。
REMINDER_WINDOWS = (7, 3, 1)

# 手动开通码 —— **这不是支付**。
# 没有微信支付商户号，所以开通用人工发码，文档和响应里都如实写明。
PUSH_ACTIVATION_CODE = os.environ.get("TUIMIAN_PUSH_CODE", "TUIMIAN-DEMO-2026")

# ---------- 第 22 节：推送响应的体积上限（2026-10-18 实测后补） ----------
#
# 2751 条数据下，`/api/push/preview` 的响应是 **535 KB**，其中
# `items` 只有 28 KB、**`suppressed` 523 KB（98%）** —— 把 2739 条被压掉的事件
# 全量塞进了响应。首屏因此接近半秒到 3 秒（取决于机器），演示第一屏就会"点了没反应"。
#
# 修法：**每个原因只保留前 N 条样例**，其余用计数说明。
# N=3 的理由：够让用户看到"哪几条被压了、为什么"，又不会把响应撑大。
# 想看全部就走 `/api/push/suppressed?reason=&offset=`（分页，契约 §16.8）。
PUSH_SUPPRESSED_SAMPLE_PER_REASON = int(
    os.environ.get("TUIMIAN_SUPPRESSED_SAMPLE", "3"))

# 画像里**参与推送匹配**的字段（最小集，见契约 §16.2/§16.5）。
# `school`（本科院校）只展示、不用于匹配 —— 本科院校不是"意向"。
PUSH_MATCH_FIELDS = ("preferred_schools", "majors", "interest_keywords", "preferred_units")

# ---------- 第 18 节：推送针对性（四维加权打分） ----------
#
# 为什么不再用 `relevance`（0/1/2）当排序主键：它是**数据层给的一条公告的**
# "像不像招生信息"，和"对你有多大关系"是两个问题。三档太粗 ——
# 实测 u_num_demo 的 20 条推送里，有 9 条 `relevance=2` 却一条画像都没命中的近似项
# 和真正命中的混在同一档里，只能靠 `urgency_weight` 决胜负。
#
# 四维各自 0~1，**加权求和后再乘 `urgency_weight`**（紧急度是另一个维度，
# 理由见 `push._sort_key` 的说明：乘表示"越对口、越紧急越靠前"）。
#
# 权重**必须可调**：这是产品口味问题，不是对错问题。演示时评委如果问
# "为什么这条排第一"，要能说"学校权重大"并且现场改一个数看效果 ——
# 硬编码在函数里就说不清了。改这里或者改环境变量，别改代码。
PUSH_SCORE_WEIGHTS = {
    # 意向学校是最强的信号：推免生基本按学校筛第一层。
    "school_hit": float(os.environ.get("TUIMIAN_W_SCHOOL", "0.35")),
    # 意向专业次之 —— 专业不对口，学校再好也没用。
    "major_hit": float(os.environ.get("TUIMIAN_W_MAJOR", "0.25")),
    # 方向（`interest_keywords`）比专业更细，但噪声也更大，权重略低。
    "direction_hit": float(os.environ.get("TUIMIAN_W_DIRECTION", "0.20")),
    # 新鲜度：产品的卖点是"今天该看什么"，旧公告的价值随时间衰减。
    "freshness": float(os.environ.get("TUIMIAN_W_FRESHNESS", "0.20")),
}

# 新鲜度分档（按**发布日**到今天的差，单位天）。契约 §18.2。
PUSH_FRESH_FULL_DAYS = int(os.environ.get("TUIMIAN_FRESH_FULL_DAYS", "30"))
PUSH_FRESH_MID_DAYS = int(os.environ.get("TUIMIAN_FRESH_MID_DAYS", "90"))
PUSH_FRESH_FULL = 1.0        # ≤ 30 天
PUSH_FRESH_MID = 0.6         # 31 ~ 90 天
PUSH_FRESH_OLD = 0.2         # > 90 天

# **发布日期读不到时给 0.5，不给 1.0 也不给 0.2。**
#
# 两个极端都是编造：给 1.0 等于"我们不知道它多旧，就当它是新的"——
# 而这正是 `is_backfill` 要消灭的那个错（把八年前的公告叫「新公告」）；
# 给 0.2 等于"不知道就当它旧"，那会把一条刚发但没写日期的公告压下去。
# 中间值 + `why` 里明说"发布日期未读到"，是把不确定说出来，不是假装确定。
PUSH_FRESH_UNKNOWN = 0.5

# 方向数据缺失时 `direction_hit` 的取值。
# 事件的可用方向信号只有 `matched[]`（数据层给的专题词）和标题 ——
# 两者都没有时**不能判成 0**：0 的意思是"我们看过，没有你的方向"，
# 而这里的真相是"我们没拿到方向数据"。契约 §14 那条原则在打分里的应用。
PUSH_DIRECTION_UNKNOWN = 0.5

# ---------- 第 18.8 节：负向维度（B20，2026-10-19） ----------
#
# 事故：QA 用"浙大物理 + 意向北大/清华/中科大 + 凝聚态 + 直博"的画像实测，
# 首屏第 1 条是「北京大学2027年高级管理人员工商管理（EMBA）专业学位硕士
# 研究生招生简章」，第 2 条是「北京大学（1101）报考点公告」，
# 真正的《北京大学 2027 年硕士研究生招生简章》排到第 3。
#
# **四维全是正向的**（学校命中就加分），于是"学校对了、这个项目根本报不了"
# 在分数里没有任何体现。缺的不是权重，是一个**负向维度**。
#
# 这里是**乘数**（1.0 = 不降权），不是过滤：
# - `degree_type` 压得狠：EMBA / MBA / 在职攻读 / 非全日制**要求工作经验**，
#   推免生（应届本科生）结构上报不了 —— 这是硬事实，不是口味；
# - `transactional` 同样压得狠，但理由不同：报考点 / 考场 / 网上确认是
#   **统考报名流程**里的东西。这个产品面对的是推免/夏令营人群，他们不走进
#   这条流程；而一条"报考点公告"带着报名截止日，紧急度天然很高（实测 ×1.2），
#   只轻压（0.8）根本压不住 —— QA 实测它照样稳居第 2。
#   所以压到 0.35：**仍会出现在列表里**（只排不滤），但不占前几条。
PUSH_MISFIT_WEIGHTS = {
    "degree_type": float(os.environ.get("TUIMIAN_MISFIT_DEGREE", "0.35")),
    "transactional": float(os.environ.get("TUIMIAN_MISFIT_TRANSACTIONAL", "0.35")),
}

# ---------- 第 2 节：`/api/match` 的打分口径来源（B18，2026-10-19 拍板方案 A） ----------
#
# **问题**：同一个画像，`/api/match` 和 `/api/push/preview` 给出不同的前 5 条。
# 根因不是谁实现错了，是**两套公式**：match 用六维（major/mode/level/affinity/
# year/freshness），推送用四维（school/major/direction/freshness）。
#
# **方案 A**：match 改成调 `push.score_dims`，两边共用一套口径；
# A 那边同步改 `collector/tools/match_preview.py`（参考实现不 import 本项目模块，
# 所以是把这段逻辑抄过去，约 20 行）。
#
# ⚠️ **默认仍是 `own`。** 参考实现没改之前切过去，门禁会红 7 条 ——
# 那 7 条不是要"改判据"，是**要等两边同时改完**（对照清单见 issues/012）：
#   match_响应形状与_fixture_一致 / match_与参考实现逐字段一致 /
#   match_分数真的按降序且降权关系成立 / match_默认不排除已截止的_显式开关还能用 /
#   match_item_带_field_trust_且_stats_恒等式成立 / match_stats_与源数据对得上 /
#   cred_unknown_不被当成_absent_统计与聚合
#
# 切换步骤（A 提交后）：① `TUIMIAN_MATCH_SCORE=push` 先跑一遍门禁，
# 一条条对清单；② 重出 `contracts/fixtures/match.response.json`；
# ③ 把下面的默认值改成 `push`，去掉环境变量依赖；④ 记 CHANGELOG。
MATCH_SCORE_FROM_PUSH = (
    os.environ.get("TUIMIAN_MATCH_SCORE", "own").strip().lower() == "push"
)

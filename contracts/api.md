# API 契约（**草案**，D1 由 B 定稿）

> 状态：草案。A 起草，B 有最终决定权。定稿后本文件冻结，改动记 `contracts/CHANGELOG.md`。
> 所有接口返回 `application/json; charset=utf-8`。
> 记录字段定义见 `notice.schema.json`，本文不重复。

## 0. 为什么先写这个

C 的前端只能对着接口写，不能等 B 写完再动手。所以接口路径和字段名必须在 D1 冻结。
**字段名一旦定了，改一次的成本是三个人的一天。**

## 1. 公告

### `GET /api/notices`

查询参数（全部可选，可组合）：

| 参数 | 类型 | 说明 |
|---|---|---|
| `school` | string | 学校全称（子串） |
| `unit` | string | 院系，支持多个逗号分隔 |
| `apply_mode` | string | `推免` / `夏令营` / `预推免` / `统考`，可逗号多选 |
| `level` | string | `硕士` / `博士` / `直博`，可逗号多选 |
| `year` | int | 招生年份 |
| `q` | string | 全文关键词 |
| `sort` | string | `publish_desc`(默认) / `deadline_asc`（**只排未来截止**、没截止日的沉底）/ `deadline_desc`（**过期优先**） |
| `page` / `size` | int | 分页，`size` 上限 50（默认 10） |
| **`limit`** | int | **`size` 的别名**（2026-10-19 补：它是合法参数，不能只白名单不生效） |
| **`offset`** | int | 从第几条开始，**优先于 `page`**（B25） |
| **`topic`** | string | `apply` / `non_apply` / `other`，可逗号多选（B25） |
| **`deadline_within`** | int | **只看 N 天内截止**：只接受 `1` / `3` / `7` / `30`（B25） |
| **`include_expired`** | bool | `sort=deadline_asc` 时是否把**已截止**的也列出来（默认 `false`；B25 修正） |
| `meta` | bool | 附带 `warnings` / `extract_meta` / `field_trust`（§14.2） |
| `include_junk` | bool | 是否包含 `record_quality=junk`（默认拒收） |
| `age_tier` | string | `live` / `recent` / `archive`，可逗号多选（2026-10-18） |
| **`notice_kind`** | string | **`announcement` / `catalog` / `emba` / `other`，可逗号多选或重复传**（2026-10-19，B24） |

**参数值不合法 → 400**（`BAD_NOTICE_KIND` / `BAD_AGE_TIER` / `BAD_SORT`），
**绝不静默当成"没筛"** —— 那会返回全量，用户以为筛过了。

> **⚠️ 未知查询参数不许静默忽略**（B24，2026-10-19 事故）。
> 改之前 `?notice_kind=announcement` 和 `?notice_kind=catalog` 的 `total`
> **一模一样**（都是 2419）：FastAPI 默认忽略不认识的查询参数，
> 前端照着文档做的筛选是彻底的假功能。
>
> 现在不认识的参数名会原样出现在响应的 **`unknown_params`** 里，并配一句
> `_unknown_note`。**只在有未知参数时出现**，默认响应形状仍严格等于
> `fixtures/notices.list.json`（所以不会把 fixture 回归打坏）。
> 选 200 而不是 422：前端可能带缓存穿透参数（`_=时间戳`），
> 422 会让整个列表白掉；而"照常返回 + 明确指出没认识的参数"既不挡展示，
> 也不让人误以为筛过了。

**按 `notice_kind` 筛时，会把这个字段回显到每条 item 上**：
它不在默认投影里（`LIST_FIELDS` 由 A 的 `make_fixtures.py` 定，
而 `notices.list.json` 是逐字段比对的标准），但"筛完看不见被筛的字段"
正是这一轮要消灭的静默失效。默认请求（不带 `notice_kind`）**不加这个键**，
形状不变。

响应：

```json
{
  "total": 18,
  "page": 1,
  "items": [ { "…": "符合 notice.schema.json 的完整记录" } ]
}
```

列表页为了速度可以裁掉 `body_text`，但**必须保留**下面这些字段（C 的卡片全靠它们）：

| 字段 | 为什么必须要 |
|---|---|
| `id` `title` `url` `school` `unit` `board` | 卡片主体 |
| `publish_date` `deadline` | **`deadline` 可以是 `null`，前端必须显示「截止日待确认」+ 提示，不许猜日期** |
| `apply_mode` `target_year` `degree_types` `majors` `tags` | 筛选和标签 |
| **`has_attachments`** (bool) | 「有附件一眼看出」——附件里常常才是正文 |
| **`attachments_count`** (int) | 同上，给角标数字用 |
| **`extract.confidence`** (number) | **< 0.5 要在界面上标「疑似」**（见 matching-spec 第 6 节） |

> 为什么把 `confidence` 放进列表：抽取本来就不完整，用户必须能看出"这条字段可能不准"。
> 不给他这个信息，他就会拿一个抽错的截止日去安排时间。

#### 默认排序 = `(relevance desc, publish_date desc)`

**两级，不是一级**：先按"像不像招生信息"（`relevance`，数据层的 0/1/2 三档）降序，
**同一档内仍按 `publish_date` 降序** —— 档内时效性不许被破坏。

- 为什么需要：只按日期排时，最新的那几条**行政/科研信息**（答辩评审名单、
  论文送审名单、Science 论文报道）会占据列表前几位，评委点开的第一条不是招生信息。
- **`relevance` 只用于排序，绝不用于过滤**（`collector/fields.py` 自己写明这个信号
  "两个方向都会误杀"：它把真的推免公示判成过非招生）。排序错了只是低几行，
  过滤错了是用户错过报名 —— 代价差一个量级。
- **结构性锁死**：`relevance` 不在 `LIST_FIELDS` 白名单里，所以它**进不了列表载荷**
  （`?meta=1` 也不给）。前端拿不到它，就没法拿它当筛选器。门禁
  `排序_relevance_不许混进列表载荷` 盯着这一条。
  （详情接口给的是**完整记录**，`relevance` 会随 `dict(rec)` 出现 —— 那被
  `fixtures/notices.detail.json` 钉着，是"详情给全"的语义；逐条拉详情要花每天 3 条额度。）

#### `?meta=1`：响应层现算的东西（**不动数据契约**）

默认形状必须等于 `fixtures/notices.list.json`（一个多余键都不加，门禁逐字段比对），
所以"响应层算出来的"一律走 `?meta=1`（先例：issues/004 的 `warnings`）：

| 位置 | 字段 | 含义 |
|---|---|---|
| item | `warnings` / `extract_meta` / `field_trust` | 见 §14.2 |
| 顶层 | **`data_state`** | `total == 0` 时分清"没数据"和"筛得太窄" —— 见 §15.3 |

```json
"data_state": {
  "loaded": 196, "matched": 0, "skipped_lines": 0,
  "source_empty": false,
  "empty_reason": "no_match",          // null（有结果）/ source_empty / no_match
  "note": "数据源有 196 条公告，但没有一条符合当前筛选条件 —— 放宽筛选试试"
}
```

### `GET /api/notices/{id}`

返回单条完整记录，含 `body_text` 与 `attachments`。

### 1.5 ⚠️ 年份：`admission_year` 是唯一权威，`target_year` 已 deprecated

两个字段都在响应里，但**含义不同、覆盖不同**，混用会让页面上的年份来回跳。

| 字段 | 含义 | 覆盖 | 状态 |
|---|---|---|---|
| `admission_year` | **这条公告在招哪一届** | 470/1120（只有 `topic=apply` 时才有值） | ✅ **权威** |
| `target_year` | 从标题/正文里抽到的年份 | 1005/1120 | ⛔ **deprecated** |

**为什么 `admission_year` 权威**：`target_year` 把"2027 年招生"和"2026 年的日常事务"
混在一起 —— 2026 只是**公告写于哪一年**，不是"招哪一届"。数据层（A3）把这两件事拆开了，
拆出来的是 `admission_year`。所以：

- **要判断"这条是不是 2027 招生"，只读 `admission_year`。**
- `admission_year == null` 的意思是「**这条公告不是在招某一届**」（讲座、拟录取名单、
  日常通知），**不是**"我们不知道"。
- `?year=` 的判据是 **`admission_year` 优先、没有才回退 `target_year`**（`store._year_hit`）。
  实测两者同时有值且不一致的只有 1 条（大连理工「连理全球」留学专项：
  `admission_year=2025` / `target_year=2026`）—— 那种情况下以 `admission_year` 为准。
- `/api/facets` 同时给 `target_years`（保留）和 **`admission_years`（新增，应改用这个）**。

**为什么两个都留、不直接删 `target_year`**：删字段是破坏兼容，而它现在还有
1005/1120 的覆盖（`admission_year` 只有 470）—— 那些非招生类公告上
`target_year` 仍然能回答"这条写于哪一年"。**留它是为了那个用途，不是为了判断招生年份。**

### 截止日排序与筛选（2026-10-19，B25；同日修正 `deadline_asc`）

这是学生最常用的动作（"哪条快截止了"）。**排序和筛选都只围绕"还来得及的"**。

**`sort=deadline_asc`（默认只排未来）**，顺序是：

```
未来截止（deadline >= 今天，日期升序）
  → [仅 include_expired=true] 已截止（日期降序，刚过期的先）
  → deadline 为 null（absent / unknown）沉底
```

- **已截止的默认一条都不出现**：改之前前 5 条是 2010 年的公告 ——
  "按截止日排序"要回答的是"哪些快来不及了"，不是"哪些早就过期了"。
- 想看已过期的：`include_expired=true`（它们排在**未来之后、null 之前**）
  或 `sort=deadline_desc`。
- **`deadline` 为 `null` 的必须沉底**，且不参与主序列 —— 第一页全是没日期的，
  会让人以为排序坏了。

**`sort=deadline_desc`（过期优先）**：它是 `deadline_asc` 的**镜像** ——

```
已截止（日期降序，刚过期的先）→ 未来（日期降序，最远的先）→ null 沉底
```

> 为什么 desc 不是"未来最远的先"：用户要这个排序是为了**看已过期的**
> （原话："想看已过期的，用 `sort=deadline_desc`"）。纯粹按日期倒序的话，
> 过期记录会沉到最底下，等于看不到。

**`deadline_within=N`**（N ∈ `1`/`3`/`7`/`30`，别的值 400 `BAD_DEADLINE_WITHIN`）：

| 算不算 | 为什么 |
|---|---|
| `found` 且 `0 <= 剩余天数 <= N` | 用户问的是"还来得及的" |
| **不含已截止**（`days < 0`） | 已经在 §16 沉底的规则里；这里再算进来只会把"还来得及"冲掉 |
| **不含 `unknown` / `absent`** | 那是替公告编一个截止日（三态不变量） |

**`/api/facets` 给两套桶** —— 因为"累计"和"可相加"是两种用途：

#### ① `deadline_buckets`：**累计**口径（给下拉的「7 天内 (18)」）

```json
"deadline_buckets": [
  {"value": "expired",   "label": "已截止",        "count": 692},
  {"value": "within_1",  "label": "1 天内",        "count": 3},
  {"value": "within_3",  "label": "3 天内",        "count": 7},
  {"value": "within_7",  "label": "7 天内",        "count": 18},
  {"value": "within_30", "label": "30 天内",       "count": 24},
  {"value": "later",     "label": "30 天以后",     "count": 12},
  {"value": "unknown",   "label": "未能读取截止日",  "count": 525},
  {"value": "absent",    "label": "未公布截止日",    "count": 1166}
]
```

> **⚠️ 这一套是累积的：`within_7` 包含 `within_3` 包含 `within_1`。**
> `within_N.count` 正好等于 `?deadline_within=N` 的 `total`（所以它适合做下拉），
> 但**不要把 `within_*` 相加** —— 相加会得到 2447，而总数是 2419。
> 只有这一种加法成立：`within_30 + later + expired + unknown + absent`。

#### ② `deadline_exclusive_buckets`：**互斥**口径（给饼图 / 要相加的场合）

```json
"deadline_exclusive_buckets": [
  {"value": "within_1",   "label": "1 天内",         "count": 3},
  {"value": "within_2_3", "label": "2-3 天",         "count": 4},
  {"value": "within_4_7", "label": "4-7 天",         "count": 11},
  {"value": "within_8_30","label": "8-30 天",        "count": 6},
  {"value": "later",      "label": "30 天以后",       "count": 12},
  {"value": "expired",    "label": "已截止",          "count": 692},
  {"value": "unknown",    "label": "未能读取截止日",   "count": 525},
  {"value": "absent",     "label": "未公布截止日",     "count": 1166}
]
```

- **八个桶互斥**，`sum(count) == total`（门禁盯着这条恒等式）；
- 两套桶由同一个 `剩余天数` 算出，不会出现两套口径各说各的；
- `unknown` 和 `absent` **必须分开**：前者要用户去看原文，后者是确定回答。

## 2. 匹配

### `POST /api/match`

请求（用户画像）：

```json
{
  "school": "浙江大学",
  "major": "物理学",
  "gpa_rank": "5/120",
  "english": "CET-6 580",
  "research": ["CUPT 国赛", "一篇二作"],
  "target_level": "直博",
  "preferred_units": ["物理学院", "材料学院"]
}
```

响应：**三个顶层键 `profile` / `stats` / `items`**（形状 = `fixtures/match.response.json`）：

```json
{
  "profile": { "…请求体原样回显…" },
  "stats": {"total": 470, "dropped_expired": 108, "dropped_mode": 46, "scored": 316},
  "items": [
    { "id": "ud1ddf924-…", "score": 0.87, "raw_score": 0.91,
      "bucket": "重点关注", "days_left": 21,
      "title": "2027年物理学院招收推免生综合考核实施细则",
      "school": "浙江大学", "unit": "物理学院",
      "publish_date": "2026-09-10", "deadline": "2026-10-17", "url": "https://…",
      "reasons": ["专业直接对口：物理学", "推免", "目标单位：物理学院"],
      "warnings": ["公告提到「托福」，若你没有相应材料请先确认是否影响申请"],
      "has_attachments": true, "attachments_count": 2,
      "extract": { "method": "rule", "version": "v5", "confidence": 0.83 },
      "field_trust": { "untrusted_count": 2,
                       "untrusted_fields": ["deadline", "attachments"],
                       "states": {"deadline": "unknown", "majors": "found",
                                  "attachments": "unknown", "body": "ok"},
                       "text": "这条有 2 个字段不可信：截止日、附件" } }
  ]
}
```

### ⚠️ 打分口径正在从"六维"迁到"推送的四维"（B18，2026-10-19 用户拍板**方案 A**）

同一个画像，`/api/match` 和 `/api/push/preview` 曾经给出不同的前 5 条 ——
不是谁算错了，是**两套公式**：match 用六维（major / mode / level / affinity /
year / freshness），推送用四维（school / major / direction / freshness）。

**方案 A**：`score_notice` 改成调 `push.score_dims`，两边共用一套口径；
A 同步改参考实现 `collector/tools/match_preview.py`（它不 import 本项目模块，
所以是把这段逻辑抄过去，约 20 行）。

现状（**必须知道**）：

- 代码已就位，开关是 `config.MATCH_SCORE_FROM_PUSH`（环境变量 `TUIMIAN_MATCH_SCORE=push`）；
- **默认仍是 `own`** —— 参考实现没改之前切过去，`match_与参考实现逐字段一致` 会红；
- `level` / `year` **不进四维分**（事件流里没有 `degree_types` / `target_year`）。
  它们仍留在 `reasons` / `warnings` 里解释"这条是不是你那届、层次对不对"，
  只是不再偷偷改动分数 —— **一边算一边不算，就不叫同一套口径**；
- A 提交后：开 `TUIMIAN_MATCH_SCORE=push` 跑门禁 → 重出 `match.response.json`
  → 把默认值改成 `push` → 记 CHANGELOG。对照清单见 `issues/012`。

> 另外：上面那个请求示例里的 `major` / `target_level`（单数）**以前会被静默忽略** ——
> 现在会**自动映射**到 `majors` / `target_levels`，并在响应里说明（§16.2，B19）。
> 契约自己的示例能打崩/打空自己的接口，是这一节最该记住的教训。

### `stats` 的四个数分别是什么（**这一节是 C 问的，一次说清**）

| 字段 | 含义 | 随数据变吗 |
|---|---|---|
| `total` | **数据源里全部记录数** —— **不是匹配结果数** | 会 |
| `dropped_expired` | 因"已过期"被硬过滤掉的 | 会 |
| `dropped_mode` | 因"招生方式不符"被硬过滤掉的 | 会 |
| `scored` | 过了硬过滤、真正参与打分的条数 | 会 |

**恒等式（门禁盯着）**：`total = dropped_expired + dropped_mode + scored`

### ⚠️ 已截止的**默认也要返回**（`exclude_expired` 是显式开关）

`dropped_expired` **默认是 0** —— 已截止的公告照样进结果。

理由三条（这是"不猜"这条主张最具体的一处落点）：

1. **截止日可能抽错。** 1119 条里 `unknown` 有 **293 条**，抽取准确率不是 100%。
2. **学校会延期。** 公告发布后追加附件说"延期到 X 月 X 日"是会发生的。
3. **补发的附件截止后照样有人要**（模板、表格）。

**压掉已截止 = 猜"这条肯定没用了"** —— 那违背整个产品的主张。

**能力仍然在**：profile 里传 `"exclude_expired": true` 就真的排掉一批
（前端要做"只看还能报的"筛选时用它）。**默认关、显式开。**

> 2026-10-18 修过一个坑：`collector/tools/profile-example.json` 里原本带着
> `exclude_expired: true`，于是**对着示例画像调 `/api/match` 时 `dropped_expired` 是 282** ——
> 看起来像"默认就压掉已截止的"。**示例画像代表"产品默认行为"，不该替用户做这个决定。**
> 已删掉，并加了门禁 `match_默认不排除已截止的_显式开关还能用` 四头钉住。

**`len(items)` 通常小于 `scored`**：`?limit=30` 意思是"只要前 30 条"，
**不是"只有 30 条匹配"**。想知道匹配总共有几条，看 **`stats.scored`** ——
既不是 `len(items)`，也不是 `stats.total`。

> 这是 C 的 Q7 第 3 条：他原来拿 `total` 当"匹配结果数"，
> 于是 30 条结果配一个 470 的 `total`，界面会写"共 470 条匹配" —— **那是错的**。

**`stats` 的数值会随抓取变，`fixtures/match.response.json` 里只是"某一次"的快照。**
门禁比的是**键**和上面那个**恒等式**，**不比具体数字** ——
"数据一改门禁就红"等于门禁在测"今天抓了几条"，那不是契约。

### 两个必须一次给全的字段

1. **`extract.confidence` / `has_attachments` / `attachments_count`**
   —— 匹配页要标「疑似」、要显示「有附件」。
   让前端为了这三个字段再按 id 拉一次列表接口是错的：实测那样只能关联上 24/30 条
   （分页和筛选会让 id 对不齐），剩下 6 条前端不敢标。
   **凡是前端渲染要用的字段，必须在同一个响应里给全。**

2. **`field_trust`** —— 形状和列表 `?meta=1` 里的**完全一致**
   （`untrusted_count` / `untrusted_fields` / `states` / `text`），
   **前端一套渲染逻辑**。它是**摘要**，不是 `field_state`（那是详情的形状）。
   匹配页只认这个键：没有它，页面只能靠 `warnings` 的文案去猜，猜不准就干脆不标。

## 3. 套磁信

### `POST /api/letter`

请求：`{ "notice_id": "...", "profile": {...}, "tone": "正式" }`

响应（形状见 `contracts/fixtures/letter.response.json`）：

> **`subject` 用完整标题，不截断**，格式是 `【<咨询类型>】关于<完整标题>的申请咨询`，
> 标题**不加书名号**。下面例子里那个 `…` 只是举例时省略，**不是"要截断"的规定**。
>
> 坑（已踩）：这句话原来有歧义 —— 我的 fixture 生成器按"要截断"理解、B 的实现按"不截断"做，
> 于是两边拼出的 subject 不一样，**测试红在了两边都没做错的地方**。
> 教训：**举例里的省略号必须写明"这是省略"，否则它会被当成规格读。**

```json
{
  "notice_id": "ucd1acaac-6667-585158",
  "subject": "【推免咨询】关于…的申请咨询",
  "body": "老师您好：\n\n我是…",
  "citations": [
    { "label": "公告标题", "quote": "东南大学2027年硕士研究生招生章程",
      "source": "title", "url": "https://yzb.seu.edu.cn/…" },
    { "label": "公告原文", "quote": "（2）参加全国大学英语四级考试取得合格证书或成绩在425分及以上…",
      "source": "body_text", "url": "https://yzb.seu.edu.cn/…" }
  ]
}
```

`citations` 是为了防止 LLM 编造导师信息 —— 每句话要能指回公告原文，前端可点开。**别省这个字段。**

**正文里的画像字段会被填进去**（2026-10-19，B24）：

| 画像字段 | 填进正文 | 没填时 |
|---|---|---|
| `school` | `我是浙江大学的…` | `____（请补充你的学校）` |
| `majors[0]` | `…的物理学专业本科生` | `____（请补充你的专业）` |
| `gpa_rank`（字典或字符串） | `GPA 排名 5/120` | `____（请补充GPA 排名）` |
| `english`（字典或字符串） | `英语 CET-6 580` | `____（请补充英语成绩）` |
| `research` | `科研经历 CUPT 国赛一等奖、…` | `____（请补充科研经历）` |
| `name` / `contact` | 落款 | `____（请补充姓名）` / `____（请补充联系方式）` |

> **改之前**正文写死了 `____`，画像填了学校/专业也还是
> 「我是____大学____专业的本科生」—— 用户看到的第一反应是"系统没读我的画像"。
> 现在：能填的填，填不了的留 `____（请补充…）`，**缺什么一眼看得出来**。
> `citations` 那条链**没有动**（条数、逐字回链、`url` 都不变）。

**改为对象数组（不是字符串）**，因为前端要能点回原文：`quote` 显示、`url` 跳转、`label` 分类。

> ⚠️ **注意 `letter.response.json` 是模板样例，不是 LLM 生成的。**
> 里面的 `body` 是拼出来的模板，但 `citations` 全部取自**真实公告原文**，逐条可回链。
> 接 LLM 后 `body` 由模型生成，**`citations` 必须仍然逐条可回链** —— 这是唯一的防编造手段。
> 要不要接 LLM、用哪个模型、key 谁出，见 `issues/005`，**目前未定，所以这一步暂时不做**。

#### 引文质检：**判不准就不给**（2026-10-17 加，§15.5）

这个功能的**全部卖点**是"引文逐条能点回原文"。**一条碎片引文就同时毁掉三件事**：
可信度、可回链性、以及"我们没编造"这个承诺。所以：

| 判据 | 处理 | 理由 |
|---|---|---|
| 正文引文是**表格行**（`\|` 分隔、制表符拼接） | **不给** | `19 \| 材料科学与工程学院 \| 李某某 \| …` 引在套磁信里毫无意义，老师只会觉得学生没读公告 |
| 引文以**省略号结尾**（`…` / `...`） | **不给** | 那是页面把原文截断了。**不补全、不猜** —— 补全就是编造 |
| 正文引文 **< 20 字** / 标题 **< 6 字** | **不给** | 两个字的名词读不出完整意思 |
| 正文引文**逐字不在原文里** | **不给** | 那就断了回链，等于编造 |

**宁可只有 3 条干净的，不要 4 条里混 1 条垃圾。**

丢掉的东西必须说出来（**不许静默降级**）：

```json
"quotes_dropped": [ { "source": "body_text", "reason": "表格行（不是能读懂的句子）" } ]
```

- **只给 `source` 和 `reason`，不给原文** —— 把表格行原样搬进响应，
  等于让它换个字段名继续出现在界面上。
- `warnings` 里同时给一句人话，说清"丢了几条、为什么、信里到底引了什么"。
- 纯表格式公告（拟录取名单、联系人表）**不再兜底引一行表格**：
  信里只引标题/材料要求/截止日期，并如实说明"正文是表格式内容，没有可引用的要求句"。
  （原来那条"兜底引表格 + 警告"实测不成立：警告没人读，能看见的只有表格行本身。）

> `materials` 里的单项（如「论文」「身份证」）**不加长度下限** —— 它们是结构化字段
> （`label` 已标明"材料要求"），砍掉会把"身份证""成绩单"这类正常材料一起砍掉。

## 4. 匿名论坛

> 2026-10-17 补齐。此前只有一张四行表，而这是最容易"各写一套然后对不上"的地方
> （B 在 `issues/007` 里列的缺口，下面逐条给定论）。

### 一条 post 的形状

```json
{
  "id": "p_20261017_0001",
  "unit": "物理学院",
  "tag": "推免经验",
  "title": "…",
  "body": "…",
  "author_display": "匿名·紫金港的松鼠",
  "created_at": "2026-10-17T01:20:00",
  "reply_count": 0,
  "report_count": 0
}
```

| 字段 | 前台可见 | 说明 |
|---|---|---|
| `id` `unit` `tag` `title` `body` `author_display` `created_at` `reply_count` `report_count` | ✅ | 就是上面这些，**一个不多一个不少** |
| `author_real_id` `created_ip` `review_status` | ❌ | **只在服务端存**；只在 `/api/admin/queue` 里返回 |

### `review_status` 枚举

| 值 | 含义 | 出现在 `GET /api/posts`？ |
|---|---|---|
| `visible` | 正常 | ✅ |
| `pending` | 待审（被举报后进入） | **❌ 绝不** |
| `hidden` | 已下架 | **❌ 绝不** |
| `rejected` | 举报不成立、恢复 | ✅ |
| `deleted` | 作者或管理员删除 | ❌ |

> **`pending` / `hidden` 绝不能出现在公开列表里** —— 否则审核队列形同虚设。
> 这是本节的硬规则，比字段名重要。

### 列表

`GET /api/posts?unit=&tag=&page=&size=`

- `unit` / `tag`：子串匹配，可逗号多选（和公告接口一致）
- 排序：`created_at` 倒序；分页同上，`size` 上限 50
- **只返回 `review_status` 为 `visible` 或 `rejected` 的**

### `GET /api/posts/facets`

返回已有取值，供前端做下拉建议：`{ "units": [...], "tags": [...] }`。

**`unit` 用自由文本**（不强制来自固定清单），但建议值由这个接口给。
**从公告和导师数据里派生院系清单是对的** —— A 已产出 `data/out/units.json`（院系去重列表），
`facets` 可以拿它当初始种，再并入用户实际发过的值。理由：自由文本不校正的话，
「物理学院」和「物理系」会裂成两个版块。

### `POST /api/posts`

请求：`{ "unit": "…", "tag": "…", "title": "…", "body": "…", "author_real_id": "u_demo" }`

响应：返回**完整的公开 post 对象**（即上面那 9 个字段）。

**`author_display` 由服务端派生，不接受前端传值。**
两个理由，第 2 条比第 1 条重要：

1. 前端传的话，作者能伪装成别人；
2. **派生必须是确定性的** —— 同一个人永远得到同一个代号，
   否则「匿名」会变成「每次都不一样」，用户连自己的帖子都找不到。

派生结果**必须不可逆**：不能从 `author_display` 反推出身份。
`author_real_id` 和 `created_ip` **一个都不出现在公开响应里**。

### `POST /api/posts/{id}/report`

请求：`{ "reason": "广告", "reporter_id": "u_demo" }`（`reason` 可选）

- `report_count += 1`
- **同一 `reporter_id` 对同一帖只能计一次** —— 否则找三个朋友就能把任何帖子压下去，
  匿名论坛会立刻变成互相举报的工具。这条是防滥用，不是可选项。
- 累计 **3 个不同举报人** → `review_status = "pending"`（进审核队列）。
  **阈值 3 是演示值**，真实产品要按流量调。

### `GET /api/admin/queue`

鉴权：请求头 `X-Admin-Token`，与 `server/config.py` 里的常量比对。

> ⚠️ **这是演示用的假鉴权，必须在响应和界面上标明。**
> 默认 token 写在代码里，并在 `GET /api/health` 里标出来 ——
> **不要让人误以为这是真的权限系统。** 真做鉴权和审计是另一件事。

返回 `pending` 状态的帖子，**含 `author_real_id` 和 `created_ip`** ——
这是「后台实名」的落点，也是《民法典》通知—删除规则下平台能定位责任主体的依据。

## 5. 订阅 / 付费墙

### 免费额度卡在哪

| 接口 | 是否受限 | 超额行为 |
|---|---|---|
| `GET /api/notices`（列表） | ❌ 不受限 | — |
| `GET /api/notices/{id}`（详情） | ✅ **每天 3 条** | `402` + `QUOTA_EXCEEDED`，**不返回正文** |
| `GET /api/faculty` / `/api/match` | ❌ 不受限 | — |
| `GET /api/posts`（列表） | ❌ 不受限 | — |
| 论坛**全文** | ✅ | 非订阅者只给**前 200 字** + `truncated: true` |

超额响应：

```json
{ "error": { "code": "QUOTA_EXCEEDED", "message": "今日免费额度已用完" },
  "upgrade_hint": "订阅后不限量，并开启截止日提醒" }
```

**返回正文就等于没有墙**，所以 402 里不含 `body_text`。

### 额度怎么计数（这条不写清会出事）

| 规则 | 值 |
|---|---|
| 计数维度 | **按 `user_id` + 自然日**（当天 00:00 重置） |
| `subscribed: true` 的用户 | **不计额度**（订阅了就不该再被拦） |
| 计数对象 | 只算 `GET /api/notices/{id}`，列表/匹配/导师/论坛列表都不算 |

> **坑（真实发生过，已查清）：** B 的 55 项契约检查里，
> `详情默认键集合与_fixture_一致` 会先拉列表再拉详情。
> 同一进程里前面的用例把当天 3 条额度用光，详情就返回 402 ——
> 断言 `"body_text" in detail` 失败。
>
> **而且不是随机的，是必然的**：用户的额度存在 `server/var/state.json`，**跨进程持久化**。
> 所以**当天第一次跑是绿的，之后每一次都红**，直到删掉状态文件或跨天。
> 实测：删掉 `server/var/state.json` 后立刻回到 54/55。
>
> **一个"当天第二次跑必红"的门禁，比没有门禁更坏** ——
> 大家会学会"跑之前先删状态"，然后开始无视红灯。所以必须修，两条都要：
>
> 1. **测试必须跑在全新的临时状态上**：套件启动时把状态重置（或用
>    `STATE_PATH` 之类环境变量指向临时文件），需要详情的用例各自用独立
>    `user_id` 或直接用 `subscribed: true` 的用户。**门禁必须可重复。**
> 2. **客户端必须按状态码分流**：402 的响应体里没有 `body_text`，
>    C 的详情页遇到 402 要显示付费墙，**不能当成"这条公告没有正文"**。

### 免费额度与演示的关系

演示时**用 `subscribed: true` 的 demo 用户**，让墙不要打断流程；
再单独准备一个 `subscribed: false` 的用户，用来**主动演示墙长什么样**。
两个身份都要有，否则要么演示被打断，要么评委看不到付费墙。

### `deadline` **不打码**（这条我同意 B 的判断，而且理由更硬）

B 不打算给 `deadline` 打码，理由是"公开公告的截止日期本来就能在原文看到"。同意，但更要紧的是：

**`deadline` 是抽取出来的字段，打码后前端会显示成「截止日待确认」，而实际上它有值 ——
那等于我们主动给用户一个错误信息。** 这跟产品承诺「消除信息差」正好相反：
不是把公开信息变私密就能创造价值，那只是制造新的信息差。

付费价值应该放在**增量服务**上：截止日**推送提醒**、公告**变更通知**、套磁信生成。
那些是"我们替你做了一件持续的事"，不是"我们把已经知道的东西藏起来"。

### `POST /api/subscribe`

```
请求：{ "user_id": "u_demo", "plan_id": "monthly" }
响应：{ "user": {…订阅后的 user…}, "order": {…}, "plans": [ … ] }
```

`order` 字段：`order_id` / `plan_id` / `amount` / `paid_at` / `status: "paid"` / `_note`。

> **响应里必须留 `_note`。** 比赛评委看到"支付成功"要能一眼看出是模拟的 ——
> B 的原话是"这是个诚信问题，不是小事"，我完全同意。
> **约定澄清**：`_note` 是**真实的响应字段**，因此**必须出现在 fixture 里**。
> 我此前定的"`_` 开头的键非契约"只适用于 **fixture 的说明性标注**（那些放在 `fixtures/META.json`），
> 不适用于接口自己返回的字段。一个代表"响应"的 fixture 必须恰好等于响应形状。

### 用户状态存哪

`server/var/state.json`（服务端自己的运行时状态），**不写 `data/out/`** ——
那是 A 的产出目录。`server/var/` 加进 `.gitignore`。**确认合适。**

## 6. 错误格式（统一，别各写各的）

```json
{ "error": { "code": "NOT_FOUND", "message": "公告不存在" } }
```

## 7. 冻结点

**D1 20:00 合流时定稿。** 之后 C 可以放心写前端，B 改字段必须走 `contracts/CHANGELOG.md` 并在 `journal/` 里 @C。

## 8. 导师

数据源 `data/out/faculty.jsonl`，契约见 `contracts/faculty.schema.json`。
这一层是产品承诺里「**专业契合**的实验室」唯一的支撑数据 ——
校级研招网只发招生章程和拟录取名单，方向和导师只能从院系师资页拿。

### `GET /api/faculty`

查询参数：`school` / `unit` / `group` / `q`（在姓名和研究方向里搜）/ `page` / `size`。

响应：

```json
{
  "total": 557,
  "page": 1,
  "items": [
    { "id": "zju-u285779f0-1a2b3c", "name": "付海龙", "title": "研究员",
      "school": "浙江大学", "unit": "物理学院", "group": "凝聚态物理研究所",
      "group_kind": "机构",
      "is_phd_supervisor": true,
      "research_areas": ["实验凝聚态物理", "极低温量子输运", "低维量子材料和量子器件"],
      "email": "teacher09@zju.edu.cn",
      "homepage": "https://person.zju.edu.cn/hailongfu",
      "matchable": true }
  ]
}
```

### ⚠️ `matchable`：**"557 位导师"这个数必须能被拆开说**

| | 条数（2026-10-17 实测） | 含义 |
|---|---|---|
| `matchable: true` | **379** | 有研究方向 → **能进匹配池** |
| `matchable: false` | **178** | 没有研究方向 → **名录级，只能看不能匹配** |

**为什么要有这个字段**：557 这个数如果直接对外说，是**虚报覆盖** ——
里面 178 位根本没有研究方向，`/api/faculty/match` 拿它们没有任何判断依据。
分成两层之后，对外能说的是一句可查的话：

> **557 位导师，其中 379 位可匹配**（其余 178 位是名录级，只做展示）

**判据只有一条：`research_areas` 非空。不按学校一刀切** ——
大连理工整体是名录级（方向覆盖 42%），但它那 **97 位有方向的照样可匹配**；
按学校剔掉会把能用的 97 位一起扔掉。

> 一处要纠正的估算：曾被描述为"424 可匹配 / 133 名录级"。
> 那个数是拿"大连理工方向缺失 58% × 233 ≈ 135"推的，
> **漏了浙大 21 人、北大 21 人也没有方向**。真实是 **379 / 178**。
> 以接口返回的为准，别照估算写 PPT。

### `POST /api/faculty/match`

请求：`{ "profile": {...}, "limit": 10 }`（画像里用 `interest_keywords`）
响应：`{ "profile": {...}, "items": [ { "…": "上面每条导师", "score": 0.82, "hits": ["高温超导"], "reasons": [...], "warnings": [...] } ] }`

> **只返回 `matchable: true` 的导师。** `matchable: false` 的**一条都不会出现**。
>
> 原因：没方向的导师命中 0 个兴趣词，只能拿到 `0.15×单位 + 0.15×层次` 的分，
> 排在最后但**仍然返回**，还带一条"无法判断契合度"的警告。两个后果都不是"少几条"：
> **① 匹配结果数虚高**（用户以为"40 个对口"，其实 18 个没法判）；
> **② 警告变成噪音**（每条都挂"无法判断"，用户会怀疑整页数据）。
>
> 所以匹配结果里**不会有**"该导师页面没有研究方向"这条警告 —— 那种导师根本不在结果里。
> 要"全部导师（含名录级）"请查 `GET /api/faculty`，用 `matchable` 区分。

**打分规则见 `docs/matching-spec.md` 第 8 节，参考实现 `collector/tools/match_preview.py --faculty`。**
（参考实现**没有**这个过滤 —— 它按"全部导师"算，用来验打分公式；
`/api/faculty/match` 在它之上多一层"只留可匹配的"。两者差异在门禁里显式体现。）

### 给 C 的两条硬要求（和公告那两条同样重要）

1. **`matchable: false` 的导师要标出来**（建议徽标「名录级」或灰色），
   并且**不要放进匹配结果的候选池** —— 要么不显示，要么明确写
   「这些导师没有研究方向，只能看不能匹配」。
2. **`email` 为空时不能显示成空**，要提示「未抽到邮箱，请联系前到主页核对」。
   当次数用 `/api/faculty/facets` 的 `missing_email` 查，别照旧文档写"50 人里 1 人"。

## 9. fixtures 怎么用于验收（**这条关系到“验收标准”还算不算数**）

`contracts/fixtures/*.json` 里混着**两种性质完全不同的东西**，必须分开对待 ——
不分开的话，「和 fixture 逐字段一致」这句话会慢慢变成一句靠运气的话。

| | 是什么 | 有效期 | 怎么用 |
|---|---|---|---|
| **形状** | 字段名、类型、嵌套、枚举、必填 | **永久** | **逐字段比对。这就是验收标准** |
| **数值** | `score`、`days_left`、`bucket`、`stats.dropped_expired` | **只对 `_as_of` 那一天有效** | **禁止跨日期比对数值** |

原因：`score` 里有 `freshness` 维度（按"距今 ≤30/≤90/≤365 天"分档），
`days_left` 和 `dropped_expired` 更是直接由当天日期算出来的。
**换一天跑，同样的代码会给出不同的分数** —— 那时测试红了，但代码没坏。

### 三条规矩

1. **`_` 开头的键一律非契约**，不要照它生成类型。
   （`_as_of` / `_time_sensitive` / `_note` 都是给人看的标注。）
2. **`match.response.json` 带 `_as_of` 字段**，列出哪些值依赖日期（`_time_sensitive`）。
   **要复现同样的数字，用 `--as-of` 重跑：**
   ```bash
   cd collector
   python tools/make_fixtures.py --as-of 2026-10-17
   ```
3. **演示前、合流前重跑 `make_fixtures.py`**，别把 fixture 当永久基准。
   跑完在 `journal/` 记一行"快照点 = \<sha\>"。

### 对 B 的 `/api/match` 验收建议

**逐条比对只允许在同一个 `_as_of` 下做。** 平时（真实数据 vs 旧 fixture）请只断言：

- 形状：字段名集合、类型、嵌套结构
- `stats` 的**键名**齐全（不断言数值）
- **不变量**：`items` 按 `score` 单调不增；`days_left` 与 `deadline` 自洽
- **边界说明**：fixture 的 `items` 是"当次 limit 下的前 N 条"，
  第 N 和 N+1 名的分数可能只差 0.01 —— **换一天就可能互换，
  所以"逐条一致"对真实数据比对不成立，这是设计如此，不是 bug。**

## 10. 今日更新（`GET /api/changes`）

> 2026-10-17 补。**此前契约里根本没有这个接口**，而 C 的今日更新页已经在
> **直接读 `data/out/changes.json`** —— 跨过了接口层，直接依赖 A 的内部产物。
> 这违反本项目自己的规矩（"前端不要直接读 data/out/，统一走接口"），
> 一旦 A 调整那个文件的字段，C 的前端会在没人察觉的时候坏掉。
>
> 这个接口是**"月度订阅"唯一可演示的落点**：用户付钱买的不是"有公告"，
> 是"哪一条是今天新出现的、哪一条被悄悄改了"。

### 形状（**精简版**，不是磁盘上那个文件）

```json
{
  "generated_at": "2026-10-17T02:10:00",
  "counts": { "previous": 78, "current": 78, "added": 0, "changed": 0,
              "reparsed": 0, "removed": 0, "reidentified": 0, "unchanged": 78 },
  "added":   [ { "id": "…", "title": "…", "school": "…", "unit": "…",
                 "publish_date": "…", "deadline": null, "url": "…",
                 "apply_mode": "推免" } ],
  "changed": [ { "id": "…", "title": "…", "school": "…", "unit": "…", "url": "…",
                 "fields": { "deadline": [null, "2026-11-20"] },
                 "body_changed": true } ],
  "reparsed": [ { "id": "…", "title": "…", "school": "…", "unit": "…", "url": "…",
                 "fields": { "title": ["…旧…", "…新…"] },
                 "body_changed": false } ],
  "removed": [ { "id": "…", "title": "…", "school": "…", "unit": "…" } ],
  "reidentified": [ { "id": "…", "old_id": "…", "title": "…" } ]
}
```

**和磁盘上 `changes.json` 的区别**：那个文件里 `added`/`removed` 嵌的是**完整记录**
（含 `body_text`，所以它一度有 640 KB）；接口返回**精简字段**，别把整条记录塞进响应。

### `changed` 和 `reparsed` 是两件完全不同的事（2026-10-17 新增 `reparsed`）

| | 含义 | 判据 | 用户该看到什么 |
|---|---|---|---|
| **`changed`** | **学校真的改了通知** | 正文变了**且** `extract.version` 没变，或**附件 URL 集合**变了 | 「这条通知被修改过」——**这是订阅付费的核心** |
| **`reparsed`** | **我们改了抽取代码** | 抽取器版本不同带来的正文差异 / 只有派生字段不同 / 附件只有 `text_len` 变 | 「我们改进了抓取，这条的信息更全了」 |

> **为什么 `changed` 要加"抽取器版本没变"这个条件**：正文是我们**抽出来的**，
> 抽取器一换正文必然不同。实测把 PDF 表格从"一个单元格一行"改成"按行拼接"之后，
> 67 个 PDF 的正文全变了 —— 不管归因的话，日报会说「学校改了 67 份通知」。
> `reparsed[].why` 会说明具体原因（"抽取器版本变了" / "附件抽取结果变了" / "派生字段不同"）。
>
> **附件的 `text_len` 是抽取结果，只有 URL 集合变化才算学校动作**（学校加/删了文件）。

> **为什么必须分开**：我修好标题解析器之后，日报立刻报出「有变化 22 条」——
> 而**公告一个字都没变**，是 22 条标题的抽取结果变了
> （从「…公示（持续更新）-电子科技大学研究生招生网」变成「…公示（持续更新）」）。
> 用户会以为学校改了 22 份通知。
>
> 这跟之前修过的 `reidentified` 是同一类错误：**把"我们的处理变了"说成"外面的世界变了"。**
> 日报只要开始说谎，就没有人再看它 —— 而"哪条被悄悄改了"正是我们收钱的那个功能。

**C：`reparsed` 不要显示成"通知被修改"。** 建议单独一栏、措辞是
「我们改进了抓取」；只有 `changed` 才配红色标记和提醒。

### 四条必须在界面上如实呈现的措辞

1. **`removed` 不是"公告被删除"**，是"**本轮没抓到**"——更可能是翻页、栏目改版或抓取失败。
   **绝不能显示成"通知撤销"。**
2. **`reidentified` 不是新增也不是消失**，是"**同一条公告的 id 变了**"（抓取算法调整）。
   B 的契约检查曾经因为它红了：30 条老公告被同时报成"新增 30"和"未抓到 30"。
3. **`reparsed` 不是"学校改了通知"**，是"**我们改了抽取代码**"（见上表）。
4. **`added` 和 `changed` 都为空时，要明说"本次没有新增或变化"**，
   而不是显示一个空列表让人以为加载失败。

### C 的过渡做法

B 实现这个接口之前，页面**继续读 `changes.json` 作为回退**，
但把数据源抽成 `API_BASE` 驱动：接口通了就用接口，没通就落回文件。
**不要为了等接口把已经能用的页面停了。**

## 11. 关注与提醒（`/api/watch`、`/api/digest`）

> 2026-10-17 补。**用户付钱买的是"跟我有关的、变了的东西"。**
>
> `/api/changes` 回答的是"这一轮变了什么"——那是**所有人的、同一份**。
> 这一节回答的是"**和我有关的**变了什么"。两者之间差着一整条链路：
> 知道关心什么（关注）→ 记住它（落盘）→ 每次抓取后按它筛（匹配）→
> 说清为什么提醒（理由）→ 能点进去（notice_id）。
>
> 这也解释了月度订阅的定价逻辑：**没有关注，`/api/changes` 每天都是一样的**
> （反正是那几条），用户看两天就不会再打开。有了关注，**每一天的结果都是他没见过的**，
> 而且"错过一条"是有真实代价的（截止日、材料要求）。这才是复购的理由。

### 存储与鉴权（沿用现有的，**不新造一套**）

- 关注规则存在 `server/var/state.json` 的 `watches` 里，和帖子/订阅同一个文件、同一套原子替换。
  **不写 `data/out/`** —— 那是 A 的产出目录。
- 身份用**现有的假登录态**：查询参数 `?user=u_demo`
  （和 `/api/notices/{id}`、`/api/me`、`/api/posts` **完全一致**）。
  **注意名字**：查询参数是 `user`，而 POST body 里的字段叫 `user_id`
  （和 `/api/subscribe` 一致）。这不是笔误，是项目已有的约定 —— 新接口跟着它走，不另起一套。
  > 我自己就在这里栽过：接口声明的参数名是 `user`、契约写成了 `user_id`，
  > 于是 `GET /api/watch?user_id=u_t` **静默回落到默认用户 `u_demo`** ——
  > 不报错，只是所有关注都"消失"了。是端到端测试抓出来的。
  > 传错参数名不报错、只是结果为空，是最难发现的那类问题。
  D3/D4 不做真登录，这一节也不引入 token。
- 唯一用 `X-Admin-Token` 的还是审核后台那三个接口。

### 一条关注规则（watch）的形状

```json
{
  "id": "w_9f8c1e2a4b6d",
  "user_id": "u_demo",
  "school": "浙江大学",
  "unit": "物理学院",
  "keywords": ["夏令营", "推免"],
  "created_at": "2026-10-17T10:20:00"
}
```

| 字段 | 说明 |
|---|---|
| `school` | 可选。学校全称，**子串匹配**（和 `GET /api/notices` 的 `school` 同一套语义） |
| `unit` | 可选。院系名，**子串匹配**（同上，和列表页筛选一致） |
| `keywords` | 可选，字符串数组。**任一**命中即可（OR），在标题**或正文**里找（和 `/api/notices` 的 `q` 同义） |

> **为什么 `school` / `unit` 用子串而不是精确相等**：要和列表页筛出来的东西**一致** ——
> 用户在列表页筛「物理」看到 12 条，关注「物理」之后收到的就该是同一批。
> 两套语义（筛选用子串、关注用精确）会让人以为提醒漏了。

> **三个字段至少给一个。** 都不给 = 关注"所有变化"，
> 那和 `/api/changes` 没区别，等于用户没表达任何意图 → 返回 `400 BAD_REQUEST`。

**`id` 由规则内容确定性派生**（`w_` + 归一化后 `(school, unit, sorted keywords)` 的 sha1 前 12 位）。
归一化 = 去掉首尾空白、空串按 `null`、`keywords` 去重并排序。
→ **幂等是自然结果，不是额外加的判断**：同一条规则重复 POST 必然得到同一个 id。
换用自增计数器就做不到这点（第二次 POST 会造出两条一样的规则）。

### `POST /api/watch`

```
请求：{ "user_id": "u_demo", "school": "浙江大学", "unit": "物理学院", "keywords": ["夏令营"] }
响应：{ "user_id": "u_demo", "created": true, "watch": {…}, "total": 1, "watches": [ … ],
        "_note": "演示用假登录态；关注规则存在 server/var/state.json" }
```

- 新规则 → `created: true`，`201`；已存在 → `created: false`，`200`。
  **两种都是成功** —— 幂等接口不该因为"你已经关注过了"报错。
- 响应**带上 `watches` 全量**，前端不用再发一次 `GET` 就能重绘关注列表。
- 三个字段都没有（或都是空） → `400 BAD_REQUEST`，`message` 说明"至少要给 school / unit / keywords 之一"。

### `GET /api/watch`

```
请求：?user=u_demo
响应：{ "user_id": "u_demo", "total": 2, "watches": [ … ],
        "schools": ["浙江大学"], "units": ["物理学院"],
        "has_watches": true }
```

- 没有任何规则 → `total: 0, watches: [], has_watches: false`，**不是错误**。
- `schools` / `units` 是**去重后的值清单**，给前端画"你关注了哪些学校"的标签用
  （让前端自己从 `watches` 里 reduce 也行，但那就多一份实现要维护）。
- **顺序**：按 `created_at` 降序（最近关注的排前面），同一时刻按 `id` 排 ——
  避免同一个时间戳下顺序不稳定。

### `DELETE /api/watch/{watch_id}`

```
请求：?user=u_demo
响应：{ "user_id": "u_demo", "deleted": "w_9f8c1e2a4b6d", "total": 0, "watches": [] }
```

- **取关不存在的 id（或不属于这个 user 的 id）→ `404 NOT_FOUND`**，
  `message`："没有这条关注规则（可能已经取关过了）"。

> **为什么不定成 200（"反正结果都是它不在了"）**：
> 那会把**前端传错 id 这类 bug 静默吃掉** —— 用户以为取关成功了，
> 规则还在，下次 digest 又冒出提醒，而日志里全是 200。
> 演示时更怕这个：点击"取消关注"看起来成功了却还在收到提醒，是当场翻车的形态。
> 宁可报 404 让人看见。

### `GET /api/digest` —— 这一节的核心

```
请求：?user=u_demo&since=2026-10-15
响应：{
  "user_id": "u_demo",
  "since": "2026-10-15",
  "since_effective": "2026-10-15",
  "until": "2026-10-17",
  "watches": [ …规则快照… ],
  "total": 2,
  "items": [ … ],
  "days_covered": ["2026-10-16", "2026-10-17"],
  "empty_reason": null,
  "message": "关注 2 条规则，2026-10-15 以来命中 2 条",
  "_note": "提醒只包含 added / changed；reparsed 等不会出现（见下）"
}
```

一条 `items[]`：

```json
{
  "notice_id": "u285779f0-69694-3215858",
  "title": "…",
  "school": "浙江大学",
  "unit": "物理学院",
  "url": "https://…",
  "publish_date": "2026-10-16",
  "deadline": "2026-11-20",
  "change": "added",
  "changed_on": "2026-10-17",
  "fields": { "deadline": [null, "2026-11-20"] },
  "matched_by": [
    { "watch_id": "w_9f8c…", "reason": "学校=浙江大学" },
    { "watch_id": "w_3a1b…", "reason": "关键词=夏令营" }
  ],
  "why": "学校=浙江大学；关键词=夏令营"
}
```

- `notice_id` 用于**点进详情**（`GET /api/notices/{id}`）。
- `changed_on` 是**它出现在哪一天的差异报告里**，不是 `publish_date`。
  两者可以不同：一条 10-10 发的公告，10-17 才被我们第一次抓到 → `publish_date` 是 10-10、
  `changed_on` 是 10-17。**界面上要显示的是后者**（"今天告诉你的原因"）。
- `matched_by` 是**"为什么提醒我"**：命中了哪几条规则、每条的原因。
  一个公告可以同时命中多条规则（关注了学校 + 又关注了关键词），**全部列出**。
- `fields` 只在 `change == "changed"` 时有意义（`[旧值, 新值]`，形状同 §10 的 `changed[].fields`）；
  `added` 时给 `null`。

### **`digest` 只包含 `added` 和 `changed`（这条最要紧）**

`reparsed` / `reidentified` / `removed` **一条都不会出现**。

理由**直接复用 §10 已经建立的区分**：

| 桶 | 含义 | 进 digest？ |
|---|---|---|
| `added` | 出现了新公告 | ✅ **外面的世界变了** |
| `changed` | 正文或附件 URL 集合变了 | ✅ **外面的世界变了** |
| `reparsed` | **我们改了抽取代码** | ❌ 提醒它 = 说谎 |
| `reidentified` | **我们调整了 id 算法** | ❌ 同上 |
| `removed` | **我们这一轮没抓到** | ❌ 同上（见 §10 第 1 条措辞要求） |

> **把 `reparsed` 发进提醒是这一节最容易犯、后果最坏的错**：
> 用户收到"浙江大学 物理学院 有更新"，点进去发现**公告一个字都没变** ——
> 是我们改了表格解析。**提醒只要开始说谎，就没有人再看它**，
> 而"哪条被悄悄改了"正是我们收钱的那个功能。
> （§10 已经因为这件事把 `changed` 和 `reparsed` 拆成两栏，这里沿用同一个判断。）

### 同一公告在区间内变了多次 → **只提醒一条，取最近那次**

`items[]` 按 `notice_id` 去重。理由：提醒回答的是"**这条你需要看一眼**"，
不是"这是流水账"。同一条公告改了三次就提醒三次，用户第三次就会开始忽略提醒 ——
那比漏提醒更糟。

### 三个边界（**都要有确定的预期行为，别让它各写各的**）

**A. 空关注（一条规则都没有）**

```json
{ "total": 0, "items": [], "watches": [],
  "empty_reason": "no_watches",
  "message": "你还没有关注任何学校 / 院系 / 关键词 —— 加一条之后这里就会有内容" }
```
**不是错误。** 新用户没有关注是最正常的第一状态，返回 400 会让人以为接口坏了。

**B. 关注了，但区间内没有变化**

```json
{ "total": 0, "items": [], "watches": [ …非空… ],
  "empty_reason": "no_changes_in_range",
  "message": "关注了 2 条规则，但 2026-10-15 以来没有新增或变更" }
```

> **A 和 B 必须能分开**，这是两个完全不同的事实：
> A 是"你还没配"，B 是"配好了，这些天**真的**没事"。
> 都返回一个空数组、都不给 `empty_reason`，前端就只能写"暂无提醒"——
> 而用户分不清是自己没设置还是系统没在跑。

**C. `since` 早于有记录的时间**

```json
{ "since": "2026-01-01",
  "since_effective": "2026-10-16",
  "since_clamped": true,
  "earliest_available": "2026-10-16",
  "notes": ["我们只有 2026-10-16 起的记录，已按那天开始算（你请求的是 2026-01-01）"] }
```
**不报错，但必须显式告诉用户被夹了。**
悄悄换一个日期就算完是**诚实问题**：用户以为在查 1 月至今，实际只查了 10 月 16 日至今，
他会得出"这半年什么都没发生"的结论，而那是错的。

> **为什么用单独的 `notes` 数组，而不是把这句写进 `message`**：
> `message` 是**主要结论**，会被"命中 N 条"/"本轮没有变更"覆盖 ——
> B 的第一版就是写进 `message`，结果被覆盖掉，用户完全看不到自己被夹了，
> 而契约要求必须显式告知。而且一条消息里塞两件事，前端没法只挑其中一件显示。
>
> **约定**：`notes` 是"要注意的事"（日期被夹、日期在未来、归档未建立…），
> 可能有多条、按发生顺序；`message` 是**这一条结论本身**。
> 两者都要给 —— 只看 `notes` 会不知道结果是什么，只看 `message` 会漏掉前提。

**C2. `since` 在未来** → `total: 0`，`since_in_future: true`，
`notes` 里说明"开始日期在未来的，所以不会有内容"。同样不报错。

**D. 一条记录都没有（归档未建立）**

```json
{ "total": 0, "items": [], "days_covered": [],
  "empty_reason": "no_records_at_all",
  "message": "还没有任何变更记录（每日归档未建立）—— 关注加上了也暂时不会有内容",
  "_error": "…" }
```
**和 B 必须分开。** 没有记录时说"没有变化"是**说谎** ——
我们不知道有没有变化。这条是本项目反复出现的同一个原则（见 §10 的措辞要求）。

### 与 `GET /api/changes/history` 的口径对齐

`history.days_with_changes` 原来把**五个桶都算作"这天有动静"**（含 `reparsed`）。
那回答的是"**我们的管道每天在跑吗**"（运维视角，是对的）。

但用户问"过去 7 天变了什么"时，含义是"**外面的世界**动了什么"。
两个口径混用就会出现互相打脸：

```
/api/changes/history  →  过去 7 天里有 3 天有变化
/api/digest?since=...  →  你的提醒是空的
```

用户看到这两句会认为**有一个是坏的**。

所以 `history` 增加一个字段：

| 字段 | 含义 | 用哪些桶 |
|---|---|---|
| `days_with_changes` | 管道产出过变更记录的天（**保持原样**） | 五个全算 |
| `days_with_user_changes` | **"外面的世界变了"的天** | 只有 `added` / `changed` |

**`digest` 的 `days_covered` 与 `days_with_user_changes` 必须能对上**：
在 `[since_effective, until]` 这个区间里，凡 `days_with_user_changes` 里的天，
`digest` 要么给出命中项，要么（因为关注规则不匹配）明确为空 —— 但**绝不可能出现
"history 说这天有变化、digest 连这天都不认识"**。端到端测试里有一条专门盯这个。

> 顺带：`index.json` 的 `days[]` 目前**没有 `reparsed` 列**（A 的 `run_daily.py` 还没写）。
> 这不影响上面的口径 —— `days_with_user_changes` 只读 `added` / `changed`，
> 这两个列在索引里是有的。等哪天补上 `reparsed`，`days_with_changes` 会更准确，
> 但 `days_with_user_changes` 不会变。

## 12. 数据新鲜度与调度

> 2026-10-17 补。**产品最核心的承诺是"每天更新"，而在这一节之前，这句话不可验证。**
>
> 具体是三个洞：
> 1. **没有任何接口能回答"数据是什么时候的"。** 评委问一句"这数据什么时候抓的"，
>    我们只能翻文件系统 —— 那是演示当场露馅的形态。`/api/changes` 说"变了什么"、
>    `/api/changes/history` 说"过去几天跑过"，**都不是"现在这份数据多新"**。
> 2. **没有调度。** 抓取只有手动跑脚本，"每天更新"是**承诺**不是**机制**。
> 3. **抓取失败不可见。** 某校今天没抓到，界面上和"没变化"长得一模一样 ——
>    用户以为"这学校最近没动静"，实际是我们坏了。
>
> 数据侧的事实由 A 产出在 `data/out/freshness.json`（字段是 A 先定、B 照写的）。
> **数据层只报事实，不做"新鲜不新鲜"的判断** —— 阈值是接口层的事，
> 免得两处各定一套标准然后互相打脸。

### 数据源 `data/out/freshness.json`

```json
{ "generated_at": "2026-10-17T08:20:16",
  "schools": [ { "school": "北京大学", "unit": "研究生招生网",
                 "homepage": "https://…",
                 "last_ok": "2026-10-17T08:20:16", "records": 14,
                 "boards": 6, "boards_empty": 1,
                 "failed": ["tls_handshake"], "freshness_hours": 0.0 } ],
  "totals": { "records": 72, "schools": 37, "failed_schools": 1 } }
```

### ⚠️ 完整性不变量（2026-10-18 事故后补，门禁盯着）

**事故**：A 逐校分批跑，某一批把 `freshness.json` **整个覆盖成了 2 所学校 / 0 条记录**，
而主库是 2751 条 / 29 所。门禁当时 193 条**一条都没发现** ——
因为所有断言都只看 `notices.jsonl` 和事件流，**没人核对过 freshness 和主库对不对得上**，
而 `/api/freshness` 和页头都读它。

三条不变量（`server/tests/test_data_health.py`，判据 12–14）：

| # | 不变量 | 事故里它报什么 |
|---|---|---|
| 1 | freshness 里出现的**学校名集合** == `notices.jsonl` 的学校名集合 | 「只在主库里：27 所」 |
| 2 | freshness 的 `records` **合计** == `notices.jsonl` 行数 | 「合计 0 ≠ 主库 2751」 |
| 3 | `generated_at` **不早于** `notices.jsonl` 的 mtime 超过 5 分钟 | 「它是过期快照」 |

第 3 条为什么给 5 分钟容差而不是严格"不早于"：freshness 是按学校分批写的，
而 notices/fixtures 可能在其后几十秒被同一轮流程重写（实测差 **121 秒**）。
严格不早于会变成每轮都红，红久了就没人看；**5 分钟足够抓住真事故**
（那次落后了整轮，差几小时）。

> **`totals.schools` 的口径要注意**：它数的是 **学校+单位** 的行数（实测 37），
> 而真正的学校数是 **29**。前端别把 37 当"覆盖了 37 所学校"。

### **`freshness_hours` 不能用（这是接口层必须自己算的唯一一个字段）**

文件里那个 `freshness_hours` 是**生成时刻算的快照**，实测 37 所学校全是 `0.0`
（因为 `last_ok` 都等于 `generated_at`）。文件放着不动，它就是 0；
**隔 20 小时后它还说 0** —— 拿它当新鲜度会得出"永远刚更新过"的结论。

所以接口层**一律自己按 `last_ok` 和当前时间现算** `age_hours`。
`freshness_hours` 只是数据层的副产品，**不参与任何判断、不出现在响应里**。

> 同理，`last_ok` 在 A 的实现里是**无条件**写成"本轮时间"的（失败的学校也写），
> 所以它的语义是"**最后一次尝试**"而不是"最后一次成功"。
> 这直接导致"抓取失败不可见"那个洞 —— 见下面 `status` 的三态划分。

### **stale 阈值：30 小时（写死，理由如下）**

```
STALE_AFTER_HOURS = 30
```

- **下限由节拍定**：管道是**一天一轮**，所以 24 小时以内的数据必须算新鲜 ——
  否则每次正常运行都会有一段时间被标成"过期"，那个标记就没人信了。
- **上限由容错定**：24 小时节拍 + **6 小时宽限**。
  少跑一轮不该立刻报警 —— 早上 08:20 跑完，到第二天 14:20 才算过期，
  整个上午的演示都是安全的；而**真的漏了一整天**一定会被标出来。
- **为什么不按"学校自己应该多久更新一次"分档**：我们没有那个先验，
  编一个出来就是假装知道。**一个数、说清理由**，比一套看起来很聪明的分档诚实。

> 这是一个**工程约定，不是事实**。所以要出现在响应里（`stale_after_hours`），
> 让前端和读接口的人看得见判断依据 —— 不能藏一个数在代码里当"客观标准"。

### `GET /api/freshness`

```json
{ "generated_at": "2026-10-17T08:20:16",
  "now": "2026-10-17T20:41:00",
  "generated_age_hours": 12.35,
  "generated_age_text": "12 小时前",
  "oldest_hours": 12.35,
  "stale_after_hours": 30,
  "worst": "ok",
  "totals": { "records": 72, "schools": 37,
              "failed_schools": 1, "stale_schools": 0, "empty_schools": 1 },
  "schools": [
    { "school": "北京大学", "unit": "研究生招生网", "homepage": "https://…",
      "last_ok": "2026-10-17T08:20:16",
      "age_hours": 12.35, "freshness": "fresh",
      "records": 14, "boards": 6, "boards_empty": 1,
      "failed": ["tls_handshake"], "has_failure": true,
      "empty": false, "status": "failed" }
  ],
  "source": "…/data/out/freshness.json",
  "_note": "stale_after_hours 是接口层的工程约定（一天一轮 + 6 小时宽限），不是事实；age_hours 按 last_ok 现算，不用文件里的 freshness_hours。" }
```

**给 C 渲染用的三个数字**（首页那行"**72 条 · 最近更新 12 分钟前**"）：
`totals.records` + `generated_age_hours` + `generated_age_text`。
`generated_age_text` 是**现成的中文文案**（"12 分钟前" / "3 小时前" / "2 天前"），
因为按本项目的规矩"凡是前端渲染要用的字段，必须在同一个响应里给全" ——
让 C 自己去算相对时间，会多一份实现要维护，而且三个页面还会算出三种写法。

### `status` 是**三态**，不是两态（这是"抓取失败不可见"的解药）

| `status` | 判据 | 用户该看到 |
|---|---|---|
| `failed` | `failed[]` 非空 | 「这次抓取**出错**了」+ **具体原因** |
| `empty` | `failed` 为空，但 `boards > 0` 且 `boards_empty == boards` 且 `records == 0` | 「抓到了页面但**一条都没解析出来**」——需人工看一眼 |
| `stale` | 上面都不成立，但 `age_hours > stale_after_hours` | 「**超过 30 小时**没更新」 |
| `ok` | 其余 | 正常 |
| （整体 `worst` 可能是 `unknown`） | 文件不存在 / 没跑过 | 「**还没有数据**」 |

**优先级 `failed` > `empty` > `stale` > `ok`**，但**三个信号都独立给全**
（`has_failure` / `empty` / `freshness`）—— `status` 只是给排序和上色用的最坏值。
一个学校可以同时"刚跑成功但有两个栏目报错"和"整体不 stale"，两个字段都要能看到。

> **`empty` 这一态是我加的最重要的一态，直接对应"某校今天没抓到，界面上和没变化长得一样"。**
> 实测真数据里浙大研招网就是 `boards=2 / boards_empty=2 / records=0 / failed=[]` ——
> **两个栏目全空、一条没抓到、而且没有任何错误码**，在旧口径下它是完美的"成功"。
> 只看 `failed[]` 抓不到这个形态。
>
> **不能说它是 `failed`** —— 我们不知道那个栏目是不是**本来就没有**招生信息，
> 也可能是栏目改版。所以措辞是"抓到页面但没解析出内容，需人工确认"，
> **不是**"抓取失败"，也**不是**"没有更新"。中间这一档必须存在，
> 否则用户会在两个错误的结论里选一个。

**整体 `worst`** = 所有学校 `status` 里最坏的那个（同一优先级）。
`schools` 为空或文件不存在 → `worst: "unknown"` 且有 `_error`。

### 从来没跑过 / 文件读不到

```json
{ "generated_at": null, "worst": "unknown", "schools": [],
  "totals": {"records": 0, "schools": 0, "failed_schools": 0,
             "stale_schools": 0, "empty_schools": 0},
  "stale_after_hours": 30,
  "_error": "还没有新鲜度数据（每日流程没跑过）",
  "message": "还没有数据 —— 跑一次每日流程就会出现" }
```
**不崩、不 500、不假装 `ok`。** "没有数据"和"数据是新鲜的"是两个结论。

### `POST /api/admin/crawl` / `GET /api/admin/crawl/{run_id}`

抓取**不能**在请求里同步跑完（真抓一轮 37 所学校是分钟级，还会打对方服务器），
所以是**提交 + 轮询**：

```json
POST /api/admin/crawl          （需要 X-Admin-Token，沿用现有假鉴权）
请求：{ "trigger": "manual" }
响应 202：{ "run_id": "run_20261017_204100_a1b2",
            "status": "running", "joined_existing": false,
            "started_at": "2026-10-17T20:41:00", "finished_at": null,
            "duration_s": null, "exit_code": null, "trigger": "manual",
            "command": ["…python…", "tools/run_daily.py"],
            "data_generated_before": "2026-10-17T08:20:16",
            "data_generated_after": null, "data_moved": null,
            "_note": "真实抓取在子进程里跑；状态用 GET /api/admin/crawl/{run_id} 轮询。" }

GET /api/admin/crawl/{run_id}   → 同形状，status ∈ running / ok / failed / interrupted
GET /api/admin/crawl            → { "total": 3, "runs": [ …最近几次… ],
                                    "current": null, "schedule_enabled": true,
                                    "interval_hours": 24, "first_delay_min": 5 }
```

**三个端点都要 `X-Admin-Token`。** 运行日志里带子进程输出（可能有内部路径），
属于运维面，不能公开 —— 我第一版漏了日志那一个的校验，是门禁抓出来的。

#### 统计：**数据到底动没动**

`data_generated_before` / `data_generated_after` 是抓取前后 `freshness.json` 里
`generated_at` 的值，`data_moved` = 两者是否不同。

**退出码 0 但数据没变，是另一种失败** —— 子进程"成功"了什么也没产出。
只看退出码会显示一个绿色的 `ok`，而这一轮一点用都没有。
所以这时 `error` 里会带一句提醒（**不判成 `failed`**：中途失败时
`freshness.json` 本来就不会重写，那时两个时间戳相同是正常的）。

**`data_moved` 拿不到时间戳时是 `null`（不知道），不是 `false`（没动）。**
把"不知道"说成"没动"是本项目一直在防的那类错误。

#### 单飞（single-flight）语义

**同时触发多次，只有一次真的跑，所有调用方拿到同一个 `run_id`。**

- 已经在跑 → 返回**同一个** `run_id`，`joined_existing: true`，`status: "running"`。
- 两种都返回 **202 Accepted**：请求都被接受了，只是一个是"我来跑"、
  一个是"已经在跑了，跟着看"。用 409 拒绝没有好处 ——
  调度器 + 手动同时触发是**正常情况**（演示时你会手动点一下），
  报错会让调用方以为出了问题。

> 为什么必须单飞：抓取会**打对方学校的服务器**。并发跑两轮不只是浪费，
> 是**对别人不礼貌**，而且会让 `crawl.py` 的覆盖写互相踩。
> 这条不是性能优化，是外部影响的责任。

**状态是 `running` 时 `exit_code` / `finished_at` 是 `null`** —— 不是 0。
0 会被读成"跑完了且成功"。

**`failed` 必须带原因**：`exit_code` 非 0，且 `tail` 里是子进程输出的最后几行。
**报 `failed` 却不给原因等于没报** —— 看的人只知道坏了、不知道坏在哪。

**服务重启时**：落盘的 `running` 条目会被标成 `interrupted`
（进程没了，那个 run 不可能还在跑）。**不能让它永远显示 running。**

> **B 不解析 A 的 md 运行报告**（`data/out/runs/<日期>.md`）。
> 那是对人写的文档，格式说改就改，解析它就是把接口绑在别人的排版上。
> 所以 `steps`（每步明细）这个字段**现在恒为 `null`**，并带说明。
> 要每步明细的话，需要 A 产一份**机器可读**的运行记录 —— 建议形状：
> `data/out/runs/<日期>.json` = `{"started_at","finished_at","records",
> "steps":[{"step","seconds","exit"}],"failed_steps":[…]}`。
> **在 A 给之前，接口层只报子进程自己的事实**（退出码、起止时间、输出尾部），
> 这些不需要解析任何人的排版。

### 调度

**进程内定时 + 手动，两条路。**

- 定时：默认 **24 小时**一次（和管道节拍一致），可用
  `TUIMIAN_CRAWL_INTERVAL_HOURS` 覆盖，`TUIMIAN_CRAWL_SCHEDULE=0` 可整体关掉。
- **启动时不抓。** 第一次检查延后 `TUIMIAN_CRAWL_FIRST_DELAY_MIN` 分钟（默认 5）。
  理由：演示时服务器一起来就开始抓 37 所学校，界面卡、网络吵，**很难看**；
  而且刚跑完的数据根本不需要再抓。
- **每次检查先看"该不该跑"**：`now - 最新 last_ok < interval` 就跳过并记一条
  `skipped`。**定时器只是"提醒该跑了"，判断依据永远是数据本身。**
- 落一份运行日志（`server/var/crawl_runs.json`，和帖子/订阅同一层），
  `GET /api/admin/crawl` 列最近 20 次，含 `trigger`（`manual` / `schedule` /
  `skipped_fresh`）—— **能看出"是定时跑的、还是人点的、还是因为数据还新而跳过"。**

> **进程内定时的边界（说清，免得当成 cron）**：服务器重启后计时重来，
> 所以一个频繁重启的服务永远不会触发定时。这是"最小可用"的选择 ——
> 真正的每日调度应该由**外部 cron / 计划任务**调 `POST /api/admin/crawl`，
> 或者直接跑 `run_daily.py`。进程内定时解决的是"演示时服务器一整天开着"的场景。

### stale 时前端该怎么措辞（**给 C**）

| 情况 | ❌ 不要写 | ✅ 要写 |
|---|---|---|
| 整体 stale | "服务异常" / "数据已过期" | 「**数据停留在 10 月 16 日 08:20**（30 小时前）」+ 一个"立即更新"按钮 |
| 单校 `failed` | "暂无更新" | 「**本次抓取失败**：tls_handshake」 |
| 单校 `empty` | "暂无更新" / "抓取失败" | 「**本次没解析出内容**，可能栏目改版」 |
| 整体 `unknown` | "暂无更新" | 「**还没有数据**，跑一次更新就有了」 |
| 正常 | —— | 「72 条 · 最近更新 12 分钟前」 |

**一句话原则**：**"没抓到"和"没有变化"必须长得不一样。**
把它们显示成同一句话，用户会安心地以为自己了解情况 ——
而实际上我们可能是坏的。这是本项目反复出现的同一条原则
（§10 的四条措辞、§11 的四种空结果，这里是第五次）。

## 13. 内容审核与合规

> 2026-10-17 补。**匿名论坛是整个项目法律风险最高的功能。**
>
> 评审只要问一句"**有人在上面骂了某个实验室，你们怎么处理**"，
> 在补这一节之前我们拿不出答案。
>
> 《民法典》的通知—删除（第 1195 条）要的不是"我们会处理"，是四件事：
> **收得到、处理得掉、留得下痕、说得出依据。**
> 反过来这是加分项 —— 能把这条链说清楚的匿名论坛，比"我们做了个论坛"强得多。

### ⚠️ 先说清楚哪些是完整的、哪些是**最小实现**

**写在这里，不藏在代码注释里** —— 半成品配上诚实的说明，比假装做完了强。

| 环 | 状态 | 说明 |
|---|---|---|
| **审计日志**（§13.2） | **完整** | append-only，结构上做不到改；跨重启；自带原文快照 |
| **处理时限**（§13.3） | **完整** | `age_hours` / `due_at` / `overdue` + 整体 `overdue_count` |
| 限速（§13.4） | **最小实现** | 滑动窗口**在内存里，不跨重启**；不做"新账号限制"（理由见下） |
| 申诉（§13.5） | **最小实现** | **单轮**申诉：一次申诉一个终态，没有多轮/升级通道 |
| 处置结果可查（§13.6） | **最小实现** | **只查不推送**：不发邮件、不发站内信，能查到就算 |

**不做"新账号限制"的理由**（这是判断，不是偷懒）：
D3/D4 **没有真登录**，`user_id` 是第一个请求时自动建的 ——
"账号年龄"因此没有意义：换个 `user_id` 就是全新账号，拦不住任何刷子；
而**正常用户第一次发帖就会被误拦**。加一个拦不住坏人、只挡好人的机制，
不如不做，并把理由写下来。

### 13.1 审计的粒度

一条审计**必须**有这六样，少一样都不叫"说得出依据"：

| 字段 | 说明 |
|---|---|
| `at` | 何时 |
| `actor_id` / `actor_role` | **谁**（`moderator` / `reporter` / `author` / `system`） |
| `post_id` | **哪条** |
| `action` | **什么动作**（见 `AUDIT_ACTIONS`） |
| `reason_code` / `reason_label` / `reason_note` | **依据** |
| `from_status` → `to_status` | **前值 → 后值** |

> **为什么 `from_status` 必须记**：只看 `to_status` 无法回答
> "这条是**原本好好的被下架了**，还是**本来就在待审里被判了**" ——
> 而这两件事的责任完全不同。

`action` 的可枚举取值（会写审计的动作，**就这些**）：

```
report           收到一条举报（重复举报不写，见下）
auto_pending     举报到阈值，自动转待审
hide             管理员下架
restore          管理员恢复
delete           管理员删除
appeal           作者申诉
appeal_upheld    申诉被驳回（维持原处置）
appeal_restored  申诉成立，已恢复
```

**发帖、删自己的帖、看帖都不写审计** —— 审计管的是**治理动作**
（有人被举报、被处置、申诉），不是全部活动。把它做成全站操作日志，
真正的那几条会被淹掉；而"说得出依据"要的是能立刻翻到那一条。

**重复举报不写审计**：它没有改变任何状态。同一条举报被点十次就有十条记录，
真正的那次淹没在里面 —— 那比不记还坏。

`reason_code` 是**固定枚举**（`illegal` / `harassment` / `privacy` / `defamation` /
`spam` / `irrelevant` / `other`）。**依据要落成类别，不能是自由文本** ——
自由文本没法统计，也没法给作者一个"不会暴露举报人"的说法。
未知类别一律落到 `other` 而**不报错**：举报入口不该因为类别写错
就拒绝一条真实举报（那是拿格式问题挡内容问题）。

### 13.2 审计日志：append-only，**结构上做不到改**

```
GET /api/admin/audit?post_id=&operator_id=&action=&limit=   （X-Admin-Token）
```

- **接口只提供读。没有 POST / PUT / DELETE。**
  而且这不是"我们约定不改"—— `StateStore` 上**只有 `append_audit` 和 `audit_log`**，
  没有修改、没有删除方法。**一条可以被改的审计记录，在法律意义上等于没有审计。**
- **独立的一份存储，不混进帖子记录里。** 帖子可能被删，
  审计不能跟着消失 —— 它是"我说得出依据"的**唯一**凭据。
- **每条自带当时的原文快照**（`post_snapshot`：标题/正文/院系/标签/匿名代号）。
  所以即使 `posts` 整个被清空，仍然能回答"当时下架的是什么内容、依据是什么"。
  **冗余是刻意的：审计宁可丑、宁可重复，不能丢。**
- 快照里**不含** `author_real_id` / `created_ip` —— 审计是给人看的记录，
  里面有身份信息就多一份泄露面；身份留在帖子记录里即可。
- **跨重启存活**：它在 `server/var/state.json` 里，和帖子/订阅同一套原子替换。
- 过滤：`post_id` / `operator_id`（即 `actor_id`）/ `action`；**新的在前**。

### 13.3 处理时限：**24 小时**（写死一个数，理由如下）

```
GET /api/admin/queue   →  每条带 reported_at / age_hours / due_at / overdue
                          整体带 overdue_count / due_hours
```

- **产品对举报人的承诺是"我们会看"，而"尽快"不是一个承诺，是一句好听话** ——
  它无法被验证，也无法被违反，所以它不构成任何约束。
- **《民法典》第 1195 条要求"及时"，没给具体小时数。** 所以
  **24 小时是我们自己的承诺，不是法条抄来的** —— 说清这一点很重要，
  免得有人以为 24 小时是法律要求。
- 为什么是 24：**一个人值班能覆盖的最小完整周期，且能跨过一个晚上**。
  更短（如 4 小时）夜里没人看会常态性违约，那这个指标立刻失去意义；
  更长（如 72 小时）对通知—删除来说太慢，被侵权的一方等不起。
- 可调：`TUIMIAN_MODERATION_DUE_HOURS`（**演示时可能要放宽，别改代码**）。

**`overdue` 只对 `pending` 判定。** 理由：

| 状态 | 算积压吗 | 为什么 |
|---|---|---|
| `pending` | ✅ **算** | 已经进队列、还没被处置 —— 正好是"积压"的定义 |
| `hidden` / `deleted` / `rejected` | ❌ | **我们动过手了**，它们不需要再被计进积压 |
| `visible` 但有举报（没到阈值） | ❌ | 阈值机制就是"多数人认为有问题才人工看"，**设计上自动不予处置**，不是积压 |

没有 `first_reported_at` 的记录返回 `overdue: false`，**不是 `true`** ——
**不知道不能算成"超时了"**（这条原则在本节出现第二次）。

**队列排序按"最该先看的"**：超时的在前，然后等得最久的在前。
原来按 `created_at` 倒序是"最新的在前"，对审核队列恰恰是反的 ——
**最老的举报最容易被一直压在最底下。**

`first_reported_at` 只记**第一次**举报的时间。用最后一次会让持续被举报的帖子
永远不超时 —— 而那正好是最该被看到的那些。

### 13.4 限速：去重管"同一条"，限速管"广撒网"

**这是两个不同的问题，需要两个机制，一个都不能省。**

已有的**举报去重**（同一 `reporter_id` 对同一帖只计一次）拦不住
**一个人去举报 50 个不同的帖子**，也拦不住刷屏发帖。所以：

| 桶 | 默认 | 环境变量 |
|---|---|---|
| 发帖 | 5 次 / 3600 秒 | `TUIMIAN_POST_RATE_LIMIT` / `TUIMIAN_POST_RATE_WINDOW_S` |
| 举报 | 10 次 / 3600 秒 | `TUIMIAN_REPORT_RATE_LIMIT` / `TUIMIAN_REPORT_RATE_WINDOW_S` |

**参数在 `config.py` 里，都是可调的**（演示时可能要放宽，别改代码）。

触发时返回 **429 + 明确原因和重试时间**，不是笼统的"太多请求"：

```json
{ "error": { "code": "RATE_LIMITED", "bucket": "发帖",
             "limit": 5, "window_s": 3600, "used": 5, "retry_after_s": 2314,
             "message": "发帖 太频繁：3600 秒内最多 5 次，已用 5 次，请 2314 秒后再试" } }
```
外加标准的 `Retry-After` 响应头。**只给一个 429 状态码，用户只能反复重试。**

**只记"成功的动作"**：校验失败的请求（400）和重复举报（`deduped: true`）
**不吃配额**。限速防的是"广撒网**发内容**"，而一条 400 什么内容都没产生 ——
把失败也算进去等于**用户表单填错三次就被锁一小时**。

> **最小实现**：窗口在**内存里，不跨重启**。落盘一份时间戳会让 `state.json`
> 一直涨，而它防的是"短时间广撒网"，重启清零对这个威胁几乎没有损失。
>
> **所以演示期间不重启服务**（2026-10-17 写死，别悬着）：重启一次 =
> 限速窗口清零 = 已经触发过的滥用又能再来一遍。这不是"应该没事"，
> 是一个**已知的、选择接受的**缺口，写在这里而不是留给下一个人猜。
> 真要跨重启，就把窗口写进 `state.json`（`state.py` 已经有串行化的写锁），
> 代价是每次发帖都多写一次盘。**演示前不要重启**。

### 13.5 申诉（**最小实现：单轮**）

```
POST /api/posts/{id}/appeal        （作者本人）
请求：{ "author_real_id": "u_x", "reason": "……" }
响应：{ "appeal_id": "ap_20261017_0001", "status": "pending", "post_id": "…",
        "submitted_at": "…", "appeal_status": "pending" }

POST /api/admin/appeals/{appeal_id}?action=uphold|restore&note=…   （X-Admin-Token）
响应：{ "appeal_id": "…", "result": "restored", "result_label": "申诉成立，已恢复",
        "review_status": "rejected", "resolved_at": "…",
        "audit_chain": ["report", "auto_pending", "hide", "appeal", "appeal_restored"] }
```

- **只有作者本人能申诉**（`author_real_id` 必须匹配）。不校验的话
  任何人都能把别人的帖子推进申诉队列。
- **只有 `hidden` / `deleted` 可以申诉**：那是作者会想要一个说法的两种状态。
  `visible` / `rejected` 本来就在台上；`pending` 等结果就行。
- **一次只有一条待处理申诉**：已有 `pending` 或已结案 → `409 CONFLICT`。
- **申诉带原文快照**（`post_snapshot` + 审计里也带一份）——
  **不能因为下架就丢原文**，否则没法和原文对照着判。
- 申诉终态只有两个：`upheld`（维持原处置）/ `restored`（恢复）。
  恢复后帖子回到 `rejected`（`VISIBLE_STATUSES` 里，前台可见）。
- **恢复之后审计里能看到完整链条**：举报 → 自动待审 → 下架 → 申诉 → 恢复。
  `audit_chain` 直接给出来，演示时一句话就能说完整件事。

> **最小实现的边界**：只有一轮申诉。没有"再申诉"、没有升级到上级、
> 没有申诉时限。真要上生产，这三样都得有 —— 但演示和"说清机制"不需要，
> 做一半的多轮流程反而更难解释。

### 13.6 处置结果可查（**别让举报人看到举报人是谁**）

```
GET /api/posts/{id}/moderation?user=u_x
```

**同一个接口按"调用方和这条帖子是什么关系"返回不同的量。**
这是"处置结果可查"和"匿名"之间的那根线：

| 谁是调用方 | 看到什么 |
|---|---|
| **作者** | 状态 + 状态人话 + **依据类别**（不是举报人）+ 时间 + 申诉进展 |
| **举报人** | **只有** `handled`（已处理/正在处理）+ `message` |
| 其他人 | `403`，看不到 |

- **给作者类别，不给他举报人**：作者有权知道依据，无权知道是谁。
- **给举报人只给结果，不给细节**：给了细节等于把"这条是不是因为我举报才被处理"
  反推出去 —— 而且他不需要知道。**举报人也不该看到别的举报人。**
- **不推送**（最小实现）：不发邮件、不发站内信。能查到就够了 —
  这也是刻意的：真做通知就要处理"发给谁、发几次、能不能退订"，
  那是另一个功能，不是这一节的机制。

### 13.7 一句话回答"有人在论坛上骂了某个实验室，你们怎么处理"

> **收得到**：任何登录用户可以举报，带固定类别；同一人对同一帖只计一次
> （防"找三个朋友压帖"），另有 5/10 次每小时的限速防广撒网。
> 到 3 次举报自动进待审、前台立刻不显示。
>
> **处理得掉**：审核队列按"超时的在前、等得最久的在前"排，
> 每条带 `age_hours` / `due_at` / `overdue`，整体给 `overdue_count` ——
> **"我们积压了多少"是界面上一个能看到的数**。**24 小时内**给出处置
> （这个数是我们自己的承诺，不是法条抄来的）。
>
> **留得下痕**：每次处置都写一条 **append-only** 的审计 ——
> 谁、何时、哪条、什么动作、**依据类别**、**前值 → 后值**，
> 并且**自带当时的原文快照**。接口层面**没有**修改和删除，
> 审计也**不随帖子删除而消失**。
>
> **说得出依据**：`reason_code` 是固定枚举；申诉恢复之后审计里能看到
> 完整链条（举报 → 下架 → 申诉 → 恢复）。作者在
> `/api/posts/{id}/moderation` 看得到自己帖子被处置了、按什么类别、什么时候；
> 举报人只看得到"已处理" —— **看不到举报人是谁**。

## 14. 字段可信度：值 / null / unknown 三态

> 2026-10-17 补。数据层（A）的 `field_state` 形状由 A 给，这一节写**语义和接口**。
>
> **现在 `deadline: null` 一个值同时表示两件完全不同的事：**
>
> - 「公告里就没有截止日」—— 读了全文，确实没有 → **用户该放心**；
> - 「我们没读到」—— 正文没抓到 / 是扫描件 / 页面是 JS 空壳 → **用户必须自己去看原文**。
>
> **对用户这是天差地别的两句话，而接口分不出来。**
> 评审问"这个还能报吗"，答成"没有截止日"而实际是我们没读到，
> 是**最伤信任**的一种错：用户按"没有截止日"做了决定，
> 而那个信息我们**根本没拿到**。

### 14.1 三态定义

数据层每条记录多一个 `field_state`：

```json
{ "deadline": "found|absent|unknown",
  "majors":    "found|absent|unknown",
  "materials": "found|absent|unknown",
  "contact":   "found|absent|unknown",
  "attachments": "found|absent|unknown",
  "body": "ok|generic|missing" }
```

| 态 | 含义 | 需要什么依据 |
|---|---|---|
| `found` | **抽到了值** | 字段里有值 |
| `absent` | **正文读到了、确实没有** | **必须数据层明确说** |
| `unknown` | **我们判不了** | 默认态 |

- **`absent` 是唯一需要证据的态。** 没有 `field_state` 的旧记录一律 `unknown`
  （见 §14.4）。
- `body` 是**另一套词**：`ok` = 站点选择器取到真正文，`generic` = 兜底选择器，
  `missing` = 没取到。
  **B 给这套词加了第四个取值 `unknown`**，只用于"老数据没记这一项"——
  **A 的产出器永远不需要发 `unknown`**。不加的话老数据必须在这三个里挑一个，
  而挑任何一个都是编造。
- **适用三态的字段是这五个**：`deadline` / `majors` / `materials` / `contact` / `attachments`。
  选它们的理由：**用户会据此做决定**（什么时候截止 / 招什么专业 /
  **要交什么材料** / **怎么联系** / 有没有附件）。
  `title` / `url` 不适用 —— 它们必然有值，三态没有意义。

### ⚠️ 2026-10-17：从 3 个字段扩到 5 个（`materials` / `contact`）

**为什么加**：这两项用户据以做决定的频次不比截止日低 ——
「要交什么材料」决定他这周做什么，「怎么联系」决定他敢不敢报。
而这两个字段的"没有"和"没读到"在界面上长得一模一样，**正是整套三态要消灭的东西**。

实测 470 条（回填后）：

| 字段 | `found` | `absent` | `unknown` |
|---|---|---|---|
| `deadline` | 132 | 241 | 97 |
| `majors` | 341 | 94 | 35 |
| **`materials`** | 214 | 189 | 67 |
| **`contact`** | 238 | 166 | 66 |
| `attachments` | 132 | 272 | 66 |

**判据复用同一个 `mark()`**，没有第二套阈值：
有值 → `found`；没值且正文可信（`body_state == "ok"` 且无扫描版附件）→ `absent`；
其余 → `unknown`。

#### ⚠️ `contact` 是复合字段：**有键不等于有值**

`contact` 的形状是 `{"email": …, "phone": …}`。
实测 470 条里 **470 条都有 `contact` 键，但其中 232 条是
`{"email": null, "phone": null}`** —— 一个"空壳"。

只判 `bool(dict)` 会把这 232 条报成 `found`（"有联系方式"），
而事实是**公告里没写**。那正是这个项目一路在防的那种错：
**一个看起来有值的字段，掩盖了"确实没有"。**

所以判据是 `bool(contact.email or contact.phone)`，**不是** `bool(contact)`。

**一个刻意的推断**：`field_state` 缺失、但字段里**有值**时 → 判 `found`。
"有值"本身就是证据，不需要 `field_state` 再确认一遍；
把一条 `deadline: "2026-11-20"` 报成 `unknown` 会让用户白跑一趟原文 ——
**那是另一种不诚实**。缺失只会推出 `found` 或 `unknown`，**永不推出 `absent`**。

### 14.2 接口暴露：列表给摘要，详情给完整

| 接口 | 给什么 |
|---|---|
| `GET /api/notices/{id}?meta=1` | **完整 `field_state`**（四个键保证都在）+ `field_trust` + `deadline_display` |
| `GET /api/notices?meta=1` | 每条的 **`field_trust` 摘要**：`untrusted_count` / `untrusted_fields` / `states` / `body` |
| `GET /api/match` | **不加字段**（形状被 fixture 钉住）。三态的出口是 `bucket`，见 §14.3 |
| `GET /api/quality` | **按学校 × 字段的三态比例** + 两个头条比率 |

**为什么 `meta=1` 而不是默认给**：详情/列表的默认形状必须等于
`contracts/fixtures/notices.detail.json` / `notices.list.json`，
而 `field_state` 现在**既不在 JSONL 里也不在 schema 里**。
这和当年 `warnings` 的处理是同一个理由（issues/004），照先例走。

> **A 落盘 `field_state` 之后**：它作为**数据字段**会随 `dict(rec)` 自动出现在
> 详情响应里，**接口代码不用改**；A 重出 fixture 时它就变成默认键之一。
> `?meta=1` 给的那份是**规范化后**的（老记录补全四个键，`_source: "inferred"`
> 标明是推断的）—— 前端照着渲染即可，**缺键比错值更难查**。

**`field_trust` 摘要的形状**：

```json
{ "untrusted_count": 1, "untrusted_fields": ["deadline"],
  "weak_fields": [], "has_untrusted": true,
  "states": {"deadline": "unknown", "majors": "found", "attachments": "found"},
  "body": "generic" }
```

**`untrusted` 只数 `unknown`。** `absent` **不算不可信** ——
它是一个**有依据的确定回答**，和 `unknown` 混在一起会让这个数字失去意义
（全是红等于没有红）。`weak_fields` 数"能用但要提示"的（目前只有 `body: generic`）。

### 14.3 核心不变量：**`unknown` 不许被当成 `absent`**

这一节存在的全部理由就是这句话。所以它必须在**排序 / 统计 / 聚合**三处都成立，
而不是只在字段里体现一下。

**① 排序**（`sort=deadline_asc`）原来把 `deadline is None` 一律 `or date.max` 沉底：

```
旧：[found 按日期] → … → [unknown 和 absent 混在一起沉在最底下]
新：[found 按日期升序] → [unknown] → [absent]
```

- **`unknown` 不能和 `absent` 一起沉底**：一个 `unknown` 的公告可能**下周就截止**，
  埋在 196 条下面等于没提醒。这就是要消灭的那个混淆。
- **`unknown` 也不排第一**：用户要的是"按截止日排"，第一页全是没日期的条目
  会让这个排序看起来是坏的。它夹在中间 ——
  **"我们不知道"比"确定没有"紧急，但没有日期就插不进 `found` 的真实顺序里。**

**② 统计**（`/api/quality`）三态**分列**，永远不给一个"没有截止日的条数"。
`deadline_missing`（`extract_meta` 里的旧字段）保留但**语义要读准**：
它只说明"**值不在**"，**不等于"没有截止日"** —— 值不在可能是 `absent`
也可能是 `unknown`。要区分请读 `field_state.deadline`。

**③ 聚合**（`/api/match` 的 `bucket`）新增一个桶名：

| 三态 | `bucket` |
|---|---|
| `unknown` | `需人工确认` |
| `absent` | **`无截止日`（新增）** |

两个桶的回答完全不同：前者是"**你去看原文**"，后者是"**这条不用赶**"。

> ⚠️ **C 要改的地方（一次性的）**：`web/assets/match.js` 里那份硬编码的
> `order = ['立即行动','重点关注','可规划','需人工确认','已过期']`
> 和 `web/assets/common.js` 里的桶提示表。
> **服务端的桶顺序是**（也是这一节的规范值）：
> `['立即行动','重点关注','可规划','需人工确认','无截止日','已过期']`。
> 不改的话，`无截止日` 的条目 `indexOf` 返回 -1、会飘到列表最前面。
> （`/api/match` 的响应形状被 fixture 钉死，**不能**把顺序塞进响应里，
> 所以只能写在这里。数据层落 `absent` 之前不会出现这个桶。）

### 14.4 老数据（没有 `field_state`）

**一律 `unknown`，而且要说清是推断的。**

```json
GET /api/notices/{id}?meta=1
{ "field_state": {"deadline": "unknown", "majors": "unknown",
                  "attachments": "unknown", "body": "unknown",
                  "_source": "inferred"},
  "deadline_display": {"state": "unknown", "text": "未能读取截止日，请点原文核对",
                       "value": null, "url": "https://…", "must_check_original": true} }
```

`_source: "inferred"` 就是"这是推断的，不是数据层说的"。
**不能因为老数据多就把它们说成 `absent`** —— 那正是这一节要防的事。

### 14.5 `unknown` 的措辞规范（**给 C**）

**`deadline_display` 已经把三种说法拼好了，直接用，不要自己拼** ——
三个页面各拼一遍会拼出三种写法，而且**很容易漏掉 `unknown` 那一支**
（漏掉就等于说了"没有"）。

| `field_state.deadline` | 显示 | 必须带原文链接 |
|---|---|---|
| `found` | 日期本身（如 `2026-11-20`） | 否 |
| `absent` | 「**该公告未公布截止日**」 | 否 |
| `unknown` | 「**未能读取截止日，请点原文核对**」 | **是** |

> ### ⛔ 绝对禁止
> **把 `unknown` 显示成"没有截止日"、"无截止日"、"暂无"、"——"、
> 或者干脆不显示这一行。**
>
> 不显示最危险：用户会以为"这条没有截止日"，于是**不去看原文**。
> 我们不知道，和它没有，**必须是两句不同的话**。
> 这是本项目反复出现的同一条原则（§10 的四条措辞、§11 的四种空结果、
> §12 的三态新鲜度、§13 的四条合规链，这里是第五次）。

### 14.6 `GET /api/quality`：让"我们哪块数据弱"变成能查的数

```
GET /api/quality                 整体 + 按学校 × 字段
GET /api/quality?school=浙江大学  只看一所
GET /api/quality?field=deadline  只看一个字段
```

```json
{ "total": 196,
  "deadline_unknown_rate": 0.6888,
  "body_generic_rate": null,
  "summary": "196 条记录：截止日 unknown 68.9%、正文 generic 无（正文抽取方式一条都没记录，generic 比例无从计算）",
  "overall": {"total": 196, "schools": 17, "fields": {
      "deadline": {"found": 61, "absent": 0, "unknown": 135, "total": 196,
                   "found_rate": 0.3112, "absent_rate": 0.0, "unknown_rate": 0.6888},
      "majors": {…}, "attachments": {…},
      "body": {"total": 196, "known": 0, "ok": 0, "generic": 0, "missing": 0,
               "unknown": 196, "ok_rate": null, "generic_rate": null,
               "missing_rate": null, "unknown_rate": 1.0,
               "_rate_base": "按「记了正文抽取方式的那些」算（known）；known=0 时比率是 null，不是 0"}}},
  "schools": [ {"school": "北京大学", "total": 35, "worst_field": "deadline",
                "fields": {…}} ],
  "states": ["found", "absent", "unknown"],
  "_why": "让'我们数据很全'这句话变成能查的数，而不是印象。" }
```

**这就是这一环的目的**：以后谁想说"我们数据很全"，**先来查这里**。
它和 `/api/freshness` 是一对 —— 那边是**数据侧**可观测性（每所学校什么时候抓的），
这边是**字段侧**可观测性（抓到的值可不可信）。

数字**全部按当前数据全集现算，一个都不硬编码**（门禁里有一条专门盯这个）。

#### 比率的分母（**0% 和"没数据"必须分开**）

- **三态字段**（`deadline`/`majors`/`attachments`）的分母是**全部记录**。
  `absent_rate: 0.0` 表示"数据层没有任何一条说 absent" —— 这是关于数据的事实，
  而 `unknown_rate: 0.6888` 摆在那里，读者不会误以为我们真验过。
- **`body` 的分母是「记了抽取方式的那些」**（`known`），**不是全部**。
  真实数据里 196/196 都是 `unknown`（老数据没记这一项）。
  按全部算的话 `generic_rate` 会是 `0.0` —— 读起来像
  "测过了，没有一条用兜底选择器"，而真相是**一条都没记录**。
  所以 `known == 0` 时 `generic_rate` / `ok_rate` / `missing_rate` 都是 **`null`**，
  `summary` 里说"**无**"并附一句"无从计算"。
- **`worst_field` 只在三态字段里选**：`body` 是另一个轴（抽取方式），
  混进来会让"哪个字段弱"退化成"哪个字段老数据多"。

> 这一条是**在真实数据上发现的**：第一版 `body_generic_rate` 报 `0.0%`，
> 而实际是 196 条一条都没记。**数字看着正常，意思完全反了。**

---

## 15. 鲁棒性：**喂坏的，看它响不响**（2026-10-17 补，E 写）

> **判据原话**：「功能跑通不代表鲁棒性就强，不希望做出来的只是偶尔成功的 happy case」。
>
> 到 §14 为止的 126 条门禁全绿 —— **但绿的道理是它们都在喂正常输入**。
> 坏数据、空数据、坏状态文件、畸形正文一个都没进过门禁。
> 所以这一节不定义新功能，只定义**"坏了必须看得出来"**，以及**在哪儿看得出来**。

三个问题，每一环都要能答：

1. **坏的时候，是失败还是"看起来正常"？**（这个项目栽得最多的一类）
2. **失败了，说不说得清是什么坏了？**（"解析失败"四个字等于没写）
3. **下一次坏，还测不测得出来？**（一次性人工验证 ≠ 鲁棒性）

### 15.0 一条总规矩：**能用，但要响**

这一节所有的取舍都服从它：

- **不因为一处坏就让整个站打不开**（演示现场一个字节坏了，用户要的是"还能用"）；
- **也不许"看起来完全正常"**（少了一条公告却什么信号都没有，那是更坏的一种）。

所以每一处"坏"都必须落在**一个机器能判断、人能读懂**的位置上，
并且**有一条门禁会在它发生时变红**。落在文案里不算 —— 文案没人读。

反过来同样重要：**"每次都报警"等于没有报警**。每一环都配一条对照断言
（正常输入不许响），否则那行红字会一直挂着，谁也不会再去看它。

### 15.1 数据加载：坏行 → `degraded`，不拒绝启动

`server/store.py` 的 `NoticeStore.load()` 遇到坏行**跳过、不抛**，
把 `line` / `error` / `excerpt` 记进 `load_meta`；`load_health(meta)` 把它变成三态：

| `data.load_state` | 含义 | 该做什么 |
|---|---|---|
| `ok` | 文件里每一行都解析成功 | 什么都不用做 |
| `degraded` | **至少一行解析失败/不是 JSON 对象/缺 id，已被跳过** | 看 `data.load.bad_lines`，修数据源 |
| `empty` | 文件读到了，但一条记录都没有 | 数据管道没跑/跑空了（≠ 筛选没命中，见 §15.3） |

`degraded` 排在 `empty` 前面判断：全是坏行时 `loaded == 0`，但根因是"坏了"不是"还没跑"。

**`/api/health` 的形状（`data` 之下，只增不减）**：

```json
"load_state": "degraded",
"load": {
  "state": "degraded", "usable": true,
  "loaded": 195, "lines": 196, "skipped_lines": 1,
  "bad_lines": [{"line": 6, "error": "不是合法 JSON：…", "excerpt": "{ 这一行坏了"}],
  "bad_lines_total": 1, "bad_lines_truncated": false,
  "message": "196 行里有 1 行无法解析（第 6 行：…）—— 已跳过这 1 行，服务仍可用，但**少了 1 条公告**。要修的是数据源，不是接口"
},
"skipped_lines": 1
```

- `skipped_lines` **保留**（老调用方读的是这个裸数，删掉是破坏兼容）；
  `load_state` / `load` 是**新增**的机器可判断版本。
- `bad_lines` 最多列 `MAX_BAD_LINES_REPORTED`（20）条，`bad_lines_total` 是准的。
- **文件不存在**仍然 fail loud（`FileNotFoundError`）：配错路径和"某一行坏了"
  是两回事，前者做什么都是错的。
- 门禁 `加载自检_坏行必须让门禁变红而且说清第几行` 两头都盯：
  真数据必须零坏行（坏了就红），造一个坏文件必须被认出来（认不出也红）。

### 15.2 排序里只有 `relevance`，载荷里没有它

见 §1 的「默认排序」。要点重复一遍，因为它被违反的后果最重：

- 排序键 = `(relevance desc, publish_date desc)`，**档内仍按日期降序**；
- **`relevance` 不做过滤**，也**不进列表载荷**（`LIST_FIELDS` 白名单结构性挡掉，
  `?meta=1` 也不给）。

> ⚠️ 2026-10-17 的一个教训（E 记）：D 报"默认排序还没接 relevance"，
> **我核了一遍，代码里早就接了**（`_sort_key_publish_desc`）。
> 他看到的「学院风采合集」「研究生招生直播宣讲会」排在前面，真正的原因是
> **数据层把它们判成了 `relevance=2`**（标题里有"招生"），不是排序没接。
> 实测 89 条 `relevance=2` 里有 8 条是宣传册/宣讲会/简介/招募类。
> **拿到的"这里有问题"先自己定位一遍再动手** —— 照错的指示改，会改到不相干的函数
> 然后以为修好了。数据层怎么修是 D 的事，写进 `journal/`，不在这里定。

### 15.3 `total == 0`：**"没数据"和"筛得太窄"必须分得开**

```json
"data_state": { "loaded": 196, "matched": 0, "skipped_lines": 0,
                "source_empty": false, "empty_reason": "no_match",
                "note": "数据源有 196 条公告，但没有一条符合当前筛选条件 —— 放宽筛选试试" }
```

| `empty_reason` | 何时 | 界面该说什么（**给 C/F**） |
|---|---|---|
| `null` | 有结果 | 正常列表 |
| `no_match` | 数据源有记录，筛选后 0 条 | 「没有符合条件的公告」+ 放宽筛选 |
| `source_empty` | 数据源本身就是 0 条 | 「**数据还没加载**」—— 这不是"没有"，也不是用户的错 |

- **只在 `?meta=1` 里给**（默认形状被 fixture 钉死，见 §1）。
  前端列表页已经在用 `?meta=1`（`web/assets/notices.js`），直接读 `data_state` 即可。
- `note` 里会带"另有 N 行数据损坏已被跳过"（有坏行时）—— 列表页是用户唯一必看的地方。
- `/api/health` 同时把 `load_state: "empty"` 摆出来：**0 条不许混在别的数字里**。
- **⛔ 不许把 `source_empty` 显示成"没有符合条件的公告"** —— 前者是系统的事，后者是用户的事。

### 15.4 状态文件坏了：备份 + 空状态 + **必须响**

`server/var/state.json` 里装着帖子、订阅、关注、**审计**。
`StateStore.load()` 现在认**三种坏**（原来只认第一种，另外两种是"服务起不来"和"每个请求都 500"）：

| 坏法 | 原来的表现 | 现在 |
|---|---|---|
| 半截 JSON | 静默改名 + 空状态，**`/api/health` 一个字不说** | 同上，但 `state.recovery` 说清 |
| 合法 JSON 但不是对象（`[]`） | `AttributeError: 'list' object has no attribute 'setdefault'` —— **服务直接起不来** | 按损坏处理 |
| 能解析但顶层键类型不对（`users: "x"`） | 服务起来了，**每个 `/api/me` 都 500** | 按损坏处理 |

**处理方式（三件事，一件都不能少）**：

1. 原文件**只改名、不改不删**：`<path>.broken-<YYYYmmddHHMMSS>`，内容逐字节保留；
2. 从**空状态**继续（**不拒绝启动**，理由见 §15.0）；
3. `/api/health` 里：

```json
"state": {
  "path": "…/state.json",
  "ok": false,
  "recovery": { "reason": "unreadable|not_an_object|bad_shape",
                "detail": "…",
                "backup": "…/state.json.broken-20261017142252",
                "message": "状态文件用不了（…），已备份到 …；**帖子、订阅、审计都没有恢复**，从现在开始是空状态。这不是‘新站’，是丢了一次状态" }
}
```

> **静默重建是不能接受的**：审计是法律意义上要留痕的东西，
> 而"从空状态重启"和"新装了一个站"在界面上长得一模一样。
> `ok: true` + `recovery: null` 是干净状态；**演示前瞄一眼这里**。

**并发写**（`state.py`）：`self._lock` 是 `RLock`，`save()` 和"改状态"的公开方法都在它里面，
需要跨语句改状态的地方用 `with state.transaction():`。
原来只锁了 `json.dump`，而**改状态发生在锁外** → 并发的 append 会插进别人的 dump 里
（`RuntimeError: dictionary changed size during iteration`），"读-改-写"还会丢更新
（额度多放行一条、同一个人的两次举报算成两个人）。
门禁 `state_并发写不丢更新也不写坏文件` 守四条不变量：被接受的写一条不少、
文件始终是合法 JSON、不留 `.tmp`、10 线程抢免费额度**正好 3 个 200**。

> **单 worker 这条不变**：多进程下这把锁不跨进程，要上多 worker 先把 `state.py` 换成 SQLite。

### 15.5 套磁信引文

见 §3 末尾的「引文质检」。一句话：**判不准就不给，丢了要说**。

### 15.6 限速器不跨重启 → 演示期间不重启

见 §13.4 末尾。这是一个**写明并接受**的缺口，不是待办。

### 15.7 这一节由哪些门禁盯着（`server/tests/run.py` 第 17 节）

| 门禁 | 盯的东西 |
|---|---|
| `加载自检_坏行必须让门禁变红而且说清第几行` | §15.1 三态 + 端到端 health + "仍可用" |
| `加载自检_正常数据不报假警` | 对照：正常数据 `load_state == "ok"` |
| `排序_relevance_高于日期_用造的数据验而不是赌磁盘` | §15.2 两级排序 + 只排不过滤 |
| `排序_relevance_不许混进列表载荷` | §15.2 结构性锁（白名单 + 默认 + meta=1） |
| `letter_判不准的引文一条都不许进信里_全量扫196条` | §15.5 四种坏形态全量扫 |
| `letter_坏正文的三种形态_都判不准就不给` | §15.5 造坏输入 + 正常公告不受影响 |
| `state_坏文件要备份要响_不许静默重建` | §15.4 备份逐字节一致 + 响 + 对照 |
| `state_合法JSON但形状不对也要拦` | §15.4 `[]` / `null` / 类型不对 |
| `state_并发写不丢更新也不写坏文件` | §15.4 四条不变量 |
| `空数据_必须和_筛选没命中_分得开` | §15.3 三态 `empty_reason` |
| `空数据_筛选接口也不许崩` | §15.3 0 条时每个列表接口 200 |

### 15.8 写这一节的门禁时的一条纪律

**坏输入一律造，不许赌磁盘上恰好是什么数据。** 这一晚"断言绑了磁盘数据"栽了五次，
解法是造临时 `notices.jsonl` / 临时 `state.json`，让它走**真的**加载器和**真的** app
（`create_app(<注入的 store>)`）—— 造数据走真代码，才叫端到端；
在测试里绕过加载器，验的就只是"我构造的 meta 能不能被检查"。

---

## 16. 推送：**事件 × 画像 → 你会收到的那条**（2026-10-17，B 写）

> 今天是**一条链**：用户填画像 → 数据层产出事件 → 后端匹配并生成推送 → 前端展示推送预览。
> 事件契约由 A 交（`contracts/events.schema.json`），这一节定义**消费它的三个接口**。
>
> **和 §11「关注与提醒」不是一回事**，别混：
>
> | | 数据源 | 回答的问题 | 用户要不要先设规则 |
> |---|---|---|---|
> | §11 `/api/digest` | `changes.json`（**这一轮**库里的变化） | 我关注的东西这轮变了吗 | 要（`/api/watch`） |
> | **§16 `/api/push/preview`** | `events.jsonl`（**历史**上发生过的事，append-only） | **有什么事值得现在打扰我** | 不用，填画像就够 |
>
> 两条都保留。digest 是"轮询式"的，推送是"打扰式"的 —— 后者才是月度订阅卖的东西。

### 16.0 这一节的第一条硬规矩

**`unknown` 不许写成「暂无」/「——」/省略不显示。**

推送是这条规矩最容易被违反的地方，因为推送文案天然短：
一句「该公告暂无截止日」字数刚好，看起来也很正常，而它对 `unknown` 就是**谎话**。

三句话必须分开（判据来自事件里必带的 `field_state.deadline`）：

| `field_state.deadline` | 推送里该写 | 带原文链接 |
|---|---|---|
| `found` | 日期本身 + 还剩几天 | 否 |
| `absent` | 「该公告未公布截止日」 | 否 |
| `unknown` | 「**未能读取截止日，请点原文核对**」 | **是** |

**`absent` 和 `unknown` 的区别就是这一节的存在理由**：前者用户该放心，后者用户该去看原文。
写成一样，用户会照着"没有截止日"做决定。

### 16.1 三个接口一览

```
GET    /api/profile?user=u_demo           读画像（没填过 → 不是 404，是"还没填"）
PUT    /api/profile                       写画像（整份替换，幂等）
DELETE /api/profile?user=u_demo           删画像（删完不再产生推送）

GET    /api/subscriptions?user=u_demo     订阅状态 + 渠道可用性
POST   /api/subscriptions                 开通（手动开通码，**不是支付**）
DELETE /api/subscriptions/{channel}?user= 关掉某个渠道（只支持 preview）

GET    /api/push/preview?user=u_demo      核心：你会收到的推送（只读、幂等）
POST   /api/push/preview                  "我接收这批" —— **只有这里会记录**去重与限频
```

**GET / POST 分开是这一节唯一一个非直觉的设计，理由见 §16.4。**

**为什么 POST 用 `PUT` 写画像**：整份替换、没有"部分更新"的语义歧义。
画像是一份小对象，读-改-写代价可以忽略，而 `PATCH` 的合并规则会成为新的契约负担。

### 16.2 画像 `/api/profile`

**最小字段就四个**（正好是 `POST /api/match` 已经在用的名字，别造第二套）：

| 字段 | 类型 | 例 | 来源 |
|---|---|---|---|
| `school` | string | `"浙江大学"` | 本科院校 |
| `majors` | string[] | `["物理学","力学"]` | 本科专业 |
| `preferred_schools` | string[] | `["中山大学","中国科学技术大学"]` | 意向学校 |
| `interest_keywords` | string[] | `["凝聚态物理","材料"]` | 意向方向 |

**额外字段原样保存**（`grade` / `english` / `research` …）：`/api/match` 已经吃这些字段，
用户以后填了不能因为我们不认就丢掉。但**推送只认上面四个**（见 §16.5 匹配规则）。

**认不出的字段要报出来**（2026-10-19，B19）——**保存 ≠ 认识**：

| 情形 | 行为 | 响应里的出口 |
|---|---|---|
| 近似字段（`intended_schools` / `major` / `target_level` …） | **自动映射**到真字段，原名不保存 | `mapped_fields: [{"from","to"}]` + `field_note` |
| 真字段和近似字段都填了 | **以真字段为准**，近似字段进 `dropped` | `dropped: [{"field","reason"}]` |
| 完全不认识的字段（`foo_bar`） | **原样保存**（以后可能用），但**不参与匹配** | `unknown_fields: ["foo_bar"]` + `field_note` |

`field_note` 是一句人话，前端可以直接显示，例如：

```
「intended_schools」不是我们的字段名，已按「preferred_schools（意向学校）」处理 ——
下次直接填「preferred_schools」就行；以下字段我们没识别：foo_bar。
它们会原样保存，但**不参与匹配**（匹配只看 preferred_schools / majors /
interest_keywords / preferred_units）
```

> **为什么必须报**：改之前 `intended_schools` 会被原样保存、然后**在匹配里彻底消失**，
> 接口返回 200，用户看不到任何提示 —— 他只会觉得"这系统不懂我"。
> QA 按 README 的固定画像跑，第一屏 **0 条**，根因就是两个字段名。
> **一个被保存但被忽略的字段，比一个被拒绝的字段更伤人**：拒绝至少会让他改。

```json
GET /api/profile?user=u_demo
{
  "user_id": "u_demo",
  "profile": { … 四个字段 + 用户填的其它字段 … },   // 没填过时是 null
  "complete": {                                  // **别让前端自己判"填够了没"**
    "filled": false,
    "has": ["school"],                           // 已经有的
    "missing": ["majors", "preferred_schools", "interest_keywords"],
    "can_push": false,
    "message": "还不能产生推送：缺 意向学校、意向方向。填了才会开始匹配。"
  },
  "updated_at": "2026-10-17T16:05:00",
  "_note": "画像只存在本机 server/var/state.json，不发送到任何第三方。演示用假登录态。"
}
```

- **没填过 → HTTP 200 + `profile: null`**，不是 404。404 的意思是"这个用户不存在"，
  而"新用户还没填画像"是**正常状态**，不是错误。
- `can_push` 由服务端算：**至少有一个"参与匹配"的字段非空**
  （`preferred_schools` / `majors` / `interest_keywords` / `preferred_units`）。
  **`school`（本科院校）只展示、不参与匹配** —— 本科院校不是"意向"，
  拿它去匹配会把"浙大学生"和"浙大招生"混成一件事。
  故意不要求"全填" —— 用户只填了意向学校，也该收到那几所的推送。
- `DELETE /api/profile` → `{"deleted": true, "user_id": …, "pushes_stopped": true}`。
  **删完 `/api/push/preview` 必须变成 `no_profile`**，不许还按旧画像推（门禁盯着）。

### 16.3 订阅与渠道 `/api/subscriptions`

**先说清楚这是什么**：`/api/subscribe`（§5）是"模拟支付开通"，
`/api/subscriptions` 是**同一个订阅关系的资源式视图 + 渠道开关**。两者读写同一份状态。

```json
GET /api/subscriptions?user=u_demo
{
  "user_id": "u_demo",
  "plan": {
    "active": false,
    "plan_id": null,
    "plan_name": null,
    "expires_at": null,
    "reason": "free",                 // free / active / expired / none
    "text": "免费版：可以浏览公告、看三态、点原文链接"
  },
  "channels": [
    {"channel": "preview", "available": true,  "enabled": true,
     "text": "站内预览：这条推送长什么样，页面直接渲染（不需要任何外部服务）"},
    {"channel": "email",   "available": false, "enabled": false,
     "text": "邮件通道还没接：没有发信服务，也不会问你要邮箱"},
    {"channel": "webhook", "available": false, "enabled": false,
     "text": "Webhook 还没接：留了适配位置，演示不碰"}
  ],
  "activation": {
    "method": "manual_code",
    "code_required": true,
    "_note": "**没接支付通道**。开通用的是手动开通码（演示用），不是付款。"
  },
  "_note": "D4 演示用，未接真实支付；开通码由课程助教/评委人工发放"
}
```

**渠道只做 `preview`。** `email` / `webhook` 的 `available` 恒为 `false` 并给原因 ——
这不是"没做完"，是**「我们不做做不到的事」的显式表达**。
一个写着"即将支持"的按钮，比一个写着"还没接，因为没有发信服务"的按钮更不诚实。

**开通（手动开通码，不是支付）**：

```
POST /api/subscriptions?user=u_demo     body: {"code": "TUIMIAN-DEMO-2026"}
→ 200 {"activated": true, "user_id": "u_demo", "plan": {...}, "method": "manual_code",
       "_note": "演示用手动开通码；本次没有发生任何支付"}
→ 400 {"error": {"code": "BAD_CODE", "message": "开通码不对或已过期"}}
```

- **`user` 在 query 里，和全项目其他端点一致**（issue 008 第 2 条修正）。
  上一版只从 body 读 `user_id`，于是 `?user=u_x` + body 只给码 → 开通到了 `u_demo`，
  而预览查 `u_x` 显示未订阅 —— **看起来像"开通了但没生效"**。
  这种"两个端点的取值位置不一致"本身就是坑，不靠文档提醒来兜。
- **仍然兼容 body 里的 `user_id`**（老调用方不该因为这次修正而坏），
  优先级是 **query > body > `u_demo`**，和别的端点行为一致。
- 码从 `config.PUSH_ACTIVATION_CODE` 读，**环境变量可改**（演示现场要临时发码）。
- **不接受"付款成功"这类参数**：没有支付，就不要留一个让人以为有的入口。
- 400 的 `message` 只说"码不对或已过期"，**不区分**是哪一种 —— 区分了就等于告诉别人码对不对。

**关渠道**：`DELETE /api/subscriptions/{channel}` → 只允许关 `preview`
（其余本来就不可用，关了没有意义；关不存在的渠道给 404，不静默成功）。

### 16.4 推送预览 `GET /api/push/preview` —— 这一节的核心

```
GET /api/push/preview?user=u_demo&limit=20&source=
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `user` | `u_demo` | 假登录态，沿用全项目口径（查询参数是 `user`，不是 `user_id`） |
| `limit` | 20 | 1..50，越界**截断并说明**，不返回 400（见下） |
| `source` | 空（真实） | 只接受 `sample`：读演示样例事件并**在响应里标注**。见 §16.9 |

响应（**默认形状 = `fixtures/push.preview.json`，一个多余键都不加**）：

```json
{
  "user_id": "u_demo_sub",
  "generated_at": "2026-10-17T16:10:00",
  "plan": {"active": true, "plan_id": "monthly", "plan_name": "月度订阅",
           "expires_at": "…", "reason": "active",
           "text": "月度订阅：推送 + 匹配排序 + 套磁信"},
  "profile": {"can_push": true, "filled": true, "missing": [],
              "text": "画像已填写，按 意向学校 / 本科专业 / 意向方向 匹配。"},
  "source": {
    "kind": "events",              // events（真实）/ sample（演示样例）/ none（文件不存在）
    "path": "data/out/events.jsonl",
    "total": 286,
    "data_time": "2026-10-17T16:46:45",   // 这批事件代表"什么时候的事实"
    "data_age_hours": 1.65,
    "data_age_text": "1.6 小时前",        // 现成中文，别自己算（§12 那条规矩）
    "stale": false,                       // 超过 stale_after_hours → true
    "stale_after_hours": 30.0,            // **和 §12 同一个数，不另定一套**
    "note": "读到 286 条事件，数据是 1.6 小时前。"
  },
  "empty_reason": null,          // 非 null 时 items 一定是 []
  "empty_text": null,            // 给前端直接显示的一句话（五态五句，见下）
  "items": [ … 见 §16.5 … ],
  "reference": {                 // 「参考资料」区（§18.9，B22）
    "total": 34,                 // 去重后的总数（不受 limit 截断影响）
    "items": [ … 最多 PUSH_REFERENCE_LIMIT 条 … ],
    "truncated": true,
    "_note": "「参考资料」：招生目录 / 简章一类，随时可查，和首屏的『今天该办的事』分开。"
  },
  "suppressed": [ … 见 §16.7 … ],
  "tally": {
    "events": 476, "matched": 382, "unmatched": 94, "backfill": 231,
    "sent": 20, "expired": 0, "expired_matched": 22, "suppressed": 231,
    "limit": 50, "daily_limit": 20, "used_today": 0, "free_limit": 3,
    "truncated": true, "truncated_reason": "limit", "committed": false,
    "catalog": 34, "emba_excluded": 4, "kind_unknown": 0, "out_of_window": 8,
    "apply_fresh_announcement": 37,          // 全库近 60 天招生通知（去重后）
    "apply_fresh_announcement_matched": 6    // 其中命中这个画像的条数
  },
  "_note": "推送只做**站内预览**，未接邮件/微信通道；每条都带原文链接；没有支付通道，订阅用手动开通码。"
}
```

**`tally` 里「池子有多大」的两个数**（B23，2026-10-19）：

| 字段 | 是什么 | 前端怎么说 |
|---|---|---|
| `apply_fresh_announcement` | 全库 **近 60 天 + `announcement` + 非 junk + 同校同标题去重** 的公告条数 | 「近 60 天全库 **37** 条招生通知」 |
| `apply_fresh_announcement_matched` | 同一池子里**命中这个画像**的条数（学校/专业/方向/院系任一，招生方式也过滤） | 「其中 **6** 条对口你」 |

**两个数都从公告库现算，不从事件流来** —— 分母是整库（2561），
不是这个用户的推送条数。它们和 `items` 的条数**可以不等**（首屏还受
事件流、免费层和 `?limit=` 影响），两个数都如实给，不互相凑。

`tally` 里两个恒等式**必须自洽**（门禁盯着）：

```
events  == matched + unmatched        （每个事件要么命中画像、要么没命中）
matched == backfill + sent + suppressed + 被截断的
```

`backfill` 是"命中画像、但因为是补抓的旧公告而没推"的条数。
**它是这个数存在的唯一理由**：用户看到"今天 2 条"，而实际匹配到 46 条 ——
剩下 44 条哪去了？不解释的话一定被理解成漏推。

**`truncated_reason`** 说清"被谁截的"：`free`（免费层 3 条）/ `limit`（`?limit=` 截的）/ `null`。

**`empty_text` 是给前端直接显示的一句话** —— 四态四句已经拼好，别让前端按
`empty_reason` 再写一遍（三个页面会写出三句，而且最容易漏掉 `no_events_file` 那一支）。

**为什么 GET 不记录、POST 才记录（这一节唯一非直觉的设计）**：

预览是"看一眼"。如果看一眼就消耗掉一条推送，用户刷新一次页面就会看到
"你已经收到过了" —— **那是把一个展示行为当成了消费行为**。
所以：

- `GET` **幂等**：刷新十次，看到的十条一模一样，`tally.committed=false`；
- `POST` 是"我接收这批"：记录 `push_id`（去重）+ 当天计数（限频），`committed=true`；
- 之后再 `GET`，那些事件会出现在 `suppressed` 里，`reason=duplicate`。

演示时这个区别正好能讲一分钟：**"预览不消费、接收才消费"**，
它是"推送不是打扰，是你自己收下的"这句话的实现。

**`empty_reason` 是六态，不是"空就是空"**（沿用 §11/§12 的口径）：

| 值 | 意思 | 前端该说什么 |
|---|---|---|
| `null` | 有内容 | 正常渲染 |
| `no_profile` | **用户还没填画像** | 「先填画像，我们才知道推什么给你」+ 入口 |
| `no_events_file` | 事件文件**还不存在**（`make_events` 还没写过） | 「今天没有新公告 —— 数据管道没产生新事件」。**不许说"没有变化"**，那是在断言一件我们没观测到的事 |
| `stale_events` | 事件文件在，但**数据超过 30 小时没更新**，且什么都没匹配到 | 「抓取管道几天没跑了，所以**不知道**今天有没有」—— **不是「没有」** |
| `no_match` | 有事件、数据也新鲜，但**一条都没命中画像** | 「今天没有跟你相关的新变化」+ 展示画像摘要，让用户能判断是不是画像填窄了 |
| `only_backfill` | 命中画像的**全是补抓的旧公告** | 「今天匹配到的都是我们补抓时翻出来的历史公告，不是新发的」。**说成 `no_match` 会让用户去改画像，而画像没问题** |

### ⚠️ `empty_reason` **只给一个**，另有一个 `empty_reasons` 详情数组（2026-10-18 补）

原来是一个 `if/elif` 链：只给一个原因，但那个原因是"代码里最后命中的那条"，
**没有把优先级说出来**，也没有出口告诉调用方"其实另外还有原因也成立"。

现在：

| 字段 | 形状 | 给谁 |
|---|---|---|
| `empty_reason` | **单个字符串**（或 `null`） | 前端 —— 只渲染一句话，不给它三个原因让它自己选 |
| `empty_reasons` | **数组**，所有成立的原因，按优先级排序 | 排查 / 值班 —— "还有哪些原因同时也成立" |

**优先级 = 用户能解决的优先**：

```
no_profile（他能填）> no_events_file（我们能修）> stale_events（我们能修）
> no_match（他能放宽画像）> only_backfill
```

理由：一个用户看到"没有推送"，必须能立刻分清**该他动手还是该等我们**。
实测场景（没填画像 + 数据也过期）：`empty_reason = "no_profile"`，
`empty_reasons = ["no_profile", "stale_events"]` —— 主因指向他能做的事，
但排查时能看见"数据也旧了"。

**`stale_events` 和 `no_match` 互斥**：数据过期时我们不知道有没有匹配，
就不能断言"没匹配"。

### ⚠️ `source.stale`：这个字段存在的唯一理由

推送的全部价值建立在"**今天**发生了什么"上。而"今天没有新公告"有两种成因：

| 成因 | 该说什么 | 现在怎么表达 |
|---|---|---|
| 管道跑了，今天真的没有 | 「今天没有新公告」 | `stale=false` + `empty_reason=no_match` |
| **管道几天没跑** | 「我们**没去看**，所以"没有"这个词现在说不了」 | **`stale=true`** + `empty_reason=stale_events` |

**数据旧了还照常说"今天"，就是把"我们没看"说成"没有"** ——
本项目反复在防的那类错误，只是这次藏在**时间**里。

- `data_age_hours` 怎么算：**最晚事件的 `at`** → 文件 mtime → `null`。
  为什么 `at` 优先：mtime 在"补数据/重算"时会变新，那时它会说"数据是刚刚的"，
  可事实是我们刚刚重新生成了一份**旧**数据 —— 补抓事件恰恰是这种形态。
- `stale` 只在 `kind in ("events", "none")` 时有意义；`sample` 时是 `null`
  （演示样例的"年龄"没有意义，它不是数据，是道具）。
- **数据旧但有内容时，`items` 照给**（不隐藏，和 `relevance` 只排不滤同一条原则），
  只是 `source.stale=true` 让前端在顶部警告。

**`limit` 越界不返回 400**，而是截断 + 在 `tally.truncated` 里说明：
推送列表是"给你看的东西"，为它返回一个错误、让页面白掉，是拿用户体验去换契约洁癖。
（这和 `?size=10000` 不同：那是**列表查询**，越界说明调用方在滥用，该 400。）

### 16.5 一个推送项的形状

```json
{
  "push_id": "push_9c1f2ab34de5f607",
  "event_id": "adde_a3d1e58a9ffaed9c",
  "type": "added",
  "notice_id": "ufa82ed2b-d989cbe1679cbd09",
  "title": "关于中国教育国际交流协会邀请我校师生参加2026博士生国际招生面试交流会的通知",
  "school": "大连理工大学",
  "unit": "研究生院",
  "url": "http://gs.dlut.edu.cn/info/1089/52022.htm",
  "at": "2026-10-17T15:23:08",
  "detected_on": "2026-10-17",
  "publish_date": "2026-10-16",
  "relevance": 2,
  "notice_kind": "announcement",   // announcement / catalog / emba / other（老数据 null）
  "is_backfill": false,            // 补抓到的（60 天窗口内的 announcement 仍会进首屏）
  "matched_because": ["意向学校：大连理工大学", "意向方向：教育"],
  "score_dims": {
    "school_hit": 1.0,
    "major_hit": 0.0,
    "direction_hit": 1.0,
    "freshness": 1.0
  },
  "misfit": {
    "weight": 1.0,
    "keys": [],
    "text": null
  },
  "why": [
    "意向学校命中：大连理工大学",
    "方向命中：教育",
    "1 天前发布（30 天内）",
    "公告未公布截止日"
  ],
  "deadline": {
    "state": "absent",
    "value": null,
    "text": "该公告未公布截止日",
    "url": "http://gs.dlut.edu.cn/info/1089/52022.htm",
    "must_check_original": false,
    "days_left": null
  },
  "reminder": null,
  "change": null,
  "channel": "preview",
  "headline": "【新公告】大连理工大学：关于中国教育国际交流协会…",
  "body": "研究生院 · 2026-10-16 发布 · 该公告未公布截止日"
}
```

逐字段里**必须解释的六条**：

1. **`url` 必带、绝不省略** —— 每条推送都能点回原文，**免费层也一样**。
   这是"可信"的底线：推送说得再漂亮，不能回链就是不可核实。
2. **`publish_date` 是公告的发布日，`detected_on` 是我们检测到的日子** —— 两件事。
   加 `publish_date` 是为了让"这条是不是旧公告"能**机器判断**
   （issue 008 的验收标准是"items 里没有早于 30 天前的"）；
   只靠 `body` 里那句"2026-10-13 发布"是字符串，验不了。
3. **`deadline` 复用 §14 的 `deadline_display` 形状**（`state`/`text`/`value`/`url`/`must_check_original`），
   只多一个 `days_left`。**前端只写一套渲染逻辑**，公告卡片和推送卡片共用。
4. **`matched_because` 是人话**，由事件里的 `matched[]` 和画像求交得出来：
   `"意向学校：大连理工大学"`。**不许只给一个 `score: 0.83`** ——
   用户没法核对一个分数，而能核对一句话（"我确实关注大连理工"）。
5. **`headline` / `body` 是给"预览"用的两行字**，服务端拼好。
   **不许把它们当模板让前端自己拼** —— 三个页面会拼出三种写法，而且最容易漏掉 `unknown` 那支。
6. **`push_id` 是稳定去重键**：`sha1(user_id|event_id)` 前 16 位。
   同一用户同一事件永远同一 `push_id`，前端可以拿它做本地去重（刷新页面不会看到"新"角标两次）。
7. **`misfit` 是负向维度**（2026-10-19，B20；详见 §18.8）：`weight` 是排序乘数
   （1.0 = 不降权），`keys` 说命中哪几维（`degree_type` / `transactional`），
   `text` 是一句人话并会**同时出现在 `why` 里**。
   **每条都有这三个键**（没命中时 `1.0 / [] / null`），前端不用先判断字段在不在。
   它**不是过滤器**：命中只降权，条目照样在列表里。
8. **`notice_kind` 是四档**（2026-10-19，A29 + B22；详见 §18.9）：
   `announcement`（今天该办的招生通知）/ `catalog`（目录·简章，去参考资料区）/
   `emba`（在职项目）/ `other`（其余）。
   **老数据是 `null`**（A29 之前的产出）—— 那时 `tier` 退回旧规则，
   条数记进 `tally.kind_unknown`。前端拿它当标签用，**不要拿它重排**。
9. **`is_backfill` 是"补抓到"**：60 天窗口内的 announcement 即使是补抓的也进首屏
   （9 月发布、10 月底截止的推免办法今天仍然有效）。前端可以据此说"补录"，
   而不是硬说"今天新发"。

**匹配规则**（写死，别让它各处漂）：四个字段里**任意一个**命中就算匹配，命中即推：

| 画像字段 | 和事件里什么比 | 规则 |
|---|---|---|
| `preferred_schools` | `event.school` | 任一项**子串**命中（去空白，中文按字面） |
| `majors` | `event.title` / `event.matched[]` | 同上 |
| `interest_keywords` | `event.title` / `event.matched[]` | 同上 |
| `preferred_units` | `event.unit` | 同上（可选字段，`/api/match` 也在用） |

**`relevance` 不再用来排序**（2026-10-18 起），但它**仍然是准入判定**：
`relevance == 0` 表示"数据层判定这条根本不是招生信息"（选课 FAQ、讲座通知），
这类记录**四维分再高也不许冒头**。所以最终排序键里仍乘着它 ——
只是它从"排序主键"降级成"开关"（0 或 1），细粒度改由 §18 的四维负责。

拿不准时宁可多推一条并在 `why` / `matched_because` 里如实说明依据 ——
过滤错了用户会错过报名，排序错了只是某条低几行。

> **⚠️ 给前端：`items` 的顺序就是"该看的顺序"，别再自己按截止日重排。**
> 后端的排序已经含了对口程度和紧急度（见下方公式与 §18）。
> 前端要显示"还剩几天"就用 `deadline.days_left`，要显示"有多急"就用 `urgency_weight`；
> **但不要拿它们重新 sort** —— 那会把"对口但不急"的挤到"极对口且急"的前面，
> 而后者才是用户最该先看的。
>
> 也不要在前端自己算一个"总分"显示给用户：
> 那是内部排序公式，我在后端调权重（`config.PUSH_SCORE_WEIGHTS`）时，
> 屏幕上那个数就会莫名其妙地变。**能显示的是四维本身和 `why`**，不是它们的加权和。

**排序**（2026-10-18 四维加权；10-19 加负向维度 §18.8、加 `tier` 前置 §18.9）：

```
① tier：一级（announcement）整层在前，二级（other）只补位
② 同一层内：未截止的在前，已截止的沉底
③           (Σ wᵢ × dimᵢ) × urgency_weight × admit(relevance) × misfit.weight 降序
      其中 dimᵢ ∈ {school_hit, major_hit, direction_hit, freshness}，各自 0~1
      wᵢ 来自 config.PUSH_SCORE_WEIGHTS（默认 0.35 / 0.25 / 0.20 / 0.20）
      admit(relevance) = 0.0 当 relevance == 0，否则 1.0
      misfit.weight 来自 config.PUSH_MISFIT_WEIGHTS（degree_type 0.35 / transactional 0.35）
④           → 发布日降序（新的在前；没发布日的沉底）
⑤           → at 降序 → event_id（让顺序确定，别依赖字典序的偶然）
```

**`tier` 为什么提到"已截止"前面**（2026-10-19，B22）：原来第一层是"未截止/已截止"，
于是一条**已截止的一级公告**会沉到二级（其他相关）后面，屏幕上出现"其他相关"
夹在招生通知中间。现在已截止**只在它自己那一层里**沉底，一条都不删。

### `urgency_weight`：截止日的紧急度权重（`1.0 ~ 2.0`）

| 距截止 | 权重 | 意思 |
|---|---|---|
| 已截止（`days_left < 0`） | **0.0** | 排序上垫底（**但不压掉**，见下） |
| 今天截止（`0`） | **2.0** | 最紧急 |
| `1~7` 天 | 2.0 | T-7 高优 |
| `8~14` 天 | 1.5 | T-14 中优 |
| `15~30` 天 | 1.2 | T-30 略优 |
| `> 30` 天 | 1.0 | 正常 |
| `absent` / `unknown` | **1.0** | **不惩罚** |

**为什么是乘不是加**：`Σ wᵢ×dimᵢ` 是 0~1 的**对口程度**，
`urgency_weight` 是 1.0~2.0 的**紧急程度**，**两个不同维度**。
乘法表示"越对口、越紧急的越靠前"；加法会让"极不对口但极紧急"排到
"很对口但还早"的前面 —— 那不是用户要的。

实测（默认权重，占位示意）：

```
1.00 × 2.0 = 2.00   学校+专业+方向都对口，7 天内截止
0.80 × 2.0 = 1.60   少命中一维，同样急
1.00 × 1.0 = 1.00   完全对口但还早
0.35 × 2.0 = 0.70   只命中学校却马上截止   ← 不会因为紧急插队
0.00 × 2.0 = 0.00   relevance=0（准入不通过）← 永远垫底
```

乘法有个好性质：**低对口的东西不会因为紧急而插队**。

**`absent` / `unknown` 给 1.0，不给 0**：`unknown` 是"我们没读到截止日"，
**它可能下周就截止**。给 0 等于把"我们不知道"当成"不用急"——
那正是这套三态要消灭的混淆。

### ⚠️ `urgency_weight = 0` **只参与排序，不参与过滤**

派单原话是"过滤掉已过期的（urgency=0）"。**没照做**，理由见下面那三条 ——
已截止的是**沉底不压掉**，这个决定是同一天早些时候定的，不能悄悄改一处。

要真的不返回，那是产品决定，得把 `is_expired` 的沉底逻辑和本节一起改回来。

**"已截止"只指 `found` 且 `days_left < 0`。** `unknown` / `absent` **一律不算**：
`absent` 是"公告没公布截止日"、`unknown` 是"我们没读到"，
把它们归进"已截止"是**编造一个我们没有的事实**（和 §14 那条不变量同一类）。
`days_left == 0`（今天截止）**也不沉底** —— 那是最紧急的一条。

**为什么是"沉底"而不是"压掉"**（这一条和 `relevance` 只排不滤是同一个道理）：

1. **截止日可能抽错。** 这个项目有过"把正文里的日期当成发布日"的实例，
   也有噪声闸误杀正确截止日。压掉一条抽错的，用户**永远不知道该去报名**。
2. **学校会延期。** "截止日从 10-15 延长到 10-30" 恰恰是最有价值的推送 ——
   而如果 10-15 那天把它压掉了，这条推送就不会出现。
3. **补发的附件截止后照样有人要**（模板、表格）。

**排序错了只是某条低几行，压错了是永久看不到。**

`tally.expired` 给"末尾有几条已截止"，前端据此可以加一句「最后 N 条已截止」。

### ⚠️ `expired` 和 `expired_matched` 是两个数，**前端要同时用**

| 字段 | 是什么 | 实测 |
|---|---|---|
| `expired` | **items 里**已截止的条数（在列表末尾） | 放开上限时 22 |
| `expired_matched` | **匹配到的**已截止总数（含被 `limit`/每日上限截掉的） | **正常上限时仍是 22** |

**为什么必须两个都有**（C 报的，这是个真 bug）：

已截止的**沉底**，而 `limit` / 每日上限会截断 —— 真实数据上匹配 382 条、只给 20 条，
**已截止的根本进不了那 20 条**，于是 `tally.expired` **恒为 0**。
C 按它做的"最后 N 条已截止"永远不触发，成了死代码。

实测：

```
limit=999 → expired=22  expired_matched=22     （能看到）
limit=20  → expired=0   expired_matched=22     （看不到，但数还在）
```

**所以前端该这样说**：`expired > 0` 时「最后 N 条已截止」；
`expired == 0 且 expired_matched > 0` 时「**另有 N 条已截止，未显示**」。

这两种沉默含义完全不同 —— 不说清，用户会以为"今天没有已截止的"。

### 16.6 截止提醒 T-7 / T-3 / T-1：**窗口，不是精确某一天**

**只对 `field_state.deadline == "found"` 的推提醒。** `absent` 没有提醒（本来就没有截止日），
`unknown` 没有提醒，但它的 `deadline.text` 已经说了"请点原文核对"。

```
days_left = deadline - 今天
  days_left <= 1  → T-1（含今天截止、已过 1 天）
  days_left <= 3  → T-3
  days_left <= 7  → T-7
  days_left > 7   → 不提醒
  days_left < 0   → 不提醒，但 deadline.text 里如实说"已过期 N 天"
```

**为什么是窗口而不是"第 7 天准时发一条"**：
推送的触发点是"用户打开看"，不是定时器。用户可能第 8 天才打开，
精确制会让他在 4 天截止时**什么提醒都收不到** —— 而那正是最该提醒的时候。
取**最紧的一档**（4 天 → T-7，2 天 → T-3），一个推送只挂一个提醒。

```json
"reminder": {"kind": "T-7", "window_days": 7, "days_left": 4,
             "text": "还剩 4 天（2026-10-21 截止），已进入 7 天提醒窗口"}
```

- **免费层不给 `reminder`**（给 `null` 并说明"提醒是订阅功能"，见 §16.8）——
  但 `deadline` 三态和 `url` **照给**。免费用户看得到"什么时候截止"，
  付的是"到点有人告诉你"。
- `T-1` 的文案是「**明天截止**」/「**今天截止**」，不要写"还剩 1 天"——
  后者读起来像还有富余。

### 16.7 去重与限频：**压掉了必须说出来**

`suppressed[]` 里放"本来能推、但这次没推"的条目，每条带 `reason`：

```json
{"event_id": "…", "notice_id": "…", "title": "…", "reason": "duplicate",
 "text": "这条你今天已经收到过了（同一事件不重复推送）"}
```

| `reason` | 意思 | 会不会再出现 |
|---|---|---|
| `backfill` | **补抓的旧公告**（`event.is_backfill == true`）—— 我们这次多抓到了，不是学校今天新发了 | 不会（它永远不是新闻） |
| `catalog` | 目录 / 简章一类**参考资料** —— 已挪到 `reference` 区，不进首屏（B22） | 在「参考资料」区能看到 |
| `emba` | EMBA / MBA / 在职 / 非全日制 —— 对**学术型/直博**画像硬排除（B22） | 明确要专硕/管理类的画像不会走到这里 |
| `out_of_window` | 发布超过 **60 天**窗口 —— 不进「本周值得看」，但仍在公告库里（B22） | 不会（除非学校重新发布） |
| `duplicate` | 同一 `event_id` 今天推过（`push_id` 已记录） | 不会（这就是去重的意义） |
| `merged` | 同一批里同一条公告的另一个事件（见下） | 不会（同一批内合并） |
| `rate_limited` | 今天推送数已到上限 | 明天会 |

**没有 `expired`。** 已截止的公告**不压掉，只沉底**（见 §16.5 的排序规则）——
压掉等于"我们替你决定这条没用"，而截止日抽错、学校延期、补发附件都真实存在过。
这是这一节唯一一处**故意不用 `suppressed` 表达"这条价值低"**的地方。

**`backfill` 是这一节最重要的一个 reason。** 实测真实数据：90 条事件里 **85 条**是补抓的，
付费身份匹配 46 条、其中 44 条是补抓 —— 不排除的话，用户会收到
「【新公告】2013 年…」，而"可信"正是这个项目的主线定位。

**判据只能是事件自己的 `is_backfill` 字段，不能是 `type`，也不能是"发布日早于 30 天"**：

- 数据层对 `deadline_changed` / `attachment_added` **永远不标 `is_backfill`** ——
  **再老的公告改了截止日也该报**。按 `type == "added"` 一刀切会把它们误杀。
- "早于 30 天"是**数据层判定 `is_backfill` 用的口径**，接口层不要自己再判一遍。
  两处各判一次，迟早一处说真一处说假，而那时没人知道该信哪个。

**`merged`：同一批里按 `notice_id` 只留一条。**

同一条公告会先后产生 `added` / `deadline_changed` / `attachment_added` 三种事件。
**跨批次**它们是三件事（时钟往前走，每次都带来新信息）；
但**同一批里**两条并列出现在界面上，看起来就是"重复推送"——
A 验收时实测到 `['ue00ab131-2bc4', 'ud1ddf924-1515', 'ud1ddf924-1515']`。

留**信息量大的**那条：`deadline_changed` > `attachment_added` > `added`
（前者带"从 X 改成 Y"，后者只说"有新公告"）。
被合并掉的**进 `suppressed`（`merged`）**，因为直接丢掉的话
`tally.matched` 和 `sent` 会对不上，用户会以为漏推了。

**跟画像没交集的事件不进 `suppressed`**，只进 `tally.unmatched` 计数。
理由：196 条事件里可能 190 条跟你无关，逐条列出来会让 `suppressed` 变成
一份噪音清单，而用户要的是"有没有漏" —— 一个数就够了。

**为什么不干脆不返回 `suppressed`**：用户看到"今天 3 条"，而实际匹配到 4 条，
剩下那条去哪了？**不解释的沉默会被理解成漏推**，而"漏推了"正是订阅产品
最致命的信任问题。`tally.suppressed` 给数、`suppressed[]` 给条目，用户能自己核对。

### ⚠️ 但 `suppressed` **只给样例**（2026-10-18 性能修复，契约 §22）

2751 条数据下这里曾经返回**全量**：2739 条、**523 KB**，占整个响应的 **98%**，
首屏 0.5~3 秒。**那是拿"可解释性"付了一笔没人用的钱** —— 没有人会去读 2739 条。

现在四个字段一起给：

| 字段 | 形状 | 用途 |
|---|---|---|
| `suppressed` | **数组，每种原因前 3 条**（`PUSH_SUPPRESSED_SAMPLE_PER_REASON`） | 保持原形状，前端不用改；"给我看看是哪几条" |
| `suppressed_total` | 整数 | "另有 **1558** 条被压" |
| `suppressed_tally` | `{reason: count}` | "其中 **1331** 条是补抓、**168** 条是脏记录" |
| `suppressed_sample` | `{reason: [前 3 条]}` | 按原因分组看样例 |
| `suppressed_truncated` | 布尔 | 说明 `suppressed` 不是全量 |

**`tally.suppressed` 仍然是真实总数**（和 `suppressed_total` 相等）——
只给样例不会让任何计数变小。**"抑制了多少条"必须还能说清。**

要翻全部：`GET /api/push/suppressed?user=&reason=&offset=&limit=`（分页，见 §16.9）。

### 16.9 `GET /api/push/suppressed` —— 被压掉的全量（分页）

```
GET /api/push/suppressed?user=u_num_demo&reason=backfill&offset=0&limit=50
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `user` | `u_demo` | 假登录态（画像来自它） |
| `reason` | 全部 | `backfill` / `junk` / `merged` / `duplicate` / `rate_limited` |
| `offset` / `limit` | `0` / `50`（上限 200） | 分页 |

响应：`{user_id, reason, offset, limit, total, items[], suppressed_tally, suppressed_total, _note}`。
`items` 的形状和 `preview` 里 `suppressed[]` 的条目**完全一样**（`event_id` / `notice_id` /
`title` / `reason` / `text`），所以前端一套渲染能用两处。

**`suppressed_tally` 是全局计数**（不受 `reason` 过滤影响），和 `preview` 里的完全一致。

### 16.8 免费 / 付费：**一个开关，不是两套代码**


**限频**：`config.PUSH_DAILY_LIMIT`（默认 20，环境变量 `TUIMIAN_PUSH_DAILY_LIMIT` 可调）。
按 `(user_id, 自然日)` 计数，落在 `state` 里。

### 16.8 免费 / 付费：**一个开关，不是两套代码**

| | 免费 | 订阅 |
|---|---|---|
| 看推送预览 | ✅ 前 **3 条**（`tally.truncated: true` + 一句说明） | ✅ 全部（受 `limit` 上限） |
| 截止日三态 | ✅ 全部 | ✅ 全部 |
| 原文回链 | ✅ **每条都有** | ✅ |
| 截止提醒（T-7/T-3/T-1） | ❌ `reminder: null` | ✅ |
| 匹配排序（`matched_because`） | ✅（能看，才知道值不值得买） | ✅ |
| 套磁信 | ❌ 402（§5） | ✅ |

**免费层故意给"前 3 条"而不是 0 条**：推送的价值必须**看得见**才卖得掉。
一个只有付费才能看到的推送，用户在购买前无法判断它值不值 —— 这不是付费墙，是赌博。

`plan.active == false` 时 `tally.truncated: true` 且多一句：
`"免费版显示前 3 条；订阅后显示全部并开启截止提醒"`。**不返回 402**：
预览不是敏感内容，把页面打成错误只会让人以为接口坏了。

### 16.9 数据源：真实是空的，演示用显式开关

| `?source=` | 读什么 | `source.kind` | 用在哪 |
|---|---|---|---|
| 不传 | `data/out/events.jsonl` | `events` | 正常路径 |
| 不传且文件不存在 | —— | `none` | `empty_reason: "no_events_file"` |
| `sample` | `contracts/fixtures/events.sample.json` | `sample` | **只在演示时** |

**`?source=sample` 是显式开关，不是自动回退。** 这和前端 `?api=fixtures` 是同一个思路：
回退可以，**但必须让人看得见**。响应里的 `source.kind` 会告诉前端"你现在看的是演示样例"，
页面上必须标出来（见 §16.10）。

**今天真实事件流是空的**（`changes.json` 是 `added=0 / changed=0 / reparsed=5`）——
这不是坏了，是今天真的没有新公告。所以：
- `data/out/events.jsonl` 还不存在 → 真实路径给 `no_events_file`。
- 演示这 2–3 分钟**用 `?source=sample`**，并在 `RUN.md` 里写清这是演示样例。

### 16.10 给 C 的交接单（照这个画，不用猜）

1. **`headline` / `body` 直接用，别自己拼文案。** 三态的三句话由服务端给全，
   前端拼一次就可能漏掉 `unknown` 那一支。
2. **`deadline` 复用公告卡片的渲染逻辑**（形状和 `deadline_display` 一致）。
   `unknown` 必须显示原文链接，且**不许**写成"暂无"/"——"/省略。
3. **`source.kind == "sample"` 时页面顶部必须有一条可见的标记**：
   「演示样例事件（不是今天真实抓到的）」。没有这个标记就是**用一个假输入冒充真结果**。
4. **`empty_reason` 四态各写一句话**（§16.4 那张表），别统一写"暂无推送"。
5. **`suppressed` 要能用一句话说出来**：「另有 1 条你今天已收到，未重复推送」。
   不给用户看也行，但**`tally.suppressed > 0` 时至少给个数**。
6. **免费/订阅两个身份都要能走完这条链**（演示用 `?user=u_demo_sub` 看完整版）。

### 16.11 这一节由哪些门禁盯着（`server/tests/run.py` 第 18 节，15 条）

四条**核心**（用户点名要的）+ 八条配套：

| 门禁 | 盯的东西 |
|---|---|
| `push_unknown_不许被当成_absent` | 三态三句话在**推送项里**也分开；`unknown` 带链接；不许「暂无」「——」；`absent`/`unknown` 的 `days_left` 必须是 None |
| `push_重复事件不重复推送` | GET 幂等、POST 才记录；第二次全进 `duplicate` 且能对上号；`push_id` 格式稳定 |
| `push_画像删除后不再推送` | 删前后对照；删完 `no_profile`；删完不再写推送记录；重复删返回 False |
| `push_每条推送必含原文链接` | 全量扫（造的事件 + 样例源 + 免费层），不抽样 |
| **`push_补抓的旧公告不进推送`** | **issue 008 核心**：`is_backfill=true` 不进 items、进 `suppressed(backfill)`；**没有 `is_backfill` 的 `deadline_changed` 必须照推**（防一刀切）；全是补抓时 `empty_reason=only_backfill` |
| **`push_同一条公告在同一批里只推一条`** | 同批按 `notice_id` 合并；留 `deadline_changed` 而不是 `added`；被合并的进 `merged`；不同公告不误合 |
| **`push_真实事件流里没有补抓的公告`** | **真实 `events.jsonl` 上回归**：items 里没有早于 30 天的；`tally.backfill>0` 时 suppressed 里必须有 `backfill` |
| `push_预览形状与_fixture_一致` | 递归键路径 == `fixtures/push.preview.json`（加键、删键都红；**列表遍历所有元素**，不是只看 `[0]`） |
| **`push_已截止的沉底但不压掉`** | 已截止在**最后一个**、但不是被压掉（`suppressed` 里没有它）；今天截止的**不沉底**；`unknown`/`absent` **不许**被归进已截止；`tally.expired` 对得上 |
| `push_提醒只对_found_且窗口取最紧一档` | 造 8/7/4/3/2/1/0/-1 八种 + `absent`/`unknown` 无提醒 + 免费层无提醒 |
| `push_免费层给前三条且说清截断` | 免费 3 条 / 订阅全量 / `truncated_reason`；开通码错 → 400 `BAD_CODE` |
| `push_limit_越界截断不报错` | `0/9999/2/4` 全 200；`abc` → 4xx 且是 JSON |
| **`push_数据旧了要说不知道_不能说没有`** | 数据 40 小时前：`stale=true`、有内容时 items 照给、没内容时 `empty_reason=stale_events`（**不许是 `no_match`**）；对照：数据新鲜时才可以说 `no_match` |
| **`push_事件文件一变接口就跟着变_不用重启`** | 追加一条事件后不重启也能读到（`(mtime_ns, size)` 重载）—— "30 秒发现"的后端一半 |
| `push_空的原因四态分得开` | `no_profile` / `no_events_file` / `no_match` / `null`，三句话两两不同 |

**全部用造的事件数据**（临时 records + 注入 `EventStore`），
不许赌 `data/out/events.jsonl` 里恰好是什么 —— 唯一例外是最上面那条
"真实事件流"回归，它**文件不存在时跳过**，但不静默跳过（逻辑由上一条钉着）。

**反向验证过**（issue 008 这一轮新增的四条，把实现改坏都会红）：

| 改坏什么 | 红在哪 |
|---|---|
| 完全不看 `is_backfill` | `补抓的旧公告进了 items：['e_changed_old', 'e_new', 'e_old']` |
| 按 `type == "added"` 一刀切 | `真新公告被误杀了：['e_changed_old']` |
| 不做同批 `notice_id` 合并 | `['e_add', 'e_dl', 'e_other']` |
| 真实数据上不过滤 | 真实流上 19 条早于 30 天的公告进了 items |

### 16.12 契约变更

**纯新增**：三个接口 + 一节契约 + 三个 fixture。不改任何已有接口的形状。
`/api/subscribe`（§5）保留不动，`/api/subscriptions` 是它的资源式视图，读写同一份状态。

---

## 17. 实现状态：**哪些接口已接前端，哪些没有**（2026-10-17，C 要求加）

> **这一节存在的理由**：契约里写着 40 个接口操作，但**演示只用到其中一部分**。
> 不加这一节，评委看到文档说"有三个功能"、页面上找不到，一点就露 ——
> 那不是"没做完"，是**文档说了一套、产品做了一套**。
>
> C 的原话：「请按"未接前端"标，别写"已实现"——尤其 `digest`。」

| 接口组 | 后端 | 前端 | 说明 |
|---|---|---|---|
| `/api/health` `/api/facets` | ✅ | ✅ | 页面顶部数据源标记、筛选项都靠它 |
| `/api/notices`（列表 / 详情） | ✅ | ✅ | 主链 |
| `/api/match` | ✅ | ✅ | 含 `field_trust`（2026-10-17 补） |
| `/api/letter` | ✅ | ✅ | 模板版 |
| `/api/faculty` `/api/faculty/match` | ✅ | ✅ | 导师页 |
| `/api/posts` `/api/posts/*` | ✅ | ✅ | 论坛（**回复功能未做**，见下） |
| `/api/changes` `/api/changes/history` | ✅ | ✅ | 今日更新页 |
| `/api/freshness` | ✅ | ✅ | 数据时间 / 实时刷新 |
| `/api/me` `/api/subscribe` `/api/subscriptions` | ✅ | ✅ | 付费墙、身份切换 |
| `/api/profile` | ✅ | ✅ | 画像（2026-10-17 接通：保存时同步到服务端） |
| `/api/push/preview` | ✅ | ✅ | 推送预览 |
| **`/api/updates`** | ✅ | ⛔ **未接** | 今日/本周更新（2026-10-18 新增，§20）。C 现在是前端自己按 30 天分档，接口版还没接 |
| **`/api/library`** | ✅ | ⛔ **未接** | 资料库（2026-10-18 新增，§20）。同上 |
| **`/api/watch`** | ✅ | ⛔ **未接** | 关注规则。演示只有 3 分钟，讲不清"你关注了什么、它怎么每天筛" |
| **`/api/digest`** | ✅ | ⛔ **未接** | 提醒摘要。**订阅卖点之一，但页面上没有** —— 说它"已实现"是误导 |
| **`/api/quality`** | ✅ | ⛔ **未接** | 数据质量面板。那是 PPT 里放一张表的活，不是页面功能 |
| **`/api/admin/*`（6 个）** | ✅ | ⛔ **未接** | 审核后台 / 审计 / 触发抓取。演示里点一下会真跑分钟级抓取，**而且假鉴权露出来还得解释一遍** |
| `/api/subscriptions/{channel}` | ✅ | ⛔ **未接** | 关渠道。演示用不到（`DELETE /api/subscriptions` 才是"取消订阅"） |

**这些"未接"是决定，不是欠账**（C 19:22 明确说"这一轮不打算做"，理由在上表）。
**后端它们是可用且有门禁盯着的**，只是页面上没有入口。

### 一个必须说清的半成品：论坛没有回复

`posts[].reply_count` **字段存在、但恒为 0** —— 没有任何接口能回复。

- **契约保留这个字段**（C 的建议：不该因为前端还没做就改形状，等真要做回复时字段已在手上）
- **前端已改成 `reply_count > 0` 才显示**，所以卡片上不会再出现「回复 0」
- 对外说明：**论坛只能发帖、举报、审核，没有回复**。别写成"论坛功能完整"。

> C 的原话比这更重：「它不只是"用户会找回复框"，它会让人以为**这个论坛有人在用**。」

## 18. 推送针对性：四维加权 + 可解释（2026-10-18，B 写）

> 起因：首屏 20 条里"对口"这件事**没有体现在名词上**。旧排序主键是数据层的
> `relevance`（0/1/2），而真实数据里 `relevance=2` 占绝大多数 ——
> 等于排序只剩 `urgency_weight` 一个变量在动，用户看到的全是"马上截止"，
> 不一定是"跟我有关"。

### 18.1 四维，每维 0~1

| 维度 | 判据 | 缺失时 |
|---|---|---|
| `school_hit` | 画像 `preferred_schools` 里有一个是 `event.school` 的子串 | `0.0` |
| `major_hit` | 画像 `majors` 里有一个出现在 `event.title` / `unit` / `matched[]` | `0.0` |
| `direction_hit` | 画像 `interest_keywords` 同上 | **`0.5`**（见 18.3） |
| `freshness` | 按 `publish_date` 距今天数分档：`≤30`→`1.0`、`31~90`→`0.6`、`>90`→`0.2` | **`0.5`**（见 18.3） |

**子串方向固定为「画像词 in 事件文本」**，和 `matched_because` 一致 ——
反方向（`事件词 in 画像词`）会让"物理"命中"物理化学"，那不叫匹配，叫联想。

### 18.2 权重在配置里，不在代码里

```python
config.PUSH_SCORE_WEIGHTS = {"school_hit": 0.35, "major_hit": 0.25,
                             "direction_hit": 0.20, "freshness": 0.20}
```

**和必须是 1.0**（门禁盯着），每维权重可以用环境变量 `TUIMIAN_W_*` 覆盖。
排序键见 §16 那个公式块。

### 18.3 ⚠️ 三处"不知道"不许当成"没有"

这是本项目 §14 那条不变量在打分里的第三次落点：

| 情形 | **不许** | **必须是** | 为什么 |
|---|---|---|---|
| 画像没填 `interest_keywords` | `direction_hit = 0` | `0.5` + `detail.direction_state="no_profile"` | 0 的意思是"我们比过了、没有你的方向"，而这里根本没有可比的东西 |
| 事件没有方向数据（`matched[]` 空、标题也没命中） | `direction_hit = 0` | `0.5` + `direction_state="no_data"` | 我们没拿到数据 ≠ 这条公告和你的方向无关 |
| `publish_date` 读不到 / 是空串 | `freshness = 1.0`（当新）**或** `0.2`（当旧） | `0.5` + `"unknown"` | 当新 = 把八年前的公告叫「新公告」（issue 008 那个错）；当旧 = 把刚发的打进历史参考。**两边都是编造** |

发布时间在**未来**（数据错）也算 `unknown`，但 `age_days` 会给出负数，不许截断成 0。

### 18.4 `why`：给人看的理由，**至少一条**

```json
"why": ["意向学校命中：北京大学", "专业命中：物理学",
        "6 天前发布（30 天内）", "3 天内截止（高优）"]
```

- 非空、每条是真句子（门禁要求 `len ≥ 4`，防"相关""匹配"这种等于没说的填法）。
- 组成：命中的画像维度 + 新鲜度一句话 + 截止日**三态**各自的说法。
- **和 `matched_because` 不是重复**：`matched_because` 是**准入判据**（空 = 不推），
  回答"够不够格推给你"；`why` 是**解释**，回答"为什么排在这儿"。
  两个键都在响应里，`matched_because` 保留是因为 C 的 `pushes.js` 在读它。

### 18.5 这条契约**不**承诺什么

- **不承诺 `score_dims` 的加权和**：它不给。给了就等于把权重公式变成对外承诺，
  以后调口味会改掉屏幕上的数（和 `urgency_weight` 的注释同一条理由）。
- **不承诺"30 天以上绝不出现"**：`freshness` 只让它掉档。
  真实数据上"30 天以上不进首屏"是靠数据层的 `is_backfill`（§16.8）保证的，
  接口层只保证"两条除发布日外一样的记录，新的排前面"。
- **不承诺 `major_hit` 的召回率**：画像写"物理学"、标题写"物理"时，
  严格子串匹配是**不命中**的。这是已知的召回损失，宁可漏也不要靠联想补
  （见 §16.5 那条"那不叫匹配，叫联想"）。要提升得从数据层给结构化 `majors`。

### 18.6 首屏分级：`tier`（2026-10-18；**2026-10-19 改按 `notice_kind`**）

| 层级 | 判据（2026-10-19 起） | 位置 |
|---|---|---|
| **`tier: 1`** | **`notice_kind == "announcement"`**（推免/报名/考核一类，今天该办） | **全部进首屏**，排在二级之前 |
| **`tier: 2`** | `notice_kind == "other"`（招生相关但不是通告） | 只**补位**，带 `tier_reason` |
| 不进首屏 | `catalog` → `reference` 区；`emba` → 对学术型画像硬排除 | 见 §18.9 |

**`kind` 缺失（A29 之前的老数据）时退回旧规则**：命中学校或专业、且
`topic == "apply"` → 1，其余 2。条数记进 `tally.kind_unknown` ——
**不知道 ≠ 不是**，不许因为字段没落地就把整批降到二级。

**`tier` 排在分数之前，也排在"已截止沉底"之前**（2026-10-19，B22）：
一级整层在前，已截止只在自己那一层里沉底（理由见 §16.5 排序块）。

为什么必须是结构性保证而不是调权重：分数是 `Σ w·dim × urgency`，于是
"**只命中方向但三天后截止**"完全可以压过"**命中你的意向学校但还有一个月**"。

### ⚠️ 为什么又加了 `topic` 一维（2026-10-18 第二次改）

第一版只按"命中学校或专业"判。用 `u_num_demo` 实测**首屏 20 条里 9 条 `topic != apply`**：

- 北大物理学院的学术讲座 ×4（"物理论坛""名师面对面""TeV 研讨会""纳米材料报告"）
- 北京大学博士后奖申报通知（面向已获博士学位者）
- 转专业学生名单公示、2025 级大二项目选拔

它们**确实命中了"北京大学 / 物理学"**，但对一个要报 2027 推免的学生**毫无用处**，
却因为学校权重最高（0.35）占满首屏。

**这不是调参能解决的，是判据缺一维。** 加 `topic` 不改任何阈值，
只把"对口"从"同一个学院"收紧到"同一个招生动作"。

- `topic is None`（老事件没有这个字段）**不当成 `non_apply`**：不知道就不降级。
- 实测效果：`topic != apply` 从 **9/20 降到 3/20**，一级 17 条、二级 3 条补位。

`tally` 里同时给 `tier1` / `tier2` 两个数：**首屏条数不写死**，
一级有几条就显示几条，`daily_limit` 只是上限。

`tier_reason` 是一句人话（"命中你的意向学校"/"命中了你的学校/专业，但这条不是招生公告"），
前端可以直接当标签用。

### ⚠️ 命名撞车警告：推送的 `tier`（1/2）≠ 公告的 `tier`（live/recent/archive）

2026-10-18 晚，数据层（A24）给**公告**加了 `tier` 字段，取值
`"live"` / `"recent"` / `"archive"`（时效分层，见 `notice.schema.json`）。
而**推送项**的 `tier` 是 `1` / `2`（首屏分级，本节）。

**同一个名字，两个含义，两个接口。** 这在本项目是明确要防的事
（§20.2 那条"`is_historical` 不能按它在哪个接口里判"是同一个病）。

现状（**必须知道**）：

- `/api/notices` 的投影（`config.LIST_FIELDS`）**目前不含 `tier`**，
  所以两个 `tier` 暂时不会出现在同一个响应里；
- 但一旦有人把 A 的 `tier` 加进列表投影、或者把公告字段并进推送项，
  **同名不同义就会真的相撞**，而 TypeScript/JS 那边只看到 `tier` 一个词。

**建议（等拍板）**：推送侧改名 `push_tier`（它只出现在 push 项里，
改动面小），公告侧的 `tier` 保持。这条**没有执行** —— 要改就一次改完
（`push.py` / 契约 / 门禁 / `docs/TODAY-PUSH-SAMPLE.md` / C 的渲染）。

### 18.7 `topic == "apply"` 是**硬门槛**（2026-10-19 补，B15）

**事故**：实测"浙大物理 + 意向北大/清华/中科大 + 凝聚态"的画像，`/api/push/preview`
返回 3 条 —— 北京大学「优秀博士后奖的通知」、「第二届TeV物理前沿专题研讨会」、
「物院论坛（第七十六讲）」。`tally` 里 `tier1=0 / tier2=3`，**一条招生公告都没有**。

根因：tier2 兜底时没有 `topic` 门槛，于是拿讲座/会议/博士后奖凑数。

| 规则 | 说明 |
|---|---|
| **tier1 和 tier2 都必须 `topic == "apply"`** | 非招生内容一条都不进首屏 |
| 被挡的进 `suppressed`（`reason=not_apply`） | **挡了不等于没发生**，用户能看到"另有 N 条命中了但不是招生公告" |
| `topic is None`（老事件）→ `tally.topic_unknown` | 不进首屏，**但要能被看见**（不知道 ≠ 不是） |
| 命中了但**招生方式不符**（画像要推免、公告是统考）→ `reason=mode_mismatch` | 与 `/api/match` 同一套硬过滤（B18 的一半） |
| 全是讲座时 → `empty_reason="no_apply_today"` | 文案带**"最近一条是几号的"** |

**宁可空，不许凑。** 空页面比"三条讲座"诚实，而且空态还带着"最近一条是 10-16 的《…》"，
不是一句干巴巴的"暂无"。

> 为什么 `no_apply_today` 不能写成 `no_match`：`no_match` 是"跟你没交集"（用户该去改画像），
> 而这里是"有交集、但都不是招生公告"——**他改画像也没用**。两种沉默必须分开说。

### 18.8 负向维度 `misfit`：明显不对口的要降权（2026-10-19 补，B20）

**事故**：QA 用"浙大物理 + 意向北大/清华/中科大 + 凝聚态 + 直博"的画像实测，
首屏 10 条全是 `topic=apply`（B15 的硬门槛生效了），但：

```
#1  北京大学2027年高级管理人员工商管理（EMBA）专业学位硕士研究生招生简章
#2  27年全国硕士研究生招生考试北京大学（1101）报考点公告
#3  北京大学 2027 年硕士研究生招生简章（校本部）     ← 真正该看的在这
```

**根因不是权重没调好，是判据缺一维。** 四维全是**正向**的（学校命中就加 0.35），
"学校对了、这个项目根本报不了"这件事在分数里没有任何体现 ——
EMBA 那条 `school_hit=1.0`、还两天后截止，紧急度 ×2.0，**按四维排它必然第一**。

| 维度 `key` | 命中什么（只扫**标题**） | 乘数 | 为什么 |
|---|---|---|---|
| `degree_type` | EMBA / 高级管理人员工商管理 / MBA / MPAcc / MPA / MEM / 在职攻读 / 非全日制 | **0.35** | 这些项目**要求工作经验**，推免生（应届本科生）结构上报不了 —— 不是口味，是硬事实 |
| `transactional` | 报考点 / 考点 / 考场 / 准考证 / 网上确认 / 现场确认 / 考试安排 / 考试须知 / 复试录取工作办法 | **0.35** | 这是**统考报名流程**里的事务公告；本产品面对推免/夏令营人群。实测它带报名截止日、紧急度 ×1.2，只轻压（0.8）根本压不住（照样第 2） |

**三条边界，缺一条就会变成另一种错误：**

1. **只降权、不隐藏。** 项目类型同样可能判错（标题写「非全日制」的公告
   也可能同时招全日制）。降权错了只是低几行，藏起来就是用户永远看不到 ——
   和"已截止沉底不压掉""`relevance` 只排不滤"是同一条原则。
2. **画像自己就要专硕/管理类时，`degree_type` 不降权。** 判据只看用户**明确写的**：
   `target_levels` 里有专硕/MBA/非全日制，或 `majors` / `interest_keywords`
   本来就是管理类。否则会把"想读 MBA 的人"的简章全压下去，那叫纠正过度。
   空画像 → 仍降权（推免产品 + EMBA 要求工作经验，这条项目类型对目标人群不成立）。
3. **必须可解释。** `misfit.keys` 说命中哪几维、`misfit.text` 说命中了哪个词，
   并且 `text` 会**同时进 `why`**。降权可以，黑箱不行。

**实测（3b 画像，真实数据 2759 条事件）：**

```
改前   #1 EMBA 简章   #2 北大报考点公告   #3 北大招生简章
改后   #1 北大招生简章  #2~#5 目录/通知    #7 EMBA（w=0.35）  #9 报考点（w=0.35）
```

EMBA 从 #1 沉到 #7，报考点从 #2 沉到 #9，**两条都还在列表里**。

**为什么不做成第五个 `score_dims`**：四维回答"这条和你多对口"（正向、可加权求和），
负向维度回答"这条你根本报不了/不需要"—— 两者含义不同。塞进同一个加权和，
`score_dims` 就变成一个含义混杂的和，而它的每一维都是要给前端分别显示的。


### 18.9 首屏三层：`notice_kind` 分层 + 参考资料区（2026-10-19，B22）

**事故**：浙大物理 + 北大/清华/中科大 + 凝聚态 + 直博的画像，首屏 10 条是
**6 条目录/简章 + 2 组重复 + 1 条 EMBA + 1 条真公告**。`topic=apply` 把
"今天该办的通告"和"随时可查的目录"混成了一类。

`notice_kind` 四档（A29，`collector/kind.py`）：

| 值 | 含义 | 去哪儿 |
|---|---|---|
| `announcement` | 推免 / 夏令营 / 报名 / 复试 / 调剂 / 材料 / 考核… | **一级**，全部进首屏 |
| `other` | 招生相关但不是通告 | **二级**，只补位 |
| `catalog` | 招生目录 / 专业目录 / 简章 / 章程 / 报考指南 | **不进首屏**，进 `reference` 区 |
| `emba` | EMBA / MBA / MPAcc / 在职 / 非全日制 | 对**直博/学术型**画像硬排除；明确要专硕/管理类的留在二级 |

**硬门槛（不放宽）**：`announcement` + 命中画像 + 排除 `catalog`/`emba` + 排 junk。

**时间窗 60 天**（2026-10-19 用户拍板，从 30 放宽）：推免季的公告 9 月发、
10 月底才截止，只算 30 天命中的自然稀缺（全库 17 条、命中画像 2 条）。
放到 60 天，全库 **37** 条、命中 **6** 条，而且**仍然有效**。

- **窗口自己判，不只信 `is_backfill`**：真实数据里有一条 178 天前的推免简章
  进了首屏 —— 它的 `added` 事件不是补抓（数据层只按 30 天标 `is_backfill`）。
  现在 `publish_date` 超过 60 天 → `suppressed(reason=out_of_window)`。
- **窗口内的 announcement 不受 `is_backfill` 拦截**：45 天前发布的推免办法
  今天仍然有效，不能因为"发布超过 30 天"就整批压掉。
- **`other` 也过窗口**：二级不该出现去年的东西。

**「不凑」原则**（用户原话）：*宁可显示 5 条全对口，也不要 10 条里 6 条是凑的。*
一级有几条给几条，二级只补位，**总共 5 条就给 5 条** —— 不拿别校的目录凑数。

**`reference` 区**从**公告库**现算（不从事件流来 —— 目录的价值是"随时可查"，
不是"今天变了"）：命中画像 + 非 junk + 招生方式对得上 + 同校同标题去重，
按发布日倒序，最多 `config.PUSH_REFERENCE_LIMIT`（默认 10）条，
`total` 给去重后的总数。

**实测**（3b 画像，2561 条公告）：

```
items 9 条 = 6 条 announcement + 3 条 other（没有 catalog / emba）
reference.total = 34（展示 10）
tally: catalog 34 / emba_excluded 4 / out_of_window 8 / kind_unknown 0
页头：近 60 天全库 37 条招生通知，其中 6 条对口你
```

> **C 要改的两处**：① `tier_reason` 现在按类型说（"招生通知"/"其他招生相关"），
> 旧标签"命中你的意向学校"只在 `kind` 缺失的老数据上还成立；② 新增 `reference` 区与
> `tally.apply_fresh_announcement(_matched)`，页头那句用它。
> 推送项还多了 `notice_kind` / `is_backfill`。



> 起因：1120 条里有 **115 条是脏的** —— 标题就是站点名（「天津大学研究生招生网」），
> 或者正文只剩"发布时间 / 点击数 / 分享至"。它们混在列表和推送里，
> 生成的是零引文的套磁信和没有标题的推送卡。
> 数据层（A1–A3）已经把这四个字段落盘，这一节规定接口怎么**透出**和**怎么收**。

### 19.1 四个字段，四个接口，**同名同义**

`GET /api/notices`（列表项）、`GET /api/notices/{id}`（详情）、
`POST /api/match`（item）、`GET /api/push/preview`（item）**都给这四个键**：

| 字段 | 取值 | 谁产出 |
|---|---|---|
| `record_quality` | `"ok"` / `"thin"` / `"junk"` / **`"unknown"`** | 数据层 `collector/quality.py`；`unknown` 是**接口层独有** |
| `topic` | `"apply"` / `"non_apply"` / `"other"` / `null` | 数据层 `collector/topic.py` |
| `is_fresh` | `true` / `false` / `"stale"` / `null` | 数据层 `collector/timeliness.py` |
| `age_days` | 整数 / `null` | 同上 |

**平铺，不套 `quality: {...}`** —— C 的卡片在列表页、匹配页、推送页共用一份渲染，
形状不同就得写三套，而漏掉一处不会报错，只会让某个页面显示空白。

`is_fresh` 的 `"stale"` 是数据层定的**第 4 个取值**（>180 天），不是布尔。
把它压成 `false` 就丢掉"半年以上"这一档，而 C 的折叠功能要用它。

### 19.2 默认拒收 `junk`，但要能查、要说出来

| 接口 | 默认 | 显式开关 |
|---|---|---|
| `GET /api/notices` | 排除 `record_quality="junk"` | `?include_junk=true` |
| `GET /api/push/preview` | 排除，并进 `suppressed`（`reason="junk"`）+ `tally.junk_dropped` | — |
| `GET /api/notices/{id}` | **不排除** —— 按 id 能查到是"可追溯"的底线 | — |
| `POST /api/match` | **不排除** —— 它要暴露 `record_quality` 让调用方自己决定 | — |

三条边界，每条都有理由：

1. **`total` 跟着变。** `include_junk=true` 时 `total` 必须**恰好**多出 junk 条数
   （实测 1005 → 1120，差 115）。前端拿 `total` 算页数，两个数不一致会"最后一页是空的"。
2. **只排 `junk`，不排 `thin`。** `thin` 是"正文 100~300 字"，多半是"见附件"型公告，
   附件里才是正文 —— 那不是脏，是形态。
3. **推送里"被排掉"必须说出来。** `suppressed` 里带上 `reason="junk"` 和一句人话，
   `tally.junk_dropped` 单独给一个数（它和 `duplicate` / `merged` 混在一条里数不出来）。
   只拿掉不给理由，用户会问"今天怎么只有 13 条"，而 `tally.matched` 又和 `sent` 对不上。

### 19.3 ⚠️ "没标过" ≠ "检查过没问题"

| 情况 | **不许** | **必须是** |
|---|---|---|
| 老记录没有 `record_quality` | `"ok"` | **`"unknown"`** |
| 没有 `topic` | `"other"`（那是一个判断结果） | `null` |
| 没有 `is_fresh` | `false`（"不新"是一个判断） | `null` |
| 没有 `age_days` | `0`（0 的意思是"今天发的"） | `null` |

这是 §14 那条不变量在质量字段上的第三次落点。默认成 `"ok"` 等于替数据层宣布
"这条我们检查过、是好的" —— 那是编造。

### 19.4 `is_fresh` / `age_days` 是**上一次管道运行时刻**的值，不是请求时刻

数据层用 `date.today()` 在管道运行那一刻算完落盘（`collector/timeliness.py`），
接口层**原样透出、不重算**。理由写在 `server/quality.py` 的模块 docstring 里，两条：

1. 两套时钟会打架（这个项目已经栽过四次"同一件事两处实现"）；
2. 列表 fixture 是**逐字段严格比对**的，现算的话 `age_days` 每天变一次，
   固化的 fixture 从第二天起就与接口不等（`match.response.json` 就是因为这个
   才在 `META.json` 里声明了 `_time_sensitive`）。

**代价要说清**：管道停了这两个值就冻住。数据多旧看 `/api/freshness` 和 `source.data_age_hours`。
要请求时刻的真值，那是数据层的活，不是接口层偷偷换一套算法。

> 例外：**推送项**的 `is_fresh` / `age_days` 是按 `publish_date` 现算的 ——
> 因为 `events.jsonl` 里根本没有这两个字段（它是"发生了什么"，不带时效快照）。
> 口径与数据层相同（30 天 / 180 天），差别只是计算时刻。这一点是**已知的不对称**。

### 19.5 这条契约**不**承诺什么

- **不承诺"脏记录绝不出现在任何地方"**：详情和匹配照给，只是打了标。
  藏起来就没人能复查，而"我们少了一条"是我们最需要能自证的一件事。
- **不承诺 `record_quality` 判得对**：它是一条会变的判断（改了选择器就可能翻转）。
  契约承诺的是"它被如实标出并且可追溯"，不是"它永远准确"。

## 20. 今日更新 / 资料库：把事件流分成两半（2026-10-18，B 写）

> 起因：`events.jsonl` 1131 条里有一大半是补抓翻出来的历史文章（发布日半年以上）。
> 把它们和"今天新增"放在同一个响应里，那个响应就不能叫「今日更新」——
> 用户看到 1131 条会说"这哪是今天"，而我们把一个**存档**叫成了**动态**。

### 20.1 两个接口，一个边界

| 接口 | 给什么 |
|---|---|
| `GET /api/updates` | **新**：发布日在 `days` 天内（默认 7）、未过期、且非 `junk` |
| `GET /api/library` | **其余全部**（`updates` 的补集），可按学校 / 年份 / topic 筛 |

```
updates = 发布日 ≤ days 天  且  未过期  且  非 junk
library = 其余全部          ← 补集，不是"另外查一遍"
```

**并集 = 事件全集，交集 = 0。** 恒等式：`updates.total + library.total == 事件总数`
（实测 27 + 1104 = 1131）。

> ⚠️ **两个接口的 `days` 必须传同一个值。** 分区边界只有一份：
> 一边取 30 天、一边用默认 7 天，8~30 天那一批会**同时出现在两边**
> （实测差 85 条）。这不是"略有重叠"，是交集直接不为空。
> 所以 `library` 也收 `days`，默认值与 `updates` 相同。

### 20.2 `is_historical` **不是**"在 library 里"

判据是 **30 天以上 或 已截止**，和它出现在哪个接口无关。

资料库按发布日降序，第一页很可能全是"20 天前、不在本周窗口内、但也不是历史"的记录。
如果按"在这个接口里"标 `true`，那文档就在说假话 —— 同一个词在两个接口里指两件事。

每一条都带 `is_historical` + 一句 `history_reason`（"120 天前发布（历史参考）" /
"已截止 3 天（仍保留，学校可能延期或补发附件）" / "20 天前发布，不在本周窗口内"）。
要看真正的历史用 `?historical=true`（实测 972 条）。

### 20.3 ⚠️ "没读到截止日"不算过期

只有 `deadline.state == "found"` 且 `days_left < 0` 才算过期。
`unknown` / `absent` **不算** —— 把"我们没读到"当成"已经过期"，
代价是**一条还能报名的公告被归档进资料库**，而用户不会去资料库找它。
这是 §14 那条不变量的第四次落点。

### 20.4 `year` 同时认三个年份

`?year=2027` 匹配 **发布年 / `admission_year` / `target_year` 任一**。

只比发布年会让 `?year=2027` 返回 0 条 —— 而这个产品的年份是**招生年**，
一条 2027 年招生的公告往往是 2026 年发的。实测三种口径合并后 `year=2027` 有 163 条。

### 20.5 和 `/api/changes`（§10）的区别

| | 回答的问题 | 数据源 |
|---|---|---|
| `GET /api/changes` | 我们**这一轮**发现了哪些**变化**（added / changed / removed…） | `changes.json`（差分） |
| `GET /api/updates` | **现在**有哪些值得看的新公告 | `events.jsonl`（分区） |

一个是"差分"，一个是"分区"，**不能互相替代**：差分里会有 `reparsed` 这种
"内容没变、只是重解析了"的项，那不该出现在"今日更新"里。

### 20.6 这条契约**不**承诺什么

- **不承诺 `updates` 一定非空。** 空的时候给 `empty_reason="no_fresh_events"` 和一句
  "这不等于今天没有更新 —— 可能管道还没跑，看 `/api/freshness`"。
  把"我们没数据"说成"今天没新闻"，是这个项目一直在防的那类错。
- **不承诺资料库只含历史。** 它含的是"不在本周窗口内的全部"，
  其中有一些还很新（见 20.2）。

## 21. 数据心跳：`GET /api/health/data`（2026-10-18，B 写）

> 起因：定时任务上线之后出现了一类新的沉默 —— **任务没跑，页面上一切正常**。
> 接口 200、形状对、数据是上一次的。它不说谎，它只是过时。
> `/api/freshness`（§12）说的是"每所学校上次抓到什么时候"，
> 这一节说的是"**我们自己的运行有没有成功**"。

### 21.1 响应

```json
{
  "last_run": {"at": "2026-10-18T11:41:03", "kind": "fast", "ok": true,
               "seconds": 130.1, "new_notices": 41, "new_events": 2},
  "runs_24h": 2, "runs_24h_failed": 0, "runs_24h_unknown": 1,
  "consecutive_ok": 1,
  "last_full_at": "2026-10-17T23:38:22",
  "notices": 1120, "events": 1133,
  "fresh_within_3d": 10, "fresh_within_7d": 27,
  "data_age_minutes": 11
}
```

数据来源：`data/logs/*.log` 的起止标记 + `data/out/stability.jsonl` + 数据文件本身。

### 21.2 ⚠️ `ok` 是三态，`null` 不是失败

| 值 | 意思 |
|---|---|
| `true` | 有显式 `exit=0`，**或**走到了最后一行（`记录 N 条 ->`，结构性证据） |
| `false` | 显式退出码非 0，**或**起了手但超过 20 分钟没有结束标记 |
| **`null`** | **我们不知道**：既没有退出码、也没有结构性证据；**或者这一轮正在跑** |

把 `null` 说成 `false` 会让人去查一个没坏的东西；说成 `true` 会在任务真挂掉时
让人以为一切正常 —— **后者最贵**。§14 那条不变量的第五次落点。

### 21.3 "正在跑"是正常状态

没有结束标记时，看**开始到现在有多久**：20 分钟内算"正在跑"（`ok=null`，
`in_progress=true`），超过才算中途退了（`ok=false`）。

**这一条是踩出来的**：第一版把"没有 end 标记"一律当成"中途退了"，
于是定时任务每跑一次、门禁就红一次 —— 而它跑得好好的（实测 13:03 那一轮）。
一个会在正常运行期间变红的指标，最后一定会被所有人忽略。

`consecutive_ok` 里"正在跑"**既不计数也不打断连胜**：它还没有结果。
打断的话这个数在半夜看是 12、白天看是 0，一个自己会抖的指标没人会信。

### 21.4 这个接口**不**承诺什么

- **不承诺每轮都有退出码。** 实测 `data/logs/fast.log` 里一条 `exit=` 都没有
  （那一轮不是从 `run_fast.cmd` 起的）。所以判定要走三层：退出码 → 结构性证据 → `null`。
- **不承诺 `runs_24h` 覆盖所有历史**：只读 `data/logs/` 里现存的日志，
  日志被轮转/清理过就数不全 —— 那时 `runs_24h` 偏小，但 `last_run` 仍然是最近一轮。

## 22. 性能预算：**耗时和体积也进门禁**（2026-10-18，B 写）

> 起因：2751 条数据下 `/api/push/preview` 是 **535 KB / 3.3 秒**，而门禁 193 条**全绿** ——
> 因为它只验形状和语义，**没有任何一条在量"这个响应有多大、要多久"**。
> 这是演示第一屏要打的接口，3 秒白屏就是"点了没反应"。
> **数据再翻一倍，同一个坑会再踩一次，而且没有人会发现。**

### 22.1 硬指标

| 接口 | 体积上限 | 耗时上限 | 备注 |
|---|---:|---:|---|
| `GET /api/push/preview` | **< 60 KB** | < 300 ms | 演示第一屏 |
| `GET /api/library` | — | < 300 ms | |
| `GET /api/updates` | — | < 300 ms | |
| `GET /api/notices` | — | < 300 ms | |
| `GET /api/health/data` | — | **< 100 ms** | 挂在页头上，每次切页/轮询都打 |

耗时取**三次里的最小值**：机器负载会抖，取 min 才是"这段代码要多久"，
取 max 会变成"刚才那台机器有多忙"。

### 22.2 两处根因（都记下来，因为都是"数据小时看不出来"的类型）

**① `suppressed` 全量返回**（523 KB / 2739 条）。
修法见 §16.7：只给每种原因前 3 条样例 + 计数 + 分页出口。

**② `store.get()` 的 O(n²)**。它原来是

```python
[r for r in self.records if r.get("id") == notice_id]
```

而 `/api/push/preview` 对**每一条事件**都调一次：2759 事件 × 2751 公告 ≈ **760 万次比较**。
数据 1135 条时只要 100 ms 上下，**完全看不出来**；到 2751 条就变成演示白屏。
修法：`NoticeStore` 一次性建 `id -> [记录]` 索引（`_id_index()`），之后 O(1)。

### 22.3 实测（2751 条 / 事件 2759）

| 接口 | 改前 | 改后 |
|---|---:|---:|
| `/api/push/preview` | 535.2 KB / 472 ms | **33.2 KB / 100 ms** |
| `/api/library` | 14.2 KB / 608 ms | **14.2 KB / 39 ms** |
| `/api/health/data` | 0.4 KB / 218 ms | **0.4 KB / 7 ms** |
| `/api/updates` | 18.1 KB / 8 ms | 18.1 KB / 8 ms |
| `/api/notices` | 35.6 KB / 25 ms | 35.6 KB / 25 ms |

（`/api/health/data` 的 218 ms 也是"重读 10 MB notices.jsonl"，修法：接口层把**已加载在
内存里的记录**传进去，另加 3 秒 TTL 缓存。）

### 22.4 这条纪律**不**承诺什么

- **不承诺绝对毫秒数**：门禁用的是"本机、真实数据、取三次最小值"，
  换一台慢机器这些数会变。它守的是**"没有随数据规模变差的东西"**，
  不是"这台机器上就是 100 ms"。
- **不承诺覆盖所有接口**：只盯首屏那几个。加新接口时如果它返回列表级数据，
  应该顺手加进来。




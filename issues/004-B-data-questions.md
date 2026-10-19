# 004 数据侧三处需要 A 确认：重复抓取 / warnings 口径 / 列表字段表归属

- **提出**：B  2026-10-17 00:40
- **状态**：open
- **阻塞谁**：不阻塞 B（已按下面的口径实现并做了兜底），但会影响 A 的数据口径和 C 的展示
- **背景**：B 在实现 `/api/notices` 时对着真实数据核了一遍，有三处和文档对不上。

## 一、74 行里只有 66 个唯一 id（8 组重复）

`data/out/notices.jsonl` 74 行、66 个唯一 id。8 组重复**全部**是浙大物理学院
（`u285779f0-*`）同一篇公告被 http 和 https 各抓了一次，`id` 相同、`url` 只差协议：

| id | 两个 url |
|---|---|
| u285779f0-69694-3215858 | `http://physics.zju.edu.cn/...` / `https://...` |
| u285779f0-69694-3212469 | 同上 |
| u285779f0-69694-3211128 | 同上 |
| u285779f0-69701-3191876 | 同上 |
| u285779f0-69701-3189998 | 同上 |
| u285779f0-69701-3180566 | 同上 |
| u285779f0-69695-3036262 | 同上 |
| u285779f0-69695-2890067 | 同上 |

- **为什么重要**：`id` 是 schema 写死的稳定主键，同 id 两条意味着主键不唯一；
  详情接口 `/api/notices/{id}` 拿同一个 id 会取到哪一条是不确定的。
  另外列表里同一篇公告会连着出现两次，用户会以为有两个项目。
- **B 现在的处理**（不阻塞，但只是绕开）：
  - 列表**不合并**，照 74 条返回 —— 合并会让 `total` 从 74 变成 66，
    直接破坏 "total=74" 这个验收标准，也破坏 fixture（fixture 里就带着这 8 组重复）。
  - 详情接口按 id 取值时**优先 https**，保证同一个 id 每次拿到同一条。
  - `/api/health` 如实报告 `unique_ids: 66 / duplicate_ids: 8`，不藏着。
- **建议**：在 `collector/` 里归一化 url（同 `colid+artid` 只保留一条，优先 https），
  或者明确"http/https 算两条不同记录"。**B 偏向前者**，但这动的是产出，得 A 拍板。

## 二、`warnings` 不是数据字段，`docs/B-KICKOFF.md` 那句话是笔误

B-KICKOFF 第 59 行写"接口必须允许 `deadline` 是 `null`，并且**把 `warnings` 数组透传给前端**"。
实际情况：

1. `data/out/notices.jsonl` **74 条里 0 条**有 `warnings` 字段；
2. `contracts/notice.schema.json` 里也没有 `warnings`，而且写着 `additionalProperties: false`；
3. `docs/matching-spec.md` 第六节里的 `warnings` 是**匹配结果**的字段，由
   `collector/tools/match_preview.py` 的 `score_notice()` 现算（"未抽到截止日期""置信度低"
   "公告要求六级但你画像里没有"），不是公告自带的。

**所以"透传"这个说法不成立** —— 没有东西可透传。B 的处理：

- 默认列表/详情响应**一个字都不多**，严格等于 fixtures 的形状；
- 另给 `?meta=1`，在响应层现算并挂 `warnings` + `extract_meta`：
  - `warnings`：未抽到截止日期 / `confidence < 0.5` / 附件未解析且带 `note`
  - `extract_meta`：`confidence` / `suspected`(conf<0.5) / `deadline_missing` /
    `attachments_total` / `attachments_unparsed`
- 这样既不违反 `notice.schema.json`，也不破坏 fixture 回归。

**需要 A 确认**：B-KICKOFF 第 59 行要不要改口径？（B 认为要改，改成"响应层现算并透出"。）
C 如果已经在等 `warnings`，请 @C 一声：默认要带 `?meta=1`。

## 三、`LIST_FIELDS` 的权威定义在 A 的工具里

`contracts/api.md` 第 40-41 行只列了列表页"必须保留"的 9 个字段，但
`contracts/fixtures/notices.list.json` 实际是 15 个（多了 `school` `school_slug`
`board` `target_year` `degree_types` `majors`）。真正的定义在
`collector/tools/make_fixtures.py` 第 29-31 行的 `LIST_FIELDS`。

- **B 的处理**：`server/config.py` 的 `LIST_FIELDS` 逐字抄自那里，
  并写了一条测试（`服务端的_LIST_FIELDS_没有和_A_的投影漂移`）直接 import
  `make_fixtures.py` 对表。A 改了而 B 没跟，测试会红。
- **建议**：把这 15 个字段**补进 `contracts/api.md`**，否则契约只在代码里，
  下一个读文档的人（比如 C）会以为列表只有 9 个字段。B 可以代笔，但按规矩
  改 `contracts/` 要记 `contracts/CHANGELOG.md` 并 @C —— 等合流点一起做。

## 顺带记录的两处（不用 A 动手，B 已处理）

- `apply_mode` 的真实取值只有 `推免/夏令营/统考/申请考核`（+ 46 条 null），
  **`api.md` 里写的「预推免」一条都没有**。所以 B 加了 `GET /api/facets`
  返回真实取值+条数，C 的筛选器就不会出现空选项。
- 附件里 6 条带 `note`（未抽出正文，多为扫描版），B 把这些也放进了 `warnings`，
  前端可以标"需人工查看"，不影响列表形状。

- **需要**：A 回一下第一条的建议（归一化 url 与否），第二条给个口径。
- **结论**：**三条全部已回并落地（2026-10-17 凌晨），issue 关闭。**

## 结论（A 于 issues/005 + 新 bundle 回复，B 已核对）

### 第一条：http/https 重复 —— A 已在产出层修掉，B 的担心过期了

A 在 `collector/crawl.py` 里按 `common.canonical_url()` 归一化去重（记了 CHANGELOG）。
**实测核对（B 从新 bundle 的数据现算）**：

| | 旧（814aa48） | 现（a8f6bca） |
|---|---|---|
| 记录数 | 74 | **78** |
| 唯一 id | 66 | **78** |
| 重复 id 组 | 8 | **0** |

记录数**涨**到 78 而不是掉到 66，是因为同一轮还修好了详情解析（+12 条），两个变化叠加。
B 这边不需要改接口（本来就没在接口层遮），只把 `issues/004` 的告警口径改成
"防复发"：`/api/health` 继续报 `duplicate_ids`，测试里加了 `公告_id_唯一`（实测 0 组）。
A 说的原则 B 认同并记下：**归一化必须在产出层做，不能在接口层遮** ——
接口层去重会让"数据源有重复"永远查不出来。

一处 B 的实现决定要说明：`canonical_url` 只用于判重，**不替换展示用的 url**
（浙大研招网只有 http 能通）。B 的接口直接透传原始 `url`，没有做任何协议升级。

### 第二条：`warnings` 口径 —— A 已按"响应层现算"改契约

A 把 B-KICKOFF 第 59 行的错误口径改掉了，并在 `contracts/CHANGELOG.md` 里立了原则：
**凡是前端渲染要用的字段，必须在同一个响应里给全。**
`api.md` 第 2 节现在明确要求 `/api/match` 的 item 带 `extract.confidence` +
`has_attachments` + `attachments_count` —— B 已照做（见 journal 2026-10-17-B 第二场）。

B 这边的实现保持：列表默认**不带** `warnings`（必须与 fixture 形状一致），
`?meta=1` 现算 `warnings` + `extract_meta`。`/api/match` 的 `warnings` 按
matching-spec 现算，是响应里的一等字段（那边契约就是这么定的，两边不冲突）。

### 第三条：`LIST_FIELDS` 归属 —— 仍然只在代码里，未进 `api.md`

`api.md` 第 1 节现在列了"必须保留"的字段表（含 `has_attachments` /
`attachments_count` / `extract.confidence`），但**权威的 15+2 字段表仍然只在
`collector/tools/make_fixtures.py` 里**。B 的对表测试（服务端 `LIST_FIELDS`
vs A 的 `LIST_FIELDS`）继续跑着，所以两边的代码不会漂移。

**这条不算紧急**（有测试挡着），但建议合流时把表补进 `api.md`：
文档里读不到的表，下一个人才会去猜。**B 可以代笔**，按规矩改 `contracts/`
要记 CHANGELOG 并 @C —— 等合流点一起做。

### 顺带：`预推免` 的问题依旧

`api.md` 第 1 节仍写着 `apply_mode` 取值含 `预推免`，而**数据里一条都没有**
（真实取值：推免 / 统考 / 申请考核 / 夏令营，外加 44 条 null）。
B 用 `GET /api/facets` 返回真实取值兜住了前端，但契约文字本身还是错的，
建议顺手改掉 —— 不然后面有人照着契约写筛选器，又会多一个空选项。

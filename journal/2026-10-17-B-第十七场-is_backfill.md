## B（后端）— 第十七场：修 issue 008（推送排除补抓的旧公告）

> 用户/A 的原话：**"第三轮只做四件，别扩。"**
> 四件全做完，没扩。门禁 146 → 149（+3 条新用例）。

### 做了（就这四件）

| # | 事 | 改哪 |
|---|---|---|
| 1 | **【阻塞】`is_backfill=true` 不进 `items`**，进 `suppressed(reason="backfill")` | `server/push.py` |
| 2 | 同一批里**按 `notice_id` 只留一条**（留信息量大的），被合并的进 `merged` | `server/push.py` |
| 3 | `POST /api/subscriptions` 的 `user` 改成**从 query 取**（兼容 body） | `server/app.py` |
| 4 | `demo_push.py` 从样例切到**真实事件流** + 新增 3 条门禁 | `demo_push.py` / `run.py` |

**四件之外只加了一个键：`items[].publish_date`。** 理由写在下面第 4 段 ——
验收标准是"items 里没有早于 30 天前的"，而 item 原本没有发布日字段，
**这条验收根本没法机器做**。不加这个键，A 只能去 `body` 字符串里抠日期。

### 关键判断：`is_backfill` 只按事件字段判，不按 type、不按发布日重判

A 在 issue 008 里特别警告了"别一刀切"，我把它当成两条独立约束来验：

1. **不按 `type == "added"` 切。** 数据层对 `deadline_changed` / `attachment_added`
   **永远不标** `is_backfill` —— 再老的公告改了截止日也该报。
   门禁里专门造了一条**没有 `is_backfill` 字段的 `deadline_changed`**，
   它必须照推。反向验证时我故意改成 `type == "added"` 一刀切，
   门禁红在「真新公告被误杀了」——**这正是那个错法的形态**。

2. **不按"发布日早于 30 天"重判。** 30 天是**数据层判定 `is_backfill` 的口径**，
   接口层再判一遍就是"同一个策略写在两个地方"（这个项目栽过 4 次的那类）。
   一处改了另一处不跟，而那时没人知道该信哪个。

**代价我也认**：如果哪天数据层忘了标 `is_backfill`，我会把旧公告推出去。
但那个错误是**可见的**（用户看到"2013 年"就知道不对），
而接口层自己重判的错误是**隐形的**（某条真公告因为发布日早了几天被吞掉）。

### 第 2 件为什么"留 deadline_changed 不留 added"

同一批里同一条公告会同时有 `added` 和 `deadline_changed`。留哪个？

我选**信息量大的**：`deadline_changed`（带"从 X 改成 Y"）> `attachment_added`（带附件名）
> `added`（只说"有新公告"）。

理由：用户关心的是"什么时候截止"，而不是"这条是不是新的"。
如果同批里既有"新公告"又有"截止日变了"，后者对他更有用。

**被合并掉的那条进 `suppressed(reason="merged")`，不静默丢掉** ——
丢掉的话 `tally.matched` 和 `sent` 会对不上，用户会以为漏推了。
这一条是我自己加的约束（A 只说"留哪个由你定"），因为它和整个项目的
"压掉的必须说出来"是同一条。

### 怎么验的

**反向验证（四条，全部会红）**：

| 改坏什么 | 红在哪 |
|---|---|
| 完全不看 `is_backfill` | `补抓的旧公告进了 items：['e_changed_old', 'e_new', 'e_old']` |
| 按 `type == "added"` 一刀切 | `真新公告被误杀了：['e_changed_old']` |
| 不做同批 `notice_id` 合并 | `['e_add', 'e_dl', 'e_other']` |
| 真实数据上不过滤 | 真实流上 **19 条**早于 30 天的公告进了 items |

**A 的验收标准实测（他的画像、真实数据、付费身份）**：

```
验收 1：items 里没有早于 30 天前的   → PASS（2 条，发布日都是 2026-10-08）
验收 2：suppressed 出现 reason="backfill" → PASS（44 条）
对照：同一条公告有没有重复出现        → PASS（无）
对照：POST /api/subscriptions 的 user 位置 → PASS（query 优先，body 兼容）
```

### 卡在哪（**这条要写进 PDF / 录屏脚本**）

**真实数据上现在只有 2 条推送，而且三态只出现 `absent`、一条提醒都没有。**

```
tally: events=90  matched=46  unmatched=44  backfill=44  sent=2  suppressed=44
items: 2 条，发布日都是 2026-10-08，deadline 都是 absent
```

is_backfill 修好了（这是对的），但**副作用是真实数据被筛到只剩两条**：
- 三态演示不出来（只有 `absent`）；
- T-7 提醒演示不出来（没有 `found` 的截止日）；
- 免费层截断也演示不出来（2 条 < 3 条上限）。

**建议的录法（我写进 RUN.md 了）**：先用真实数据证明"它真的在跑"（顺便讲那 44 条 backfill），
再切 `?source=sample` 展示三态和提醒，**画面里保留 `source.kind = sample` 那一行**。

两句过渡可以直接说：
> *真实数据今天只发了两条，剩下那些态是我们用样例演示的。*

### 需要谁

**给 C 三点（都不破坏现有渲染，但第 2 点建议补）**：

1. `items[].publish_date` 新增 —— `pushes.js` 目前不读它，加了无害。
   如果想在推送卡片上显示"发布于 X"，现在有结构化字段了。
2. **`empty_reason` 新增第五个值 `only_backfill`**：命中画像的**全是补抓**时用它。
   前端若只判四态，会落到默认文案。建议补一句：「今天匹配到的都是我们补抓时
   翻出来的历史公告」。**说成 `no_match` 会让用户去改画像，而画像没问题。**
3. `tally.backfill > 0` 时界面最好提一句「另有 N 条是补抓的历史公告，不计入推送」
   —— 和"压掉的必须说出来"是同一条原则。

**给 A 一条**：真实事件流现在只有 `added`（90 条全是），没有 `deadline_changed` /
`attachment_added`。所以"截止日变了"这类推送在真实数据上一次都演示不到。
如果下一轮能造出这两类变更，我把 RUN.md 里"两步录法"改成一步。

### 契约动没动

**动了，非破坏**（只加键、只加枚举值）：`api.md` §16.2 / §16.4 / §16.5 / §16.7 / §16.11
+ `CHANGELOG.md` 一条（含"对 C 的三处影响"）。

**没有删字段、没有改类型、没有改已有接口的形状。**

### 我自己踩的两个坑

1. **形状门禁只看 `items[0]`。** 我写的 `key_paths` 对列表只取第一个元素，
   而 `items` 里条目类型不同（`added` 没有 `change`、`absent` 没有 `reminder`）——
   于是 fixture 一换，就报"fixture 缺 change.\* / reminder.\*"这类**假失败**。
   改成遍历所有元素。**这是被新 fixture 抓出来的**，不是我想到的。

2. **文案"扩抓"和 issue 里的"补抓"不一致**，门禁断言 `"补抓" in text` 直接红了。
   统一成"补抓"（和数据字段 `is_backfill` 的语义对齐）。

### 提交

`server/push.py`、`server/app.py`、`server/tests/run.py`、`server/tests/demo_push.py`、
`contracts/api.md`、`contracts/CHANGELOG.md`、`contracts/fixtures/push.preview.json`、
`RUN.md`、本文件。

门禁 **149 通过 / 0 失败**；推送冒烟 **6/6**（真实事件流）；原冒烟 **5/5**；
`server/var/state.json` 一个字节没动。**commit 不 push。**

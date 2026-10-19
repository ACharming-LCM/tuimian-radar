# B 第三十三场：freshness 完整性断言（B14）—— 补一条防"刚发生过的事"的门禁

> 时间：2026-10-18 23:50 前后。基础 HEAD：`9d87953`（A22-A25）。数据 2751 条。
> 交办：加 3 条断言，防 freshness 被分批覆盖。

## 一句话

门禁 **195 → 198**，全绿。反向验证过：把 freshness 改成"2 行 / records 全 0"，
**恰好那两条**变红。

## 事故与它为什么没被发现

A 逐校分批跑时，某一批把 `data/out/freshness.json` **整个覆盖成了 2 所学校 / 0 条记录**，
而主库是 2751 条 / 29 所。**门禁当时 193 条一条都没发现** ——
因为所有断言都只看 `notices.jsonl` 和事件流，**从来没有人核对过 freshness 和主库对不对得上**。

而 `/api/freshness`（§12）和页头的"数据新鲜度"都读它。它错了之后：
用户看到的是"只有清华在更新"，而实际上我们抓了 29 所 —— **不是缺数据，是那份汇总错了**。

这条和之前几次是同一个模式：**断言只覆盖了自己熟悉的那条链路**。
数据面从"一个文件"变成"两个文件要对账"时，门禁不会自动跟着长。

## 三条不变量

| # | 断言 | 判据 |
|---|---|---|
| 1 | `freshness_学校数与主库一致` | freshness 里出现的**学校名集合** == `notices.jsonl` 的学校名集合 |
| 2 | `freshness_记录合计等于主库行数` | `records` 合计 == notices 行数 |
| 3 | `freshness_不是过期快照` | `generated_at` 不早于 notices 的 mtime 超过 **5 分钟** |

**第 3 条为什么给 5 分钟容差**：freshness 按学校分批写，而 notices/fixtures 可能在其后
几十秒被同一轮流程重写（实测差 **121 秒**）。严格"不早于"会变成每轮都红，
红久了所有人都会学会忽略它 —— 那比没有这条更糟。5 分钟仍能抓住真事故
（那次落后了整轮，差几小时）。

三条都写成"**集合/合计/时间**"的对比，不是"某个数大于 0"那种近似断言 ——
后者在 2 行 / 0 条那种事故下也可能通过。

## 反向验证（判据要求）

造一份残缺 freshness（只留 2 所、records 清 0），用 `TUIMIAN_FRESHNESS` 指过去跑整条门禁：

```
原 freshness：37 行 / records 合计 2751
残缺 freshness：2 行 / records 合计 0
门禁（残缺）：196 通过 / 2 失败
红的：freshness_学校数与主库一致、freshness_记录合计等于主库行数
```

**没碰真实的 `data/out/freshness.json`**，残缺文件在 `server/var/tmp_verify/`（gitignored）。

## 顺带：`/api/health/data` 不读 freshness —— 那次事故没让它变错

心跳读的是 `data/logs` + `stability.jsonl` + `notices.jsonl` + `events.jsonl`，
**不含 freshness**。所以用户担心的"心跳会返回错的数"这一次不成立。

A 修完 freshness 后重测：

| 接口 | 值 |
|---|---|
| `/api/health/data` | `notices 2751 / events 2759 / fresh_within_7d 27 / consecutive_ok 7` |
| `/api/freshness` | 37 行、records 合计 **2751**、`failed/stale/empty` 全 0 |

## ⚠️ 顺手发现一个标签问题（已写进契约）

`freshness.totals.schools` = **37**，但那数是**学校 + 单位**的行数，
真正的学校数是 **29**。前端如果写"覆盖 37 所学校"就会多报 8 所。

这条我没改代码（`totals` 是数据层产出，改口径要走 issue），只在契约 §12 写明。
**要 A 改的话我再开 issue**。

## 判据对照

- [x] 三条断言落地（门禁 195 → **198**）
- [x] 把 freshness 改成只有 2 条学校，门禁**红在那两条**上（验证输出见上）
- [x] 门禁 198/0

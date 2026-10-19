# B（后端）第四十场：`deadline_asc` 只排未来 + 互斥桶（B25 修正）

> 2026-10-19 16:25。用户指出 `sort=deadline_asc` 把 2010 年的排最前，
> 并订正了"facets 桶相加 == total"那条判据（是他写错了）。

## ① `deadline_asc` 之前把 16 年前的排最前

实测（改之前）：

```
GET /api/notices?limit=5&sort=deadline_asc
→ 2010-05-31 中南大学…新增硕士专业学位授权点工作方案
→ 2010-06-10 自行审核博士…
```

根因：`_sort_key_deadline_asc` 把 `found` 一视同仁按日期升序 —— **已截止的日期更小，
自然排最前**。排序没错，"用户要的排序"错了。

现在：

```
future（deadline >= 今天，日期升序）
  → [仅 include_expired=true] 已截止（日期降序，刚过期的先）
  → deadline 为 null（absent / unknown）沉底
```

实测：

```
sort=deadline_asc                          total 1727 = 36 条未来 + 1691 条 null
  first5: 2026-10-20 ×3, 2026-10-21 ×2  ✅ 都在未来、升序
  include_expired=false → 已截止 0 条 ✅
sort=deadline_asc&include_expired=true     total 2419
  边界：未来段（末尾）→ 已截止段（开头 2026-10-18，日期降序）→ null
sort=deadline_desc                         total 2419
  first5: 2026-10-18, 10-15, 10-12 …  ✅ 已截止优先
```

**`total 1727` 别误读**：按你的第 3 条（`None` 仍然排最后、只是不进主序列），
它们**还在列表里**，所以 total 含 1691 条 null；36 是"有未来截止日的"条数。
如果你希望 `deadline_asc` 的 total 就只算有截止日的（把 null 从 total 里也去掉），
那是另一个产品决定，说一声我改。

**`deadline_desc` 的语义**：我定义成 `deadline_asc` 的**镜像**
（已截止→未来→null），因为你原话是"想看已过期的用 `deadline_desc`"。
如果按字面"日期倒序"，过期记录会沉到最底下，等于看不到 —— 那不是你要的。

## ② facets：累计 vs 互斥

你订正得对：`within_7` 包含 `within_3` 包含 `within_1`，累计桶相加本来就是 2447，
不是 total。**数据没动**，改的是契约和前端提示：

- `deadline_buckets`：**累计**。`within_N.count == ?deadline_within=N` 的 total
  （所以它适合做下拉的「7 天内 (18)」）；
- `deadline_exclusive_buckets`（新增）：**互斥**，
  `within_1 / within_2_3 / within_4_7 / within_8_30 / later / expired / unknown / absent`。

实测互斥桶：

```
within_1 3 / within_2_3 4 / within_4_7 11 / within_8_30 6 /
later 12 / expired 692 / unknown 525 / absent 1166
相加 = 2419 == total ✅（门禁订着）
```

两套桶从**同一个 `left = 剩余天数`** 算，不会各说各的。
契约里写明"**不要把 `within_*` 相加**"，并给前端一句提醒。

## ③ 门禁

- `列表_deadline_asc默认只排未来且升序`（前 5 条未来、递增、已截止 0 条、null 沉底）；
- `列表_deadline_asc_include_expired把已截止放最后`（边界：未来→已截止；已截止段降序）；
- `列表_deadline_desc先给已过期的`（第一条已截止、过期段降序、未来段在后）；
- `facets_截止日分桶和筛选对得上`：扩成**同时验两套桶** ——
  累计桶和筛选 total 一一对应；互斥桶**独立从 RAW 复算**、相加 == total；
- 「每个查询参数都改变结果」补了 `deadline_desc` 和 `include_expired` 两个探针。

## ④ 还没做 / 等拍板

- `deadline_asc` 的 `total` 要不要把 null 也排除（见上）；
- `obsolete` 的写法：现在已截止在 desc 里按"刚过期的先"；如果要"按过期时长分档"
  （1 周内过期 / 1 个月内 / 更久），那是再来一档的事。

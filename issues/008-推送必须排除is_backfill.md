# 008 推送必须排除 `is_backfill`（实测：20 条推送里 18 条是 2018~2026 的旧公告）

- 提出：A（数据）16:35
- 状态：**open，阻塞演示**
- 影响：`server/push.py`（B 的目录）
- 相关：`contracts/events.schema.json` 的 `is_backfill`、`contracts/CHANGELOG.md`

## 现象（A 独立验收 B 的推送链时实测）

用付费身份 + 一个正常画像（浙大/物理/意向北大复旦浙大/方向物理材料化学），
`GET /api/push/preview?limit=50`：

```
tally: events=90  matched=46  sent=20  suppressed=26  daily_limit=20
推送 20 条，其中 is_backfill=true 的 18 条
发布日范围：2018-09-27 ~ 2026-10-08
早于 30 天前的：18 条
前 10 条发布日：2018-09-27, 2019-04-26, 2022-09-16, 2023-11-06,
                2024-10-07, 2026-02-26, 2026-03-06, 2026-03-17, ...
```

而且每条 `headline` 写的是 **「【新公告】…」**：

```
【新公告】北京大学：北京大学法学院2026年硕士招生拟录取公示      ← 2026-04-03
【新公告】浙江大学：…推荐 2027届优秀应届本科毕业生免试攻读研究生工作细则  ← 2026-08-29
```

**8 年前的公告被称为「新公告」。**

## 为什么会这样（不是你的错）

- 你的提交 `a3dc3be` 是 **15:39:52**
- 我给事件加 `is_backfill` 是 **16:07:21**（`5723168`）

**你是按当时的事件形状实现的，而我后加了一个字段。时序问题，不是实现错误。**

## 为什么必须修（这条不修，演示视频会翻车）

`data/out/events.jsonl` 现在 90 条里 **85 条 `is_backfill=true`**。
原因是这一轮把 `details_per_board` 从 3 放到 6，多抓出 90 条 ——
它们是**我们之前没抓到的旧文章**，但在 diff 眼里是 `added`。

不排除的话：
- 评委看到「【新公告】2018 年…」，会直接怀疑数据的可信度
- 而"可信"正是这个项目的主线定位（三态 + 回链原文）

## 建议改法（一行即可）

`server/push.py` 在 ② 去重/过期 那一段（约 L353）之前加一步：

```python
# ---- ②' 排除补抓：'我们多抓到了' ≠ '学校今天新发了' ----
for item in matched:
    if item.get("is_backfill"):
        suppressed.append(_suppressed(item, "backfill"))
    else:
        ... 原有 fresh/suppressed 逻辑
```

`_SUPPRESSED_TEXT` 加一条：
```python
"backfill": "这条是我们补抓到的历史公告，不是今天新发的（不计入推送）",
```

**注意两点：**
1. **`deadline_changed` / `attachment_added` 永远不该被排除** ——
   数据层对这两种类型**不会**标 `is_backfill`（再老的公告改了截止日也该报）。
   所以按 `item.is_backfill` 判断就够了，不用看 `type`。
2. **事件的 `is_backfill` 要读进来**。`server/events.py` 的 `REQUIRED_EVENT_FIELDS`
   不用动（它是可选的），但 `build_item` 要把它带出来，否则 `push.py` 看不到。
   建议加一条门禁：**`is_backfill=true` 的事件不许出现在 `items` 里**，
   并且造数据验（别赌磁盘上恰好有补抓事件）。

## 验收标准

```
GET /api/push/preview?limit=50（付费身份，画像同上）
→ items 里没有任何一条的 publish_date 早于 30 天前
→ suppressed 里出现 reason="backfill"
→ 门禁新增一条：造一个 is_backfill=true 的事件，断言它不进 items
```

## 附：两处次要问题（不急，但建议记一下）

1. **同一批里同一条公告可能出现两次。** 实测第 4 次 POST 返回
   `['ue00ab131-2bc4', 'ud1ddf924-1515', 'ud1ddf924-1515']` ——
   同一条公告的两个不同事件（`added` + `deadline_changed`）各自通过去重，于是并列出现。
   语义上讲得通，但界面上像"重复推送"。建议同批内按 `notice_id` 只留一条（留哪个由你定）。
2. **`POST /api/subscriptions` 的 user 从 body 取，其他端点从 query 取。**
   我自己验收时就踩了：`?user=xxx` + body 只给 `plan_id`/`code` → 开通到了 `u_demo`，
   而预览查 `u_xxx` 显示 `plan.active=false`，看起来像"开通了但没生效"。
   契约里写清了，但**两个端点的取值位置不一致**本身是个坑。建议统一，或在契约里加粗警告。

## 附：A 验收时自己踩的三个坑（说明文档其实是够用的）

我按自己的理解调用，错了三次，都是**我去读了契约才对的**：
1. 画像字段用了 `undergrad_school`/`target_schools` → 实际是 `school`/`preferred_schools`
2. 开通码参数用了 `activation_code` → 实际是 `code`
3. 订阅的 user 放在 query → 实际在 body

**这不是文档的问题，是我没先读。** 记在这里是为了让下一个人少走一遍。

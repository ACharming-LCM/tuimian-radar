## 15:40 A（数据）— events schema 已就位（阻塞 B/C 的那一环）

> **给 B 和 C：schema 是第一小时交付物，现在可以开工了。**
> 契约：`contracts/events.schema.json`
> 演示样例：`contracts/fixtures/events.sample.json`（5 条，覆盖三种截止日态 + 两类变更）
> 真实事件流：`data/out/events.jsonl`（append-only，今天**是空的**，见 §三）

---

## 一、事件流和 notices.jsonl 不是一回事

| | 回答的问题 |
|---|---|
| `notices.jsonl` | 现在库里有什么 |
| `events.jsonl` | **历史上发生过哪些值得打扰用户的事** |

推送要的是后者。事件是**追加式**的，永不覆盖 —— 昨天的"新公告"事件今天还在。

## 二、三种事件 / 四个必须知道的字段

```json
{"event_id":"adde_3f2a…", "type":"added|deadline_changed|attachment_added",
 "notice_id":"…", "at":"2026-10-17T15:40:00", "detected_on":"2026-10-17",
 "url":"https://…", "school":"…", "unit":"…", "title":"…", "publish_date":"…",
 "deadline":"2026-10-21"|null,
 "field_state":{"deadline":"found|absent|unknown","majors":…,"attachments":…,"body":…},
 "relevance":0|1|2, "matched":["硕士","推免","材料"], "change":{…}}
```

**四个不许省的**：

1. **`event_id` 稳定去重键，不含生成时间。** 后端按它去重就能保证「同一条变更不重复推送」。
   含了生成时间的话每天重跑都变新事件，去重立刻失效。
2. **`field_state` 必带。** 缺了它，后端只能看见 `deadline: null`，
   然后**一定会**把它说成"没有截止日" —— 那就是硬规矩第 1 条禁止的事。
3. **`url` 必带。** 缺 url 的事件 `make_events.py` 直接跳过并记原因，不会静默发出去。
4. **`relevance` 只许用于排序**，不许用来决定"不推"。

**`reparsed` 绝不产生事件。** 那是我们改了抽取代码，公告一个字没变。

## 三、今天的真实事件流是空的（这是诚实的结果，不是坏了）

`changes.json` 现在是 `added=0 / changed=0 / reparsed=5 / unchanged=191` ——
**今天没有新公告**（数据是今天早些时候抓的，所以增量是 0）。

所以：
- `data/out/events.jsonl` **还没创建**（`make_events.py` 没有新事件就不写文件）
- **演示必须用 `contracts/fixtures/events.sample.json`**

样例的 5 条全是从真实 `notices.jsonl` 里挑记录生成的，字段值原样搬，
只有 `type`/`at` 是演示值，`event_id` 带 `sample-` 指纹以便和真事件区分：

| type | deadline | 该有的措辞 |
|---|---|---|
| added | 2026-10-21（found） | 显示日期 |
| added | null（**absent**） | 「该公告未公布截止日」 |
| added | null（**unknown**） | 「**未能读取截止日，请点原文核对**」+ 原文链接 |
| deadline_changed | 2026-10-21 | 说清"从什么变成什么" |
| attachment_added | 2026-10-21 | 说清"补了什么附件" |

**⛔ `unknown` 不许写「暂无」/「——」/省略不显示。** 不显示最危险：
用户会以为没有截止日，于是**不去看原文**。

## 四、`data/out/runs/<日期>.json` 也补上了

形状照 B 在 `api.md` §12 里写的要求（键名没自创）：
`started_at / finished_at / records / steps[{step,seconds,exit}] / failed_steps`。
**B 的 `/api/admin/crawl` 的 `steps` 从此可以不再是 `null`。**
文件在**下一次真跑 `run_daily.py` 时**才出现（今天还没跑）。

## 五、我这边剩下的

| 事 | 状态 |
|---|---|
| events schema | ✅ 已交付 |
| 事件文件产出 | ✅ 工具+12 道测试，今天产出 0 条（真的没有新公告） |
| `runs/<日期>.json` | ✅ 代码就位，等第一次真跑 |
| 演示样例事件 | ✅ `contracts/fixtures/events.sample.json` |
| **日更启动** | ⏸ **等用户点头**（10 分钟、会请求 37 个站点） |
| fixtures 重出 | ✅ 已重出（relevance 收紧后） |

## 六、契约变更

`contracts/CHANGELOG.md` 已记：新增 `events.schema.json` + `events.sample.json`，
**纯新增，不动任何已有契约**。

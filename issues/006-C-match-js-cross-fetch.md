# 006 给 C：match 响应已经自带 `extract.confidence`，`match.js` 那次补拉可以删了

- **提出**：B  2026-10-17 02:10
- **状态**：open（需 C 改一行代码，不急但会掉数据）
- **阻塞谁**：不影响能不能跑；影响"疑似"标记的**覆盖率**
- **相关文件**：`web/assets/match.js`（C 的）、`contracts/api.md` 第 2 节、`contracts/fixtures/match.response.json`

## 背景

C 的 `match.js` 现在这样拿置信度（第 21-24、40-50、178 行）：

```js
const [match, list] = await Promise.all([
  loadFixture('match.response.json'),
  loadFixture('notices.list.json').catch(() => null)   // 只是用来补 confidence
]);
...
const conf = state.conf.has(it.id) ? state.conf.get(it.id) : null;
const suspect = conf !== null && conf < 0.5;
```

因为**旧**的 `match.response.json` 里没有 `extract`，C 只能跨请求按 id 去列表接口里找。

A 在 `contracts/CHANGELOG.md` 里记了实测代价：
**这样只关联上 24/30 条** —— 列表接口分页/筛选会让 id 对不齐，剩 6 条前端不敢标"疑似"。
所以 A 在 `api.md` 第 2 节把三个字段加进 match 响应，并立了原则：
**凡是前端渲染要用的字段，必须在同一个响应里给全。**

## B 已按新契约实现并验证

`POST /api/match` 的每条 item 现在都带：

```json
{ "id": "...", "score": 0.9205, "raw_score": 0.97, "bucket": "需人工确认",
  "has_attachments": true, "attachments_count": 1,
  "extract": { "method": "rule", "version": "v3", "confidence": 0.83 },
  "reasons": [...], "warnings": [...] }
```

**实测**：`/api/match?limit=30` 与 `contracts/fixtures/match.response.json`
逐字段比对，**30/30 条零差异**（顶层键 `profile` / `stats` / `items` 也一致）。

## 建议 C 这样改（两处，五分钟）

```js
// ① 主数据源直接就有 confidence，不再需要第二个 fixture
const match = await loadFixture('match.response.json');

// ② 优先用 item 自带的，列表接口只作为兜底（万一后端没给）
const conf = (it.extract && typeof it.extract.confidence === 'number')
  ? it.extract.confidence
  : (state.conf.has(it.id) ? state.conf.get(it.id) : null);
```

改完之后：
- `notices.list.json` 那次请求可以整个删掉，`buildConfIndex` 也就不用维护了；
- 30 条**全部**有 confidence，`suspect` 覆盖从 24/30 变成 30/30；
- `has_attachments` / `attachments_count` 也已经在同一条 item 里，附件角标不用再找地方。

## B 这边不会做的事

- **不动 `web/` 下任何文件** —— 那是 C 的目录。这里只登记问题和建议改法。
- 也不打算在 `/api/match` 里再返回一份"列表项"给 C 补字段：那就是把
  "同一个响应给全"这条原则绕过去了，下次还会踩。

## 补充：一个已经失效的注释

`match.js` 第 45-47 行那段注释还在讲"同一个 id 可能有多条记录（http/https 两个 URL）"。
**那个数据问题 A 已经修掉了**（`crawl.py` 按 `canonical_url` 去重，实测 78 行 / 78 个唯一 id /
0 组重复）。逻辑留着当兜底没问题，但注释描述的现状已经不准了，顺手改一下更省后人时间。

- **需要**：C 改上面两处；顺便确认一下 `match.js` 现在是指向 fixture 还是真后端。
- **结论**：（待填）

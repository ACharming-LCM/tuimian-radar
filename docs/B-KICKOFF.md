# B 启动包（后端）

> 你不需要读完整套文档。**读这一页 + `contracts/api.md` 就够开工了。**
> 全部背景在 `docs/COLLAB.md`，遇到冲突时以它为准。

## 你的目标（一句话）

把已经抓好的数据变成 **HTTP 接口**，让 C 的前端能读，并实现"按背景匹配 + 套磁信 + 付费墙"。

## 你能改哪里

**只有 `server/`。** 其他目录一律不碰：
- `collector/` 是 A 的（数据管道，已跑通，**不要重新抓数据**）
- `web/` 是 C 的
- `contracts/` 改动要记 `contracts/CHANGELOG.md`，并在 `journal/` 里 @C

**数据已经躺在仓库里了**：`data/out/notices.jsonl`，74 条、8 所学校，
每行一条，字段定义见 `contracts/notice.schema.json`。**clone 下来就有，不用跑爬虫。**

## 第一小时（照这个顺序，不要跳）

```bash
git clone <仓库地址> tuimian && cd tuimian
git config user.name "B-后端"        # 每人设自己的，不要共用
git config user.email "b@tuimian.local"

python -m venv .venv && .venv\Scripts\activate
pip install fastapi uvicorn
```

1. **读 `contracts/api.md`（草案，你是定稿人）** 和 `contracts/fixtures/notices.list.json`。
   契约里如果你觉得哪条不合理，现在就改 —— **一旦 C 开始写，改一次成本是三个人的一天。**
2. 建 `server/app.py`，让 `GET /api/notices` 返回**和 fixtures 完全一样形状**的 JSON。
3. 验收（必须真跑）：
   ```bash
   uvicorn server.app:app --reload --port 8000
   curl "http://127.0.0.1:8000/api/notices?page=1&size=5"
   ```
   → `total` 应该是 **74**，`items` 是 5 条，字段齐全。

## 然后按这个顺序做（每一步都可交付）

| 顺序 | 做什么 | 验收 |
|---|---|---|
| 1 | `GET /api/notices` 带筛选（school/unit/apply_mode/q）和分页 | 换参数结果真的变 |
| 2 | **把 `web/` 作为静态目录挂到同一个服务下** | 浏览器能打开 C 的页面，**且没有跨域问题** |
| 3 | `GET /api/notices/{id}` 返回完整记录 | 含 `body_text` 和 `attachments` |
| 4 | `POST /api/match` | 照 `docs/matching-spec.md`；**参考实现直接抄 `collector/tools/match_preview.py`（纯标准库，我写好了）** |
| 5 | `POST /api/letter` 套磁信 | 必须返回 `citations`，每句话能指回公告原文 |
| 6 | 论坛 API + 审核队列 | 后台实名、前台匿名、举报入口 |
| 7 | `GET /api/me` + `POST /api/subscribe` | **D4 用模拟支付**，不接真实通道 |
| 8 | **`GET /api/faculty`（导师）** | 读 `data/out/faculty.jsonl`；这是"专业契合"唯一的支撑数据 |

**第 2 步别跳过。** 你顺手把 `web/` 挂成静态目录，C 就永远不用碰 CORS，
这一条能省掉你们俩加起来半天的扯皮。

## 四个必须知道的坑（A 已经踩过，别再踩）

1. **`deadline` 只有 26% 的记录抽到了。** 接口必须允许它是 `null`，
   并且**把 `warnings` 数组透传给前端** —— 前端要把"未抽到截止日期，请点开原文确认"显示出来。
   **绝不能填个假日期糊过去，那是会害人的。**
2. **`body_text` 里可能有 `【附件正文：文件名】` 分隔标记**（附件 PDF 的内容被拼进来了）。
   前端要分段渲染就按这个标记切。
3. **`extract.confidence` 每个记录都有**，低于 0.5 的要在界面上标"疑似"。
4. 不要自己重新抓数据。要新字段找 A，走 `issues/`，不要偷偷往 JSONL 里塞。

## 开工前把这段贴给你自己的 AI

```
你是 tuimian 项目的后端 agent。项目根：<你 clone 的绝对路径>
- 你只能修改 server/ 下的文件。collector/ 是别人的，web/ 是别人的。
- 数据源：data/out/notices.jsonl（已存在，74 条，不要重新抓）
- 数据契约：contracts/notice.schema.json（只读）
- 接口契约：contracts/api.md（你是定稿人，改它必须记 contracts/CHANGELOG.md）
- 契约样例：contracts/fixtures/*.json —— 你的返回必须和这些形状一致
- 匹配规则：docs/matching-spec.md；参考实现 collector/tools/match_preview.py（可直接抄）
- 开工前先读：journal/ 最新 3 条、issues/ 未关闭项
- 收工必须写：journal/<日期>-B.md 追加一段（做了/产出/卡住/需要/契约有无变更）
- 禁止：git add -A、格式化别人的文件、改 contracts/ 而不记 CHANGELOG
- 冒烟测试：uvicorn server.app:app --port 8000 然后 curl /api/notices
```

## 你不需要做的

- 不用管数据怎么抓、怎么解析
- 不用管页面长什么样
- 不用做用户注册登录（D4 演示用假登录态就够）

## 明确不做的（别自己加，也别默默加）

一张**显式的"不做"清单**比"没写"有用：没写的东西三个人会各自理解成不同的事。

| 不做 | 为什么 | 谁定的 |
|---|---|---|
| **用 LLM 补 `deadline`** | 接口如实返回 `deadline: null` + 提醒"点开原文确认"，是**诚实**的做法；让 LLM 去猜日期有**编造**风险。真要做只能："抽取不生成 + 回链原文片段 + `extract.method='llm'` + 前端标疑似"，而且要**先定模型和 key 谁出** | B 提出，A 同意（见 issues/005） |
| `POST /api/letter`（套磁信） | 同样是 LLM 依赖，同一笔账。等 LLM 那件事定了再一起做 | A |
| 论坛、付费墙 | D3 / D4 的事 | A |
| 在前端做 URL 去重来兜底 | 重复记录是**数据源**的问题，已在 `crawl.py` 按归一化 url 修掉。接口层再遮一次会让问题更难发现 | A |
| 自己重新抓数据、或往 `notices.jsonl` 里加字段 | 会绕过契约。要新字段提 `issues/` | A |

## 你的验收标准就是 fixtures

`contracts/fixtures/*.json` 不是"参考"，**是验收标准**：
C 已经用 `match.response.json` 写好了匹配页，跑通 18 项断言。

**你的 `/api/match` 返回必须和 `match.response.json` 形状一致。**
做不到就是接口不对，不是前端不对 —— 这样你们不用互相说服。

其中 `match` 的 item 必须带 `extract.confidence` / `has_attachments` / `attachments_count`
（C 原来为了拿 confidence 又拉了一次列表接口，30 条只关联上 24 条）。

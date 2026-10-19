# 交接：B（后端 / `server/` + `contracts/`）

> **写于 2026-10-19 02:05，HEAD `734134f`，数据 2751 条时代。**
> 这份是**覆盖式重写**——本文之前的版本记的是 470/1119 条时代，数字全废了。
> 需要考古就去 `git log --follow docs/HANDOFF-B.md`。

---

## 0. 三十秒版本

门禁 **202/0 全绿**，两个冒烟 6/6。**在做的只有一件事：B18 等一个决定**（见 §5）。
其余三条 P0（B15 招生类硬门槛 / B16 免费 10 条 / B17 english 500）已经推上去了。

```powershell
$env:PYTHONIOENCODING="utf-8"; Set-Location D:\tuimian
D:\python.3.10.11\python.exe server\tests\run.py            # 门禁 202 条
D:\python.3.10.11\python.exe server\tests\demo_push.py      # 推送冒烟 6 步
D:\python.3.10.11\python.exe server\tests\demo_rehearsal.py # 演示冒烟 6 步
```

**`PYTHONIOENCODING=utf-8` 不设就会在中文输出上崩**，每次都设。

---

## 1. 现在的真实数字（01:20 实测）

| | 数 |
|---|---|
| 公告 | **2751 条 / 29 所学校 / 37 个抓取单元** |
| 质量 | ok **2323** / thin **260** / junk **168** |
| 主题 | apply **1290** / non_apply **811** / other **650** |
| 事件 | **2759 条**，其中 `is_backfill=2573`（**93% 是补抓**） |
| 导师 | 557 |
| 门禁 | **202 / 0** |
| 首屏 | 20 条 = 一级 17 + 二级 3，**全部 `topic=apply`** |
| 免费层 | **10 条**（`PUSH_FREE_LIMIT`）；每日上限仍 **20**（`PUSH_DAILY_LIMIT`，不降） |
| 性能 | push/preview **33 KB / 100 ms**；library 39 ms；health/data 7 ms |

---

## 2. 边界（别越界，会跟另外两个 agent 打架）

| 目录 | 谁的 | 我能动吗 |
|---|---|---|
| `server/` | **我（B）** | ✅ |
| `contracts/` | **我（B）** | ✅ |
| `journal/` `issues/` `status/B.md` `docs/HANDOFF-B.md` | 我 | ✅ |
| `collector/` | A（数据层） | ❌ **绝对不动** |
| `web/` | C（前端） | ❌ **绝对不动** |
| `data/out/` | A 产出 | ❌ 只读，**从不写** |

例外只有一个：`collector/tools/make_fixtures.py` 里的字段表（加字段时两边都要动）。

**踩过的坑**：我 `git checkout -- server/match.py` 时把 B17 的修复一起回退了，
因为那个文件同时有 B17/B18 的改动。**回退前先看 `git diff` 里有什么。**

---

## 3. 已经定死、别改的决定

1. **`unknown` ≠ `absent`。** 任何排序/统计/聚合都不许把"不知道"算成"没有"。三态到处都要。
2. **已截止的沉底、不压掉**（`urgency_weight=0` 只参与排序）。
3. **首屏只放招生类**（B15）：`topic == "apply"` 是硬门槛，tier1/tier2 都要满足。
   **宁可空，不许凑**——空态给 `no_apply_today` + "最近一条是几号的"。
4. **`tier` 排在分数之前**（结构保证，不是调参）：一级永不被二级插队。
5. **推送上限 20 不降。** "为了凑 80% 把分母改小是掰尺子"（用户原话）。
6. **接口层不重算数据层字段。** `relevance` / `is_fresh` / `record_quality` 一律透传。
   这条栽过四次。
7. **`suppressed` 只给样例**（每种原因前 3 条），全量走 `/api/push/suppressed` 分页。
8. **空态只给一个主因**（`empty_reason`），其余进 `empty_reasons` 数组；
   优先级 = 用户能解决的优先（`no_profile` 最高）。
9. **性能是门禁的一部分**：体积/耗时超标就红（`perf_*` 两条）。

---

## 4. 这轮做完的（B5–B17 摘要）

| 档 | 内容 | 提交 |
|---|---|---|
| B5 | `test_data_health.py` 15 条数据健康断言 | 多 |
| B6 | `GET /api/health/data` 心跳（`ok: null` 不许编） | `46ebcf8` |
| B7 | 首屏 `tier` 分级 + 漏斗 | `c6bf7cb` |
| B8 | 天津/上交重验 | 同上 |
| B9 | `admission_year` 收口 | 同上 |
| B11 | `docs/TODAY-PUSH-SAMPLE.md`（**已过期，见 §6**） | `54e6863` |
| B12 | `suppressed` 瘦身：535 KB → 33 KB | `7f431bc` |
| B13 | `store.get()` 从 O(n²) 改成 id 索引（library 608→39 ms） | `7f431bc` |
| B14 | 3 条 freshness 完整性断言 | `2f0fe97` |
| B15 | `topic == "apply"` 硬门槛 + `no_apply_today` | `734134f` |
| B16 | 免费层 3 → 10 | `734134f` |
| B17 | `english` 字符串 500 修复 + 契约示例门禁 | `734134f` |

---

## 5. ⚠️ 唯一的进行项：B18 等决定

**问题**：同一个画像，`/api/match` 和 `/api/push/preview` 给不同的前 5 条。

**卡在哪**：`/api/match` 的打分公式被 **A 的参考实现**钉死——门禁有一条
`match_与参考实现逐字段一致`，直接跑 `collector/tools/match_preview.py`，
逐字段比 `score / raw_score / reasons / warnings`。我单方面统一 ⇒ 红 7 条。
而 `collector/` 是 A 的目录，我改不了。

**已经做到的**（不依赖拍板，已落地）：
- 推送补上 `apply_mode` 硬过滤（实测 u_num_demo 只要**推免**，却收到中山「少数民族骨干」
  ——那是**统考**公告，真 bug）；
- 两条路径前 5 条**重合 4/5**（改前 2/5），残差是"事件流 vs 整库"。

**三个选项**（详见 `issues/012`）：

| 方案 | 做什么 | 代价 |
|---|---|---|
| **A（我推荐）** | `score_notice` 改成调 `push.score_dims`，**A 同步改参考实现**（约 20 行） | 要 A 动手；能做到 5/5 |
| B | 只统一硬过滤 + 字段（`why`/`tier`/`topic`），不动分数 | 今天就能收口，但判据降级 |
| C | match 内部调 push 的排序 | 不现实（排的对象不同） |

**下一步动作**：等用户拍 A 还是 B。选 A 就开 issue 给 A，选 B 就补 `why`/`tier` 三字段。

---

## 6. 别人欠的 / 我欠的

| 谁 | 事 | 状态 |
|---|---|---|
| **C** | 付费墙文案「免费看前 3 条」→「解锁订阅提醒 + 资料库」 | 后端已改 `free_limit=10`，**文案在 `web/`，我碰不了** |
| **C** | `docs/TODAY-PUSH-SAMPLE.md` **已过期**（首屏 20 条只和它重合 3/20）。已加过期警告，但**要重出一份** | 我可以重出，约 20 分钟 |
| **A** | 天津 3 条「天津大学研究生招生网」标题是站点名、正文 279~291 字成句 —— 在 5% 阈值内，但属"好记录被扔" | 不急，已写进 journal |
| — | `freshness.totals.schools=37` 是"学校+单位"行数，真学校数 **29** | 契约已注明；要 A 改口径就开 issue |

---

## 7. 环境（照抄，别自己找）

| 项 | 值 |
|---|---|
| Python | `D:\python.3.10.11\python.exe`（**不是** PATH 里的） |
| 门禁 | `server\tests\run.py`，零依赖，`@case` 装饰器自动注册 |
| 数据 | `data/out/notices.jsonl` / `events.jsonl` / `freshness.json` / `stability.jsonl` |
| 日志 | `data/logs/fast.log` / `full.log`（**只在 B6 心跳里读**） |
| 权限令牌 | `X-Admin-Token: <环境变量 TUIMIAN_ADMIN_TOKEN，未设则启动时随机生成>`（审核类接口） |
| 单 worker | 状态在进程内存里，**必须单 worker** |

**提交纪律**：`git commit -F <文件>`（PowerShell 没有 heredoc；`^` 是 cmd 转义符）。
**先 `git fetch` 再看 `git log HEAD..origin/main`**，A/C/统筹 随时在推。
**`git add` 指名道姓**，别 `-A`（会把别人的改动卷进来——已经被卷过一次）。

---

## 8. 我栽过的坑（按痛感排序）

1. **中文串里嵌 ASCII 双引号** → `SyntaxError: Perhaps you forgot a comma?`
   这轮栽了 **5 次**。写 Python 里的中文说明一律用「」或“”，别用 `"`。
2. **PowerShell `>` 重定向写文件是 UTF-16**，Python 再读会 `UnicodeDecodeError`。
   → 让脚本自己 `open(..., "w", encoding="utf-8")`。
3. **`Out-File` 加 BOM**，`json.loads` 崩。→ 同上。
4. **在这里写多行 Python（`-c @"..."`）极易被 shell 吃掉引号**。
   → 稍复杂就 `write` 一个 `.py` 到 `server/var/tmp_verify/` 再跑。
5. **断言口径写错会反向伤害正确实现**：`junk 里正文完好` 原判据把
   「招生目录」索引页（2.7 万字）算成"真公告被冤枉"，比例顶到 9.4%，
   **等于逼数据层放宽正确的索引页判据**。判据要能分清"索引页"和"真公告"。
6. **用例造的数据过期了，却在断言"没匹配"** → 报 `stale_events`。
   凡是造事件的用例，**日期用 `date.today()`**，别写死。
7. **`assemble` 只要 80 ms，接口却 500 ms** —— 瓶颈在"每条事件 `store.get()` 全表扫描"
   （2759 × 2751 ≈ 760 万次比较）。加索引后 100 ms。**先量再改。**
8. **`suppressed` 全量返回 = 523 KB**，占响应 98%。可解释性要，但不该用全量买。

---

## 9. 验证方式（比"门禁绿"强的那些）

- **反向验证**：改完断言要做一次"造坏数据 → 必须红"。
  这轮做过三次：性能（去掉索引 → 红）、freshness（2 行 / 0 条 → 红）、
  junk（索引页不该被判"冤枉"）。
- **`--check`**：`make_push_fixture.py --check`、`make_feed_fixtures.py --check`
  用来确认 fixture 和接口没漂。
- **fixture 是契约**：默认响应形状必须逐键等于 `contracts/fixtures/*`，
  列表是逐字段逐位置严格比。

---

## 10. 一句话给下一个人

**这个项目的门禁是资产，不是装饰。** 202 条里有一半是"这次别再犯"的具体事故
（上交换壳、一条公告没事件、索引页混进首屏、freshness 被分批覆盖、535 KB 白屏）。
改代码前先读 `journal/2026-10-19-B-第三十四场-P0四条.md` 和 `issues/012`，
改完跑门禁 + 两个冒烟，**别为了让某条变绿去改判据**。

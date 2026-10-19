# 关键页面截图清单

> 2026-10-17 智能体 C；**2026-10-18 01:20 按 1119 条重写 §0 与三态那两张**。给 PDF 材料、PPT 和演示视频用。
>
> **每一张都标了「什么时候截的」和「这一张要证明什么」。**
> 没有"要证明什么"的截图不该进材料——评委看的是证据，不是页面有多长。
>
> 数据基准：**2751 条公告（默认展示 2581）/ 2759 条事件 / 557 位导师**（取数时刻 **2026-10-18 20:14**，`HEAD=fd6764f`，见 `docs/DATA-NUMBERS.md`）。

---

## 0. 截图前必须先做的事（不做就会截出错的东西）

**现在只需要一个服务**：8001（真接口 + 静态页同源）。

```powershell
Set-Location D:\tuimian          # ⚠️ cwd 必须是这个目录，否则 web/ 读的是别的地方
D:\python.3.10.11\python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8001 --log-level warning
```

> ⚠️ **5173 那条路已经废了，别再用。** 这一版页面**只走真接口**：
> `pushes.js` 的 `PREFER_REAL_EVENTS = true`，服务端读 `data/out/events.jsonl`。
> 上一版那份「起个 http.server 从仓库根、用 `/web/pushes.html` 读 fixtures」的说明已经作废 ——
> 8001 也不挂 `contracts/`（实测 `/contracts/fixtures/push.preview.json` → 404）。

### 0.1 三个演示身份（`server/var/state.json` 里现存的）

| 身份 | 画像 | 订阅 | 用途 |
|---|---|---|---|
| `u_num_demo` | ✅ | ✅ 月度 | **主演示 + 所有截图默认用它** |
| `u_c_demo` | ✅ | ❌ 免费 | 付费墙那两张 |
| `u_demo` | ❌ | ❌ | 只当空态/兜底，**别用来截图**（昵称是"示例同学"，没画像） |

实测（2026-10-18 02:20，事件补全后）：`u_num_demo` → **544 命中** / 20 条推送；`u_c_demo` → 3 条 + 墙。

**8001 上要先用一个身份填画像**，否则接口返回 `empty_reason=no_profile`：

```powershell
# 填一份（只存在 server/var/state.json，不发给任何第三方）
$b = @{ school='浙江大学'; majors=@('物理学')
        preferred_schools=@('浙江大学','中国科学技术大学','中山大学')
        interest_keywords=@('凝聚态物理','材料') } | ConvertTo-Json
Invoke-RestMethod 'http://127.0.0.1:8001/api/profile?user=u_c_demo' -Method Put `
  -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($b))

# 确认
Invoke-RestMethod 'http://127.0.0.1:8001/api/push/preview?user=u_c_demo'
```

> 用 `u_c_demo`（免费）这种一次性身份截图，**主演示身份 `u_num_demo` 别拿来反复试**。
>
> **订阅怎么开通**：先 `PUT /api/profile`，再
> `POST /api/subscriptions?user=<身份>`，body `{"code":"TUIMIAN-DEMO-2026","user_id":"<身份>"}`。
> ⚠️ **body 里的 `user_id` 必须写** —— 实测只给 query 会被忽略、开到别人头上（与契约 §16 不符，待 B 修）。

**每条截图前都要确认页脚**：写「真接口」还是「本地样例」。
**页脚说什么，材料里就得说什么。** 拿样例截图说成实时数据，是一句话就能被戳穿的事。

> ✅ **主画面：真数据、20 条、无横幅。** B 的 `c9e94c6` 修了 `is_backfill`；`pushes.js` 的
> `PREFER_REAL_EVENTS` 是 `true`。**2026-10-18 02:20 重测**：
> `source.kind=events`、`push-card` **20 张**、`.sample-banner` **0 个**、375 与 1440 均无横向溢出。
>
> ⚠️ **三态分布每补一次数据就变一次**（这是这两轮反复踩的地方）：
>
> | | 470 那版 | 1119 且事件 476 时 | **这一版（事件 1133）** |
> |---|---|---|---|
> | 20 条推送里的三态 | found 9 / absent 8 / unknown 3 | found 19 / absent 1 / unknown 0 | **found 12 / absent 7 / unknown 1** |
> | 带 T-7/T-3 提醒 | 6 条 | 10 条 | **13 条** |
> | 命中 / 未命中 | 235 / 241 | 235 / 241 | **544 / 587** |
>
> 也就是说：**「还剩 N 天」和 T-7 提醒现在真数据就能拍**（13 条），
> **`unknown` 现在有 1 条**（B2 换排序后上来了），但位置会变，所以三态同屏仍推荐去公告列表页拍。
>
> 🔧 **`?source=sample` 已经不能用来补拍三态了。** 实测：样例 5 条事件命中 3 条，
> **被「同一条公告合并成一条」（`suppressed.reason=merged`）压成 1 张卡**，
> 且横幅照常挂上。所以那条「样例里三条恰好各占一态」的旧说法**不成立了**。
> **要拍三态齐全，去公告列表页（`notices.html`），见下表第 3 张。**

---

## 1. 必进 PDF 的五张

| # | 文件 | 怎么截 | **要证明什么** |
|---|---|---|---|
| 1 | `pushes-desktop-sub.png` | `http://127.0.0.1:8001/pushes.html?user=u_num_demo`，1440×900 首屏（**真接口、20 条、无横幅**） | **推送长什么样**——锁屏通知的样子，学校/标题/截止日一眼读完 |
| 2 | `pushes-desktop-free.png` | `http://127.0.0.1:8001/pushes.html?user=u_c_demo`，**整页**（真接口 3 条 + 免费墙） | **付费墙只挡推送和匹配，不挡原文**：图上同时有「还有 N 条推送」和「看原文 ↗」（N 现看，别写死：`tally.matched - tally.sent` = 544 − 20） |
| 3 | **`notices-3states.png`** | `http://127.0.0.1:8001/notices.html`，1440×2200（**首屏就有三种**，不用滚） | **三态措辞**。实测首屏前 7 张卡里三种齐全：`该公告未公布截止日`（第 1 张）、`⚠ 未能读取截止日，请点原文核对 看原文 ↗`（第 2、5、6 张）、`截止 2026-10-xx`（第 7 张起）。整页口径 **found 30 / absent 13 / unknown 7**（20:14 实测，随数据变），`unknown` 全部带链接。**这一张是全场最能说明"我们不敢编数据"的证据** |
| 4 | `onboard-desktop.png` | `http://127.0.0.1:8001/onboard.html?user=u_c_demo`，1440×900 | **画像只要四行字** |
| 5 | `pushes-375.png` | 第 1 张的地址，视口 375×812 | **手机上推送页可用**，导航单行、无横向滑动（实测 hOverflow=0） |

> 📌 **第 3 张为什么从推送页搬到列表页**：
> 推送页 20 条里 `unknown` = 0，**拍不出三态同屏**；公告列表页首屏里三种齐全。
> 而且列表页是用户天天看的页面，说服力更强。
> 旧的 `pushes-3states.png` 那一行按契约 ⛔ 的老毛病写的是"样例三条各占一态"，**已失效**。
>
> ⚠️ **「已过期」徽标和"三态"不在同一屏**（2026-10-18 02:20 重测）：
> 列表默认按发布时间排，**首屏（卡片 1~7）能拍到三种说法，但一个已过期徽标都没有** ——
> 徽标最早出现在**第 21 张卡**（约 y=3000），50 张里一共 **6 个**。
> **要一屏同时有「三态 + 已过期」，只能用整页截图**（`-full.png`，1440 宽下整页高约 6900px），
> 否则就得拆成两张：第 3 张拍三态，第 10 张拍徽标。
>
> **「已过期」徽标在推送页上一个都没有**：`expired_matched=19` 全部按紧急度沉底，
> 544 条命中压不进前 20。B 第二十五场已确认是「沉底但保留」，不是被过滤。
> 所以别在推送页找那个徽标，找不到不是 bug。

## 2. 有位置就加（支撑材料）

| # | 文件 | 怎么截 | 要证明什么 |
|---|---|---|---|
| 6 | `onboard-375.png` | `onboard.html` 375×812 | 手机能填画像，输入框 44px 起 |
| 7 | `pushes-empty.png` | `pushes.html?user=u_demo`（没画像 → `empty_reason=no_profile`） | **空态不是白屏**：「今天没有值得打扰你的新变化」+ 解释 |
| 8 | `pushes-e500.png` | 起 `_c-states.py --port 8011`，切 `e500`，开 `8011/pushes.html` | 接口挂了也有出路：明确报错 + 重试 |
| 9 | `pushes-timeout.png` | 同上切 `timeout` | **不再无限转圈**：8 秒后说「接口没有响应」 |
| 10 | `notices-expired-badge.png` | `notices.html` **往下滚到第 21 张卡**（约 y=3000）；1440 宽下徽标在第 **21、30、33、35、36、50** 张卡上（共 6 个） | **徽标规则是 `state==='found' && days_left<0`**，`absent`/`unknown` 一律不标。样式是「`截止 2026-10-09` **已过期**」，悬停提示「可能因学校延期或我们抽取有误仍有效，请点原文核对」 |

## 3. 不要用的截图

- **用 `?source=sample` 拍的图，当成"真实推送"讲**。样例那张图顶部挂着横幅，而且现在只剩 1 张卡。
- 任何**页脚写着「本地样例」而正文没解释**的图。
- 论坛页（**当天先看列表有没有帖子**，0 条会显得产品是空的）。
- 详情页的**付费墙**当成推送的付费墙用：两者话术不同（402 是额度用尽，推送墙是订阅）。

---

## 4. 怎么重复得到同一批图（不靠手工）

截图不是用手点的 —— 用 CDP 驱动，**同一份任务清单能反复跑出同样的图**，
而且除了图还会量回「有没有横向溢出 / 有没有 undefined / 触控目标够不够大」。

```powershell
cd C:\Users\<user>\OneDrive\文档\deepseek-harness\default-workspace
$env:CDP_PORT='9333'          # 先起好 headless Chrome（见 HANDOFF-C.md §2）

& 'D:\nodejs\node.exe' _c-cdp.js _c-tasks-3state3.json _c-3state3   # 顶部那一行 / 三态 / 徽标
& 'D:\nodejs\node.exe' _c-cdp.js _c-tasks-3state4.json _c-3state4   # 推送卡数 / 横幅 / 免费墙
& 'D:\nodejs\node.exe' _c-brief.js _c-3state3                       # 一行一页，人读
```

**这一轮加了 `evalAfterProbe`**（任务里能带一段 JS，跑完探针再量一次），
于是「三态各有几条」「几个已过期徽标」「`unknown` 带没带链接」都成了**量出来的数**，
不再靠肉眼看截图数。这一轮的全部结论都来自它：

```javascript
// 任务里的字段，探针跑完后在同一页再执行一次，结果写进 report.json 的 extra
"evalAfterProbe": "(function(){var q=...;return {found:..., absent:..., unknown:..., badgeExpired:...};})()"
```

> ⚠️ 还是那两条坑：**必须 `Network.setCacheDisabled`**（脚本里已加，别删），
> 以及**别用 `Set-Content -Encoding UTF8` 写任务 JSON**（会加 BOM，`JSON.parse` 直接炸）——
> 用 `Out-File -Encoding utf8` 配合 `ConvertTo-Json`，或者干脆用 Node 写。

> 「我改好了」和「屏幕上对了」是两件事 —— 所以每张图都是**改完之后重截**的，
> 而且同一次跑里同时拿到了量出来的数（横向溢出 0、脏字 0）。
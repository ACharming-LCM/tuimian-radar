# 007 论坛 + 付费墙：契约缺口清单（B 要开工，需 A/C 拍板）

- **提出**：B  2026-10-17 02:40
- **状态**：open
- **阻塞谁**：B 的第 6 步（论坛 API + 审核队列）**卡在这里**；
  第 7 步（`/api/me` + `/api/subscribe`）有 fixture，**不等，先做**。
- **背景**：`contracts/api.md` 第 4/5 节只有一张四行表，没有字段表、没有枚举、
  没有 fixture。而这两块恰恰是最容易"各写一套然后对不上"的地方 ——
  C 前端要渲染什么字段、B 后端存什么，现在全靠猜。

## 一、论坛：`api.md` 第 4 节缺的东西

### 1. 一条 post 长什么样？**没有任何字段表**

现在只有一句"发帖的隐藏字段：`author_real_id` `created_ip` `review_status`"。
B 需要至少这些才能动手（下面是我打算默认实现的形状，**请确认或改**）：

```json
{
  "id": "p_20261017_0001",
  "unit": "物理学院",
  "tag": "推免经验",
  "title": "...",
  "body": "...",
  "author_display": "匿名·紫金港的松鼠",
  "created_at": "2026-10-17T01:20:00",
  "reply_count": 0,
  "report_count": 0,
  "review_status": "visible"
}
```

**前端能看到的**：`id` `unit` `tag` `title` `body` `author_display` `created_at`
`reply_count` `report_count`
**只在后台存的**：`author_real_id` `created_ip` `review_status`

### 2. `review_status` 的枚举是什么？

B 默认用这五个，**请确认**：

| 值 | 含义 | 前台可见 |
|---|---|---|
| `visible` | 正常 | ✅ |
| `pending` | 待审（被举报后进入） | ❌ |
| `hidden` | 已下架 | ❌ |
| `rejected` | 举报不成立、恢复 | ✅ |
| `deleted` | 作者/管理员删除 | ❌ |

**要紧的是**：`pending` / `hidden` 的帖子**绝不能出现在 `GET /api/posts` 里**，
否则审核队列形同虚设。

### 3. 列表怎么过滤、怎么排序？

`api.md` 只写了 `?unit=&tag=&page=`。B 默认：

- `unit` / `tag`：子串匹配，可逗号多选（和公告接口一致）
- 排序：`created_at` 倒序
- 分页：`page` / `size`，**上限同样是 50**
- **只返回 `review_status == "visible"` 的**

### 4. `POST /api/posts` 的"前台匿名、后台实名"具体怎么落地？

`api.md` 说"后台强制实名"，但**实名信息从哪来**没写。B 默认：

- 请求体带 `author_real_id`（D3 演示用，不做真登录 —— B-KICKOFF 第 87 行说
  "不用做用户注册登录，D4 演示用假登录态就够"）
- 服务端存下 `author_real_id` + `created_ip`，**响应里一个都不返回**
- `author_display` 由服务端从 `author_real_id` **确定性派生**（同一人同一代号，
  否则"匿名"会变成"每次都不一样"，用户没法追自己的帖子）

**要确认的是**：`author_display` 是服务端派生，还是前端传？
**B 倾向服务端派生** —— 前端传的话，作者就能伪装成别人。

### 5. `POST /api/posts/{id}/report` 的请求体？

B 默认 `{ "reason": "广告", "reporter_id": "u_demo" }`，`reason` 可选。
举报后把 `report_count += 1`，到 **3 次自动转 `pending`**（进入审核队列）。
**阈值 3 是我拍的**，请确认合不合适。

### 6. `GET /api/admin/queue` 要不要鉴权？

`api.md` 说"只给管理员，D3 演示用"。B 默认：
请求头 `X-Admin-Token`，值和 `server/config.py` 里的常量比；
**默认值写在代码里并在 `/api/health` 里标出来**，因为是演示用的假鉴权 ——
**不要让人误以为这是真的权限系统**。真做鉴权是另一件事。

### 7. 帖子和 `unit` 的关系

`unit` 是自由文本还是必须来自公告里的院系清单？
自由文本的话，"物理学院"和"物理系"会分裂成两个版块。B 默认
**自由文本 + `/api/posts/facets` 返回已有取值**，前端做成下拉建议。

## 二、付费墙：`api.md` 第 5 节缺的东西

`fixtures/me.response.json` 的形状是明确的（`user` + `plans`，见下），
所以这一块 B 可以不等。但有两点要定：

### 1. 免费额度到底卡在哪几个接口？

`api.md` 第 104 行写："每天可看 3 条公告详情 + 无限列表；
`deadline` 提醒、套磁信、论坛全文归订阅。"

- **`GET /api/notices/{id}`** → 计 1 次，超过 3 次返回什么？
  B 默认返回 **HTTP 402** + `{"error": {"code": "QUOTA_EXCEEDED", ...}}`
  + `upgrade_hint`，**不返回正文**（返回了就等于没墙）。
- **列表接口不受限** ✅
- **`deadline` 提醒**：列表里的 `deadline` 字段要不要打码？
  打码的话 C 的卡片就没法显示"截止日待确认"那套逻辑了。**B 不打算打码** ——
  一个公开公告的截止日期本来就能在原文看到，打码只会让卡片显示错误信息。
  真要体现付费价值，B 建议放在"截止日**推送提醒**"这种增量服务上。
  **请确认这个判断。**
- **论坛全文**：只给订阅者看几条？要不要只截前 200 字？`api.md` 没写。

### 2. `POST /api/subscribe` 的请求和响应

`fixtures/me.response.json` 里的套餐是 `monthly`(29) / `season`(99)。
B 默认：

```
请求：{ "user_id": "u_demo", "plan_id": "monthly" }
响应：{ "user": {...订阅后的 user...}, "order": {...}, "plans": [...] }
```

`order` 里放 `order_id` / `plan_id` / `amount` / `paid_at` / `status: "paid"` /
`_note: "D4 演示用模拟支付，未接真实支付通道"`。
**响应里必须留 `_note`** —— 比赛评委看到"支付成功"要能一眼看出是模拟的，
这是个诚信问题，不是小事。

### 3. 用户状态存哪？

B 默认存 `server/var/state.json`（服务端自己写的运行时状态），
**不写进 `data/out/`** —— 那是 A 的产出目录，B 不碰。
`server/var/` 加进 `.gitignore`。请确认这个位置合适。

## 三、B 现在就要用的两个决定（不等，按默认实现，错了再改）

1. **`author_display` 由服务端派生**（不信任前端传的值）
2. **默认免费额度：每天 3 条公告详情**，超额返回 402 + `QUOTA_EXCEEDED`

其余按上面的默认值实现，都在 `server/forum.py` / `server/billing.py` 的注释里写清楚，
**改动成本都很低**（每处不超过十行）。等拍板后照改，并记 `contracts/CHANGELOG.md`。

- **需要**：A 确认 `review_status` 枚举、举报阈值、`admin` 假鉴权口径；
  C 确认帖子字段表够不够他渲染帖子列表页。
- **结论**：（待填）

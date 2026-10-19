# tuimian · 推免雷达

把散落在各校研究生院 / 院系官网的招生公告抓成结构化数据，按你的背景筛出**该看的那几条**，每条都能点回原文核对。

> **这份 README 是 2026-10-19 重写的。** 旧版是开发第一天的日志（写着"18 条""286 条"），已作废。
> 项目内部的教训与决策过程在 `docs/` 和 `journal/`（300+ 篇），不需要全读。

> **数据说明（公开仓库）**：本仓库**不含原始抓取数据** `data/out/`
> （公告正文里有学校公示的考生名单，导师数据含联系方式）。
> 但带了一份**脱敏后的完整副本** `data/sample/out/`（2026-10-19 快照，2,561 条）：
> 名单类公告的正文已隐去、手机号/证件号已打码、导师电话已清空，截止日与三态不变。
> 服务端在 `data/out/` 不存在时会自动使用 `data/sample/out/`，**新克隆不用先抓数据也能直接跑**。
> 抓新数据后，用 `python collector/tools/sanitize_for_deploy.py --src data/out --dst data/sample/out`
> 重新生成公开版。
> 文档与日志中引用的真实姓名、学号、电话、邮箱已替换为占位符（如「张某某」、`teacher01@…`）。

**交付材料**：[BP.md](BP.md)（商业计划书）· [DEMO.md](DEMO.md)（2–3 分钟演示脚本）·
[demo/demo.html](demo/demo.html)（截图版演示，浏览器直接打开）· [DEPLOY.md](DEPLOY.md)（魔搭 / Docker 部署）。

---

## 一、它是什么 / 不是什么

**是**：一个**保研信息监控与核对工具**。官网发一条公告，最快 2 分钟进入系统；每条记录都能点回原文；读不到的字段会说"没读到"，而不是编一个。

**不是**：不是"保研指导平台"。它**不**预测录取概率、**不**给选校建议、**不**有历年分数线。这些需要多年数据，我们没有。

**核心主张只有一句**：**说得清自己哪里不知道。**

所以每个字段都是三态：

| 状态 | 含义 | 界面上怎么写 |
|---|---|---|
| `found` | 读到了 | 直接显示 |
| `absent` | 正文可信，学校确实没写 | 「该公告未公布截止日」 |
| `unknown` | 我们没读到（正文是兜底抓的 / 太短 / 扫描件） | 「未能读取，请点原文核对」 |

**`absent` 和 `unknown` 绝不能混。** 混了就等于用"不知道"冒充"没有"。

---

## 二、现在是什么状态（2026-10-19 16:30 实测）

```
公告入库        2561 条          （29 所学校 / 34 个院系单位）
默认展示        2419 条          （142 条 junk 默认拒收，可切换查看）
事件流          2561 条          （无事件公告 = 0）
导师            557 位           （327 位有邮箱）
契约门禁        232 通过 / 0 失败 / 共 232 条（原始数据与部署脱敏数据各跑一遍）
页面            11 个，全部走真接口
```

内容类型（`notice_kind`）：

| 类型 | 条数 | 说明 |
|---|---:|---|
| `announcement` | 512 | 招生通知：推免、夏令营、预报名、报名、复试、调剂 |
| `catalog` | 285 | 目录 / 简章 / 章程 —— **参考资料**，不进推送首屏 |
| `emba` | 54 | EMBA / MBA / 在职攻读 —— 对学术型画像是噪声 |
| `other` | 1710 | 其余（讲座、会议、竞赛、事务通知） |

截止日三态：`found` 728 / `absent` 1167 / `unknown` 666。

**其中「截止日已经明确、且还没过」的只有 36 条。** 这个数字很小，不是 bug —— 多数学校根本不写截止日，我们不猜。

---

## 三、怎么跑起来

### 环境

```
Python 3.10+（3.10 和 3.13 都实测通过门禁）
依赖：pip install -r requirements.txt（测试另需 jsonschema）
```

### 三步

```powershell
# 0. 建虚拟环境并装依赖（一次即可）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt jsonschema

# 1. 起服务（自带体检：检查数据文件、端口、依赖）
.venv\Scripts\python.exe run.py            # 默认 127.0.0.1:8000，可用 --port / 环境变量 PORT 改

# 2. 浏览器打开
#    http://127.0.0.1:8000

# 3. 只体检不起服务
.venv\Scripts\python.exe run.py --check
```

也可以双击 `run.bat`：它按 `%PYTHON%` → 仓库下 `.venv` → `py -3` → `python` 的顺序找解释器（故意写成纯 ASCII —— `cmd.exe` 按 GBK 解析 .bat，写中文会崩）。

管理口令：设环境变量 `TUIMIAN_ADMIN_TOKEN`；不设则每次启动随机生成，打印在启动输出里。部署到公网见 `DEPLOY.md`。

### 跑测试

```powershell
cd server
..\.venv\Scripts\python.exe tests/run.py      # 契约门禁，230 条，必须 0 失败
```

### 抓数据

```powershell
cd collector
$env:PYTHONIOENCODING = 'utf-8'      # 不设的话，GBK 控制台遇到特殊字符会让整轮抓取崩溃

..\.venv\Scripts\python.exe crawl.py --fast        # 增量：只读第 1 页 + 只抓新 URL，约 2 分钟
..\.venv\Scripts\python.exe crawl.py --refresh     # 全量重抓，约 16~60 分钟
..\.venv\Scripts\python.exe crawl.py --only 物理学院  # 只跑一个单位

..\.venv\Scripts\python.exe tools/make_events.py --daily --write   # 生成事件流（推送的数据基础）
..\.venv\Scripts\python.exe tools/run_daily.py                      # 完整日更（默认走缓存）
```

**抓取期间的纪律**：数据写到 `.tmp` 再原子替换，抓取过程中线上文件保持完整。这条是血的教训（见 `docs/HANDOFF-TEAMMATE.md`）。

---

## 四、目录

```
tuimian/
├─ collector/       数据管道（A 的地盘）
│   ├─ sites.json       站点与栏目配置（34 个单位 / 37 个站点）
│   ├─ discover.py      首页 → 自动发现招生栏目
│   ├─ listparse.py     列表解析（翻页、去重）
│   ├─ detail.py        详情正文 / 标题 / 附件
│   ├─ fields.py        规则抽取：截止日、专业、材料、联系方式 + notice_kind 分类
│   ├─ kind.py          内容类型判定（announcement / catalog / emba / other）
│   ├─ crawl.py         抓取主入口
│   └─ tools/           日更、事件、fixture、诊断工具
├─ server/          服务层（B 的地盘）
│   ├─ app.py           全部 HTTP 接口（35 个）
│   ├─ push.py          推送打分与分层（tier1 / tier2 + why）
│   ├─ match.py         画像匹配
│   ├─ quality.py       数据健康门禁
│   └─ tests/run.py     契约门禁（226 条）
├─ web/             前端（C 的地盘，11 个页面，纯静态）
├─ contracts/       数据契约（改代码前先改这里）
├─ data/
│   ├─ raw/             页面缓存（785 MB，按 URL sha1 命名，删了就要重抓）
│   └─ out/             产出：notices.jsonl / events.jsonl / freshness.json 等
├─ docs/            设计文档、数据口径、演示脚本
└─ status/          三人（A/B/C）状态板 + 质检说明
```

---

## 五、接手先读这四份

| 顺序 | 文件 | 为什么 |
|---|---|---|
| 1 | **`docs/HANDOFF-TEAMMATE.md`** | 交接文档：现在能做什么、不能做什么、12 个坑、下一步 |
| 2 | `docs/DATA-NUMBERS.md` | 所有对外数字，每个都能用命令复算。**引用前重跑** |
| 3 | `docs/PROJECT-BACKGROUND.md` | 项目从哪来、给谁用、为什么这么设计 |
| 4 | `status/BOARD.md` | 谁在做什么、什么卡着 |

演示相关：`docs/RECORDING.md`（七步脚本）、`docs/DEMO-NOTICES.md`（用哪几条公告）、`docs/PPT-OUTLINE.md`。

---

## 六、已知边界（写在前面，别等被问）

**数据覆盖**

- 29 所学校 / 37 个配置站点，其中**上海交通大学**详情页是 JS 渲染，正文抓不到（53 条被判 junk），基本等于没接
- 三校抽样对照（北大 / 川大 / 清华物理）：改配额后漏率从 74% 降到 17.4%；**其余 26 所没验过**
- 厦门大学只有 5 条、东南大学 / 华东师范不足 10 条

**内容性质**

- 2561 条里 78% 是半年前的历史公告（最老 2010 年）。这是"不漏"的代价 —— 我们把官网档案也收进来了
- 近 30 天发布的只有 114 条，其中 `announcement` 更少

**技术限制**

- 微信公众号内容抓不到（无开放接口）
- SPA / JS 渲染页面抓不到（需浏览器预渲染）
- 扫描版 PDF 无 OCR 引擎，13 条附件读不出字
- 鉴权是假的（演示用假登录态），没有真实账号体系
- 没有邮件 / 微信通知渠道，"盯着并叫你"只做到站内红点

---

## 七、三条铁律

1. **宁可空，不许凑。** 没有对口的招生公告时，就显示空 + 说清"今天没有"。拿讲座凑满一屏是最坏的选择。
2. **读不到就说读不到。** `unknown` 不许改写成 `absent`，也不许为了数字好看放宽判据。
3. **改完代码要重启服务。** 默认不开 `--reload`（开发时可用 `run.py --reload` 或 `set RELOAD=1 & run.bat`），不重启的话验收的人测到的全是旧行为 —— 这个坑栽过两次。

---

## 八、许可与数据来源

所有数据来自各高校公开官网，仅用于信息聚合与个人查询。抓取遵守礼貌间隔（1~2 秒/请求）、走本地缓存、不绕过验证码与访问限制。

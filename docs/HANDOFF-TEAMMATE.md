> **历史文档（截至 2026-10-20）**：本文的数字/状态可能过时；最新口径见根目录 README.md 与 DELIVERY.md。

# 交接文档：把 tuimian 交给队友

> 写给**接手的人**。假设你从没见过这个项目、也没有任何上下文。
> 写于 2026-10-19 16:30，最后更新 17:25（`HEAD = 255a544`，门禁 **230/0**）。
>
> 配套阅读：`README.md`（项目是什么、怎么跑）、`docs/DATA-NUMBERS.md`（所有数字）。

---

## 零、先说清楚你接的是什么

这个项目原本是**三个人 + 三个 AI agent** 并行做的：

| 角色 | 地盘 | 做的事 |
|---|---|---|
| A（数据） | `collector/` | 抓取管道、字段抽取、内容分类、去重 |
| B（服务） | `server/` `contracts/` | 35 个接口、打分排序、数据健康门禁、**230 条**契约测试 |
| C（前端） | `web/` `docs/` | 11 个页面、演示脚本、所有对外数字 |

交付状态：**能跑、能演示、有 230 条自动检查**，但**不是"保研指导平台"**，是一个**信息监控与核对工具**。

交接后这些活全落到你一个人身上。所以下面这份文档的重点不是"功能清单"，是**哪些地方会咬你**。

---

## 一、发给你什么文件（以及不发什么）

### 必须发

| 内容 | 大小 | 说明 |
|---|---:|---|
| **`tuimian.bundle`**（git bundle，含 **306 个提交**） | **15.3 MB** | **首选**。一条命令拿到全部代码 + 全部历史 |
| **`data/out/`** | 32 MB | 产出数据。已在 git 里，但**如果 A 有未提交改动，要一起打包** |
| **`recordings/`** | 2 MB | 演示关键帧 12 张 |
| `docs/` `status/` `journal/` | 1.7 MB | 300+ 篇决策与踩坑记录。不看也行，但遇到怪问题时答案多半在里面 |

> **为什么用 bundle 而不是压缩包**：bundle 带完整提交历史，你能看到"每个决定是哪天、为什么这么改的"，出问题能 `git log -p` 追溯。压缩包只有一个快照，等于把 300 条经验丢了。

生成命令（在你自己的机器上跑）：

```powershell
cd D:\tuimian
git bundle create ..\tuimian-20261019.bundle --all
```

对方接收后：

```powershell
git clone tuimian-20261019.bundle tuimian
cd tuimian
git remote add origin <你们的远端>
```

### 不发（但要知道它们存在）

| 目录 | 大小 | 为什么不发 |
|---|---:|---|
| `data/raw/` | **785 MB** | 页面缓存。删了不影响代码，但**下次抓取得重新请求所有学校**（一轮 16~60 分钟）。传得动就传，传不动就重抓 |
| `_scratch/` | **129 MB** | 调试用一次性脚本和日志（`a15-scu.log` 之类），基本是垃圾，可以直接删 |
| `server/var/` | 小 | 运行时状态（假登录态 / 帖子 / 订阅）。**不带也不会坏**：第一次启动会自动生成一份**干净的演示状态**（`u_demo` 免费 + `u_num_demo` 完整画像），见 `server/state.py` 的 `load_or_seed()` |
| `__pycache__/` | 小 | 删掉 |

### 打包前检查清单（2026-10-19 17:25 已全部做过，你再确认一次）

```powershell
cd D:\tuimian

# 1. 工作区必须干净 —— 未提交的东西进不了 bundle
git status --porcelain
# 期望：没有任何输出

# 2. 数据完整
D:\python.3.10.11\python.exe -c "import json;n=[json.loads(l) for l in open('data/out/notices.jsonl',encoding='utf-8') if l.strip()];print(len(n))"
# 期望 2561，且没有坏行

# 3. state 只剩演示用户（现在已经是 2 个：u_demo / u_num_demo）
D:\python.3.10.11\python.exe -c "import json;d=json.load(open('server/var/state.json',encoding='utf-8'));print(len(d.get('users') or {}))"
# 期望 2

# 4. 门禁绿
cd server
D:\python.3.10.11\python.exe tests/run.py
# 期望 230 通过 / 0 失败

# 5. 生成 bundle
cd D:\tuimian
git bundle create D:\tuimian-20261019.bundle --all
```

> 想清测试用户的话有个现成脚本：`server\tests\clean_demo_state.py --keep-demo-only --apply`
> （默认只干跑报告，`--apply` 才真删，会先备份）

### 怎么确认 bundle 是好的（别直接发出去）

**在一台干净目录里真的克隆一次、跑一次门禁** —— 这是唯一能证明"发出去的东西能用"的办法。

```powershell
git clone D:\tuimian-20261019.bundle D:\_check
cd D:\_check\server
D:\python.3.10.11\python.exe tests/run.py
```

**期望结果（2026-10-19 17:20 实测）**：

```
230 通过 / 0 失败 / 共 230 条

跳过 1 条（缺少运行记录，不判红；**不是通过**）：
  SKIP  data_最近一轮fast必须成功
        data/logs/ 里没有任何 fast 轮次（新克隆 / 从未跑过定时任务）—— 不判红
```

**那条 SKIP 是正常的**：`data/logs/` 在 `.gitignore` 里，新克隆没有定时任务日志；
在你自己机器上跑过一次定时任务后，它会变成正常断言。

**在干净克隆里看到任何红色，先别发出去。**

---

## 二、环境：三个机器特定的坑

| 依赖 | 实际情况 | 踩了会怎样 |
|---|---|---|
| **Python 路径** | 必须用 `D:\python.3.10.11\python.exe` | 系统 `py -3` 解析到 3.13，缺 fastapi / lxml，起来就崩 |
| **端口 8001** | 服务默认开在 `127.0.0.1:8001` | 如果被占用，`run.py` 会报错退出（不会自动换端口） |
| **编码** | 所有命令前设 `$env:PYTHONIOENCODING='utf-8'` | 不设的话，抓到含特殊字符（如北大栏目名里的 `\ue603`）的页面时 `print` 崩溃，**整轮抓取全废** |

依赖清单（`D:\python.3.10.11` 已装齐）：`fastapi` `uvicorn` `lxml` `pypdf` `python-docx`。
**没有** OCR 引擎，**没有**浏览器自动化（playwright 之类），所以扫描件和 JS 页面都抓不到。

---

## 三、现在能做什么 / 不能做什么

### 能（都实测过）

```
填入画像（学校 / 专业 / 意向学校 / 方向 / 目标层次）
  → 拿到 10 条推送，每条带「为什么推给你」和「还剩几天」
  → 点开详情：材料清单、联系方式、附件、正文、原文链接
  → 匹配导师（浙大物院 10 位教授，方向精准，邮箱齐全）
  → 生成套磁信（带 4 条引文，每条可点回原文）
  → 关注某个学院，有新公告时站内红点提醒
```

技术指标：门禁 **230/0**（其中 1 条在新克隆里是 SKIP，见下）；首屏接口 33 KB / 240~430 ms；数据 2561 条 / 2419 可见 / 29 所学校。

### 不能（别对外承诺）

| 不能 | 原因 |
|---|---|
| 预测录取概率 / 推荐选校 | 需要历年分数线与报录比，没有 |
| 微信 / 邮件通知 | 需要服务号备案，只做到站内红点 |
| 抓微信公众号 | 无开放接口 |
| 抓 JS 渲染页面（如上海交大详情页） | 需浏览器预渲染，没做。上交 53 条正文是空的 |
| 处理扫描版 PDF | 无 OCR 引擎，13 条附件读不出字 |
| 真实账号体系 | 鉴权是假的（固定假登录态） |
| 覆盖所有学校 | 29 所 / 37 个配置站点，且只验过 3 所的完整性 |

---

## 四、十二个坑（全是这个项目真踩过的）

按"会不会让你浪费半天"排序。

### 1. 改完代码不重启服务，验收全是假象

uvicorn 没开 `--reload`。代码改了、进程还是旧的，你打接口测到的全是旧行为。

**这个坑今天栽了两次**：一次判"B 没做"，一次判"接口没生效"，重启后全好了。

```powershell
# 验之前先看这个
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'uvicorn' }
# 服务的启动时间必须晚于代码的最后修改时间
```

### 2. 抓取跑到一半会毁掉共享数据文件

真实事故：定时任务在 06:25 补跑全量，`notices.jsonl` 被中途写成了 69 条（原本 2751），**持续 3 小时**，服务只读到 69 条。

根因：增量落盘直接写线上文件。修法已落地（写 `.tmp`，跑完 `os.replace()` 原子替换），但**你自己写脚本时别再犯**。

**推论：任何会写 `data/out/` 的实验，先想清楚"我要是中途挂了，会不会毁掉现有数据"。**

### 3. 服务只读到旧数据时，先看 `store_reloads`

数据文件更新后接口会自动重载，但如果没重载，页面就是旧的。
`GET /api/health/data` 里的 `store_reloads` 能告诉你是几次重载。

### 4. 画像字段名写错会被"静默忽略"

产品读的是 **`preferred_schools`**（复数）和 **`majors`**（复数数组），
不是 `intended_schools` / `major`。

写错不会报错，只会**悄悄忽略**，然后你会得到"推送 0 条"的假象。
（这个坑让统筹者误判过一整轮"产品第一屏是讲座"。）

现在已经加了 `unknown_fields` 提示，但**自己调接口时仍要确认字段名**。

### 5. 一条坏字符能废掉整轮抓取

北大的栏目名里有私用区字符 `\ue603`，`print` 在 GBK 环境下抛 `UnicodeEncodeError`，
**整轮抓取当场崩，数据一条没落盘**。同类的还有全角冒号 `http：` 让 `urljoin` 抛异常。

已修（`stdout` 兜底 + `norm_url` 兜异常 + 单条记录失败不杀整轮），但抓取脚本的健壮性永远要当第一优先级。

### 6. `--refresh` 很危险

`run_daily.py` 原本默认带 `--refresh`（从零重抓）。一旦补跑就撞上工作时间，且要跑 16~60 分钟。

现在默认走缓存，`--refresh` 必须显式传。

### 7. PowerShell 的 `>` 重定向写 UTF-16

```powershell
git show HEAD:data/out/notices.jsonl > x.jsonl   # ❌ 写出 UTF-16，Python 读不了
git show HEAD:data/out/notices.jsonl | Set-Content x.jsonl -Encoding utf8   # ✅
```

### 8. `/api/match` 是 POST，且 items 是扁平的

- `POST /api/match`（不是 GET，GET 返回 404 是真的 404，不是接口坏了）
- `items` 里的字段直接在 item 上（`item.title`），**不是** `item.notice.title`
- push item 里 `id` 是**事件 id**，`notice_id` 才是公告 id —— 拿错会查到空

### 9. `absent` 和 `unknown` 不许混

`absent` = 学校确实没写；`unknown` = 我们没读到。
混了就是"用不知道冒充没有"，是这个项目最不能犯的错。

截止日三态现在：`found` 728 / `absent` 1167 / `unknown` 666。

### 10. 一个判据只在部分站点出错时，最容易瞒过去

案例：用 `/\d{5,}/` 判"文章 id"，结果维普系**栏目** URL 就是 5 位栏目号，
浙大材料 136 个栏目全被杀，该站从 6 条掉到 0 条。别的学校没炸，只因为栏目名是字母。

**改任何过滤器之后，必须逐站点前后对账，不能只看总数。**

### 11. 不要相信"看起来对"的指标

- `eval_faculty.py` 用"像人名的链接"做分母，把「学院概况」「师资队伍」算成人，回收率显示 16%（实际 100%）
- `crawl.py` 打印的统计是**去重前**的，附件字数会虚高 —— 对外一律从 `notices.jsonl` 现算

**一个不可信的指标比没有指标更危险，它会制造假结论。**

### 12. 定时任务"补跑"会挑最坏的时机

任务设 03:07，机器没开，Windows 会在机器醒来时补跑。
结果：早上 06:25 开始全量抓取，正好撞上工作时间 + 数据被写残。

如果保留定时任务，要么确保机器 24 小时开，要么让它写到独立的输出文件。

---

## 五、定时任务现状

| 任务 | 状态 | 说明 |
|---|---|---|
| `tuimian-fast` | **Disabled** | 每 30 分钟增量（`--fast`，约 2 分钟）。开发期间被停掉，交接后按需开启 |
| `tuimian-full` | **Ready**，下次 2026-10-20 03:07 | 每天全量 `run_daily.py` |

脚本在 `data/logs/run_fast.cmd` / `run_full.cmd`（纯 ASCII + 设 UTF-8 环境变量）。

开启/关闭：

```powershell
schtasks /change /tn "tuimian-fast" /enable
schtasks /change /tn "tuimian-fast" /disable
schtasks /end /tn "tuimian-full"      # 强行停止正在跑的那一轮
```

**注意**：两个任务可能同时跑（每 30 分钟的 fast 撞上 03:07 的 full），
原子写能让读者不受影响，但会浪费请求。要避免就错开时间。

---

## 六、下一步该做什么（按性价比）

### 优先级 1：把"第一屏质量"再抬一档（半天）

现状：首屏 10 条里 7 条 `announcement` + 3 条 `other`（报考点公告），**界面上看不出区别**。

- 给每张卡加类型标签：`招生通知` / `其他` / `参考资料`（后端已返回 `notice_kind`）
- `catalog`（285 条目录/简章）收进"参考资料"区，不进推送流（后端已支持 `notice_kind` 筛选）
- 「截止日读不到」的两种状态分开显示（`absent` 1167 / `unknown` 666）

### 优先级 2：补搜索（半天）

现在没有 `/api/search`。学生想找"凝聚态"、"考核细则"只能靠筛选。
搜 `title + body_text` 就够，不需要分词。

### 优先级 3：把剩下的 26 所学校的漏率验一遍（1~2 天）

现在只有北大 / 川大 / 清华物理三所有对照（漏率 17.4%）。
其余 26 所不知道漏多少。方法见 `docs/外部对照.md` —— 逐栏目翻到第 8 页，按 URL 对账。

### 优先级 4：补通知渠道（1 周）

现在只有站内红点。要做邮件或微信，需要：
- 邮件：SMTP 账号 + 定时任务扫新事件
- 微信：服务号（要备案，个人做不了）

### 不建议做的

- **再扩学校**：先把 29 所的完整性验清楚，比多抓 10 所更有价值
- **加"录取概率"功能**：没有数据支撑，做了就是编
- **把 junka 判据放宽**：宁可少几条，不可把真公告扔掉

---

## 七、接手第一天该做的事（检查表）

```powershell
# 0. **先确认"从零也能跑"**（B26/B27，2026-10-19 补）
#    server/var/ 和 data/logs/ 都不进 git，新克隆没有它们。
#    删掉再跑门禁 —— 必须仍然是 230/0：
#      · 缺 state.json → 自动建演示状态，门禁用临时状态，不红；
#      · 缺 data/logs/ → fast 用例**跳过**（会打印 SKIP，不是失败）。
cd D:\tuimian
Remove-Item server\var\state.json -ErrorAction SilentlyContinue
Remove-Item data\logs -Recurse -ErrorAction SilentlyContinue
cd server
D:\python.3.10.11\python.exe tests/run.py       # 期望 230 通过 / 0 失败（可能 1 条 SKIP）
cd ..
#    再起一次服务：会自动生成演示状态（u_demo 免费 / u_num_demo 完整画像）
D:\python.3.10.11\python.exe run.py --check     # 体检（不写状态）
D:\python.3.10.11\python.exe run.py             # 起服务（首次会生成 server/var/state.json）

# 1. 确认能跑
cd D:\tuimian
D:\python.3.10.11\python.exe run.py --check     # 体检
D:\python.3.10.11\python.exe run.py            # 起服务，打开 http://127.0.0.1:8001

# 2. 门禁必须是绿的
cd D:\tuimian\server
D:\python.3.10.11\python.exe tests/run.py       # 期望 230 通过 / 0 失败（可能 1 条 SKIP）

# 3. 用真实画像走一遍（字段名别写错）
#    POST /api/profile?user=u_test
#    {"school":"浙江大学","majors":["物理学"],
#     "preferred_schools":["北京大学","清华大学","中国科学技术大学"],
#     "interest_keywords":["凝聚态"],"target_level":"直博"}
#    然后 GET /api/push/preview?user=u_test
#    期望：10 条，topic 全是 apply，notice_kind 以 announcement 为主

# 4. 看数据是否自洽
D:\python.3.10.11\python.exe -c "import json;n=[json.loads(l) for l in open('data/out/notices.jsonl',encoding='utf-8') if l.strip()];print(len(n))"
# 期望 2561

# 5. 读这四份
#    README.md / docs/DATA-NUMBERS.md / docs/RECORDING.md / status/BOARD.md
```

---

## 八、如果你想继续用 AI agent 协作

原来那套协作法是可以复用的，关键在 `status/` 和 `docs/COLLAB.md`：

- **`status/BOARD.md`** —— 统筹者的工作台（谁在做什么、什么卡着）
- **`status/A.md` `B.md` `C.md`** —— 各角色的状态板（覆盖式更新，只写"现在"）
- **`status/QA-README.md`** —— 只读巡检角色说明（六项固定检查 + 基线数字）
- **`docs/COLLAB.md`** —— 目录所有制 + 三条禁令

**最有用的经验**：让一个 agent 专门做**只读巡检**，每轮自己跑一遍门禁、自己用学生画像走一遍产品。
这个项目里所有"自述与实测不符"的问题，最后都是巡检发现的，而不是开发的人发现的。

---

## 九、最后一句

这个项目最值钱的不是那 2561 条数据，是**它对"不知道"的处理方式**：

> 读不到截止日就说"读不到，请点原文核对"，而不是猜一个日期；
> 空的时候就说"今天没有"，而不是拿讲座凑满一屏；
> 判据会错，就把判据和实测一起写进文档，让下一个人能推翻它。

**如果你继续做，请守住这三条。** 守住它们，这个项目就算只有 29 所学校，也是可信的；丢了它们，就算覆盖 200 所，也只是另一堆没人敢用的数据。

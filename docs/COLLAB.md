# 三人协作 + AI 防打架规范

> 三天内三个人的 AI 会把项目搞乱，唯一原因不是 AI 笨，是**两个 AI 同时写同一个文件**。
> 这份文档就是解决这一件事。所有人和所有 agent 开工前必须先读它。

## 一、目录所有制（最重要的一条）

**任何人、任何 agent，只准写自己名下的目录。** 需要改别人的东西 → 提 `issues/`，不要直接动。

| 人 | 负责 | 独占目录 | 交付物 |
|---|---|---|---|
| **A 数据** | 爬虫、解析、去重、每日调度 | `collector/`、`data/` | `data/out/notices.jsonl` |
| **B 后端** | API、匹配打分、套磁信生成、付费墙 | `server/` | `GET /api/notices` 等接口 |
| **C 前端** | 页面、可视化、演示脚本 | `web/` | 可演示的 web 应用 |
| 共管 | 契约、文档、日报 | `contracts/`、`docs/`、`journal/`、`issues/` | —— |

`contracts/` 是共管但**不共写**：改动只能由当天的值班人落笔，且必须写 `contracts/CHANGELOG.md`。

## 二、契约先冻结，再各干各的

三个 AI 之间唯一的"接口"是两份文件，不是聊天记录：

1. `contracts/notice.schema.json` —— 数据结构。A 产出它，B 只读它，C 不碰它。
2. `contracts/api.md` —— HTTP 接口。B 产出它，C 只读它。

**规则：契约里的字段名、类型、必填项，一经冻结当天不许改。** 需要加字段 → 写 `contracts/CHANGELOG.md` 一行，升 `version`，并在 `journal/` 里 @ 相关人。

实例（今天真实发生）：抓取时发现"文件公示"的正文其实是内嵌 PDF，需要给 `attachments` 加 `kind` 字段。正确做法是改 schema + 记 CHANGELOG，而不是让 A 悄悄往 JSONL 里塞字段——那样 B 的解析会在半夜炸掉。

## 三、AI 之间怎么"沟通"：三个文件通道

AI 不能开会，不能互相发消息，**只能读文件**。所以沟通 = 写文件 + 读文件。只需要三个通道：

### 1. `journal/YYYY-MM-DD-<A|B|C>.md` —— 日报，单向广播，只追加

每次收工（或每 3 小时）追加一段，格式固定：

```markdown
## 14:30 A
- 做了：修好 lxml 编码 bug，附件抽取支持 pdfsrc
- 产出：data/out/notices.jsonl（18 条，mean conf 0.48）
- 卡住：deadline 召回率只有 1/18
- 需要：B 在 D2 接 LLM 兜底，接口先留 extract_fields(title, body, publish)
- 契约：无变更
```

**开工前必读**：`journal/` 里最新 3 条 + 所有未关闭的 `issues/`。这一步不做，三个 AI 一定重复劳动。

### 2. `issues/NNN-<短标题>.md` —— 需要别人决策/动手的事

```markdown
# 003 附件解析谁来写
- 提出：A  14:40
- 状态：open
- 背景：材料学院推免细则正文是内嵌 PDF，不解析等于没有这条公告
- 选项：1) A 用 PyMuPDF 抽文本  2) B 在入 LLM 前先转文本
- 需要：B 决定，因为影响 server 的入库流程
- 结论：（留给 B 填）
```

### 3. `contracts/CHANGELOG.md` —— 接口变更，一行一条

```markdown
- 2026-10-16 A: attachments[].kind 新增，枚举 file|pdf_embed|iframe|image（v1 兼容，非破坏性）
```

## 四、git 是唯一的同步点，而且有三条禁令

- 一人一分支，`main` 只接受合并，**禁止 force push**。
- **禁止 `git add -A` / `git add .`。** AI 会顺手把别人没写完的文件提交上来，这是最隐蔽的冲突源。必须写明确路径：`git add collector/detail.py`。
- **禁止重构不属于你的文件**（改缩进、改命名、跑格式化工具）。CI 不检查这个，但它制造 90% 的假冲突。

每天固定两次合流（午饭前、收工前），合流时每人只报三件事：改了什么文件、契约动没动、有没有阻塞。

## 五、一个人的 AI 怎么用 subagent

你的判断是对的，但要划边界。**只在这一个条件下开子 agent：两件事之间没有数据依赖，且产出物的路径不重叠。**

| 该开 | 不该开 |
|---|---|
| A 写爬虫 ‖ C 写前端 | 一个人写 schema，另一个人写解析器 |
| 同时调研 3 个学校的站点结构 | 让 agent 去"检查/审阅"另一个 agent 的产出 |
| 独立转录、独立取数、取多数一致值 | 串行依赖的流水线（A 的输出是 B 的输入） |

三条硬约束：

1. **一个主 agent 最多带 2 个子 agent。** 不是 20 个。
2. **绝不用 agent 做验证。** 实测教训：83 个 agent 跑核验，其中 45 个因为余额不足直接失败，而失败的被裁决逻辑静默算成"这条不成立"——**失败被伪装成结论**，比浪费 token 危险得多。核验一律主 agent 内联做：Grep → Read → 比对。
3. 子 agent 的 prompt 必须自带上下文（它看不到你的对话），并明确写"你只能改 `collector/listparse.py`"。

## 六、给每个 AI 的固定开场白

每次开新会话，把这段贴在最前面，不要靠它自己猜：

```
你是本项目的数据管道 agent。
- 项目根：<路径>/tuimian
- 你只能修改：collector/ 下的文件
- 数据契约：contracts/notice.schema.json（只读，改之前先提 issues/）
- 开工前先读：journal/ 最新 3 条、issues/ 全部未关闭项、contracts/CHANGELOG.md
- 收工必须写：journal/YYYY-MM-DD-A.md 追加一段（做了/产出/卡住/需要/契约）
- 禁止：git add -A、格式化别人的文件、改 contracts/ 而不记 CHANGELOG
- 冒烟测试：cd collector && python crawl.py --details 0（必须 0 报错）
```

## 七、四天时间表

| 天 | A（数据） | B（后端） | C（前端） |
|---|---|---|---|
| D1 | 30 校抓取管道、去重、每日调度 | 定 API 契约、搭 server 骨架、读 JSONL 出接口 | 定页面信息架构、搭前端骨架 |
| D2 | PDF/附件解析 → 正文补全；LLM 兜底抽取 | 匹配打分、套磁信生成 | 列表页 + 详情页 + 匹配结果页 |
| D3 | 论坛种子内容入库、审核后台数据 | 论坛 API、匿名/审核/举报、付费墙 | 论坛页 + 付费墙页 + 后台页 |
| D4 | 全量跑一遍、修漏抓、准备数据看板 | 联调、兜底错误、模拟支付 | 打磨、录演示脚本、截图 |

**每天 20:00 合流，只允许在合流点前后各 30 分钟改契约。**

## 八、冒烟测试（防止 A 改坏契约把 B 搞崩）

```bash
cd collector
python crawl.py --details 0        # 只测列表发现+解析，不抓详情，秒级
python -c "import json;[json.loads(l) for l in open('../data/out/notices.jsonl',encoding='utf-8')]"
```

B 和 C 在开工前跑这两条，全绿才开始写自己的部分。

## 九、提交前检查（三道，缺一道不许提交）

| 谁 | 命令 | 查什么 |
|---|---|---|
| A | `cd collector && python tests/test_<某>.py` | 六道回归测试全绿 |
| **B** | `python server/tests/run.py` | **55 项契约检查**（对 fixture 比形状、比对接口返回）—— **零第三方依赖，3 秒** |
| C | `node --check web/assets/*.js` + 自己的断言 | 语法与页面断言 |

**B 的那道必须进所有人的提交前检查。** 理由：A 改 `collector/` 产出、B 改 `server/`，
谁都可能踩到对方的形状假设，而这道检查只跑 3 秒。
（B 在 `issues/006` 里提出，A 同意。）

## 十、发快照的规矩（踩过的坑）

**给别人发快照（bundle / 压缩包 / 手工拷贝）的那一刻，等于承诺"之后不再改契约"。**

做不到的话，必须**同时补发新快照或 `git format-patch` 补丁** ——
否则对方是在流沙上盖房子。实测：A 发完 bundle 后又提交了 5 次，
其中 `api.md` 和两个 fixtures 都变了，B 差点照着过期契约写完整个 `/api/match`。

正确做法（B 提的，比空口承诺有用）：

> 发快照时在 `journal/` 里写一行：**快照点 = `<sha>`，之后如有契约改动会补发 patch**。
> 这样对方能自己判断要不要等。

## 十一、fixtures 的时效性（规矩）

`contracts/fixtures/` 里**形状是永久的，数值只对 `_as_of` 那天有效**。
详见 `contracts/api.md` 第 9 节。两条硬性动作：

1. **演示前、合流前重跑** `python tools/make_fixtures.py`，跑完记一行"快照点 = `<sha>`"。
2. **禁止跨日期比对数值**（`score` / `days_left` / `bucket` / `stats`）。
   要复现同样的数字就用 `--as-of` 重跑。

# 匹配打分契约（草案，A 起草，B 定稿）

> 状态：草案。**A 提供参考实现**（`collector/tools/match_preview.py`，纯标准库、可直接读），
> B 在 `server/` 里实现生产版本。本文是接口和规则契约，不是实现说明。
>
> 为什么先定这个：匹配是整个产品的价值主张 —— **"输入我的背景，它能不能告诉我该报什么"**。
> 这件事必须早证伪。规则打分不需要 LLM 就能跑，所以今晚就能拿真实数据验。

## 一、为什么是"可解释打分"而不是"排序模型"

67 条真实数据、没有标注、没有点击日志。任何"训练一个排序模型"的想法在这个数据量下都是自欺欺人。

更重要的是：**用户不会相信一个不解释的黑盒**。"推荐你报这个"必须能回答"凭什么"。
所以每个分数都要能拆成几条人话理由，这正是 `reasons[]` 的用途。

B 接 LLM 时，LLM 的职责是**补充规则抽不准的字段**和**把理由写得更像人话**，
不是替换打分公式。公式要保持可复现、可调参、可回归测试。

## 二、用户画像（profile）

输入 schema，按示例给：

```json
{
  "name": "示例学生",
  "school": "浙江大学",
  "majors": ["物理学", "力学"],
  "grade": "大三",
  "target_entry_year": 2027,
  "gpa_rank": { "rank": 5, "total": 120 },
  "english": { "cet6": 580, "toefl": null, "ielts": null },
  "research": ["CUPT 国赛一等奖", "凝聚态实验一段"],
  "target_levels": ["直博", "硕士"],
  "wanted_modes": ["推免", "预推免", "夏令营"],
  "preferred_schools": ["浙江大学", "中国科学技术大学", "复旦大学"],
  "preferred_units": ["物理学院", "材料科学与工程学院"],
  "exclude_expired": true
}
```

设计取舍：
- `majors` 是**数组**，同义词由用户自己写（"物理学" + "力学" + "应用物理"），
  不在代码里维护一张同义词表 —— 那张表会烂掉，而用户最清楚自己算哪个学科。
- `wanted_modes` 是硬过滤依据。统考类公告对推免生是纯噪声。
- `target_entry_year` 决定"是今年的机会还是明年的"。

## 三、打分公式

六个维度加权求和，权重之和 = 1。

| 维度 | 权重 | 取值规则 |
|---|---|---|
| `major` | **0.30** | 公告 `majors` 与画像 `majors` 有交集（互为子串即算命中）→ 1.0；公告 `majors` 为空 → **0.4**（未知不惩罚）；非空且不匹配 → 0.0 |
| `mode` | **0.20** | `apply_mode` ∈ `wanted_modes` → 1.0；为 `null` → 0.4；否则硬过滤掉（见第四节） |
| `level` | 0.15 | `degree_types` 与 `target_levels` 有交集 → 1.0；为空 → 0.4；不匹配 → 0.0 |
| `affinity` | 0.15 | 命中 `preferred_units` → 1.0；只命中 `preferred_schools` → 0.7；都没命中 → 0.2 |
| `year` | 0.10 | `target_year == target_entry_year` → 1.0；`>` → 0.6；`<` → 0.0；为 `null` → 0.3 |
| `freshness` | 0.10 | 按 `publish_date` 距今天数：≤30 天 → 1.0；≤90 → 0.7；≤365 → 0.4；更早 → 0.1 |

**关键设计：字段缺失给 0.4，不给 0。** 我们的抽取本来就不完整（`deadline` 只有 30%），
如果"抽不到"等于"不相关"，那抽取的缺陷会被伪装成匹配的结论 —— 这正是要避免的错误类型。
缺失必须表现为"降权 + 提示人工确认"，而不是"判定不相关"。

## 四、硬过滤（先过滤，再打分）

1. `exclude_expired = true` 且 `deadline` 已过 → 丢弃，计入 `dropped_expired`
2. `apply_mode` 非空且 ∉ `wanted_modes` → 丢弃，计入 `dropped_mode`
   （`apply_mode` 为 `null` **不丢**，因为 67% 的记录抽不到它）

## 五、置信度降权

```
final_score = raw_score × (0.7 + 0.3 × extract.confidence)
```

`extract.confidence` 是抽取器对自己这份记录的自评。低置信记录整体降权，最多降到 70%。
**这是为了不让"抽错的记录"排到"抽对的记录"前面。**

## 六、输出

```json
{
  "profile": { "...": "回显输入" },
  "generated_at": "2026-10-16T23:50:00",
  "stats": { "total": 67, "dropped_expired": 3, "dropped_mode": 12, "scored": 52 },
  "items": [
    {
      "id": "u1a2b3c4d-69706-3201144",
      "score": 0.78,
      "raw_score": 0.84,
      "bucket": "重点关注",
      "title": "2027年物理学院招收推免生综合考核实施细则",
      "school": "浙江大学", "unit": "物理学院",
      "publish_date": "2026-09-10", "deadline": null,
      "days_left": null,
      "url": "https://...",
      "reasons": ["专业对口：物理学", "推免", "目标单位：物理学院"],
      "warnings": ["未抽到截止日期，务必点开原文确认"]
    }
  ]
}
```

### `bucket` 枚举（按 `deadline` 定）

| bucket | 条件 |
|---|---|
| `立即行动` | `deadline` 存在且 `days_left ≤ 7` |
| `重点关注` | `days_left ≤ 30` |
| `可规划` | `days_left > 30` |
| `需人工确认` | `deadline` 为 `null` |
| `已过期` | `days_left < 0`（仅在 `exclude_expired=false` 时出现） |

### `reasons` 与 `warnings` 的写作规则

- `reasons`：只列**得分 ≥0.7 的维度**，一句话一张牌，格式 `维度：具体值`。
  得分 0.4（字段缺失）**不要**写成理由，它进 `warnings`。
- `warnings` 至少覆盖这三种，一条都不能省：
  1. `deadline` 为 `null` → "未抽到截止日期，务必点开原文确认"
  2. `extract.confidence < 0.5` → "该公告字段抽取置信度低，请核对原文"
  3. **材料缺口**：公告 `materials` 提到"六级/托福/雅思/推荐信"而画像里没有 → "公告要求 X，你的画像里没有"
- 第 3 条是产品差异化的关键：**用户最怕的不是不知道有哪些项目，是点进去才发现自己不符合条件。**

## 七、导师匹配（"专业契合的实验室"这一层）

公告只告诉你"怎么报名"，**不告诉你"这个方向要不要我"**。后者只能靠导师数据。
数据源：`data/out/faculty.jsonl`（契约 `contracts/faculty.schema.json`）。

画像新增**可选**字段：

```json
{ "interest_keywords": ["凝聚态物理", "超导", "量子", "自旋电子学"] }
```

打分（刻意简单，**可解释比准确更重要**）：

```
score = 0.70 × 兴趣覆盖率     （命中的兴趣词数 / 用户写了的兴趣词数，上限 1.0）
      + 0.15 × 单位匹配        （命中 preferred_units → 1.0；只命中 preferred_schools → 0.7；否则 0.2）
      + 0.15 × 层次匹配        （博导且目标含博士/直博 → 1.0；博导 → 0.8；否则 0.6）
```

命中判定用**互为子串**：`凝聚态物理` 能命中 `实验凝聚态物理`。

### 必须做的三件事

1. **`reasons` 要给具体命中的方向词**，不能只说"匹配度高"。
   用户要能一眼看出"为什么推这个老师"，然后自己去核对主页。
2. **`interest_keywords` 为空时不许静默降级。** 退化为用 `majors` 粗匹配，
   并在 `warnings` 里明说"你没填兴趣词，这里只用专业名做了粗匹配"。
3. **没有研究方向的导师要带警告**（"该导师页面没有研究方向，无法判断契合度"），
   并且不要因为字段缺失就把它排到前面。

### 快速验证

```bash
cd collector
python tools/match_preview.py --faculty --limit 10
```

实测（浙大物理 50 位导师，兴趣词"凝聚态物理/超导/量子/自旋电子学"）：

```
[0.82] 付海龙 研究员  实验凝聚态物理、极低温量子输运、低维量子材料和量子器件
[0.82] 曹光旱 教授    高温超导、非常规超导、新型量子材料
[0.65] 常凯   教授    凝聚态物理、半导体物理、自旋电子学
```

## 附：参考实现（公告匹配与导师匹配共用一份）

`collector/tools/match_preview.py`（纯标准库，无第三方依赖）：

```bash
cd collector
python tools/match_preview.py                                   # 公告匹配，用 tools/profile-example.json
python tools/match_preview.py --profile tools/profile-example.json
python tools/match_preview.py --min-score 0.5 --limit 20
python tools/match_preview.py --json                            # 给 B 对接用
python tools/match_preview.py --faculty --limit 10              # 导师匹配（第七节）
```

**这是基线，不是生产代码。** B 在 `server/` 实现时以本文为准；
如果发现本文的规则有问题，改本文并记 `contracts/CHANGELOG.md`，不要各写一套。

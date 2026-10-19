# -*- coding: utf-8 -*-
"""`notice_kind`：这条公告**是哪种**（A29）。

## 为什么要它

`topic=apply` 只回答"是不是招生相关"，把两类完全不同的东西混在一起：

- 「接收推免生工作通知」「夏令营报名」——**今天该看**，有时限、要动作；
- 「2027年硕士研究生招生专业目录」「招生章程」——**参考资料**，随时可查。

实测 apply 且非 junk 的 1193 条里，catalog 占 296 条，而首屏 10 条里有 6 条是它们。
把两类分开之后，首屏排序就能把 catalog 降权（B 那边用 `notice_kind` 排序）。

## 四档与判据

| 值 | 含义 | 关键词 |
|---|---|---|
| `emba` | 非全日制/在职项目 | EMBA / MBA / MPAcc / 高级管理人员 / 在职攻读 / 非全日制 |
| `catalog` | 参考资料 | 招生目录 / 专业目录 / 招生计划 / 专业一览 / 简章 / 章程 / 报考指南 |
| `announcement` | 招生通知 | 推免 / 夏令营 / 预推免 / 报名 / 复试 / 调剂 / 材料 / 接收 / 选拔 / 考核 |
| `other` | 其余（含所有 topic≠apply 的记录） | — |

## 判定顺序（两条要解释）

1. **emba 最先**：「EMBA 招生简章」既是简章也是在职项目，归 emba 更有用。
2. **两类词同时命中时**（42 条，例如「…接收优秀应届本科毕业生免试攻读研究生招生简章及专业目录」）：
   若目录类词是 **专业目录 / 招生计划** → 判 `announcement`；否则（简章 / 章程 / 招生目录）
   → 判 `catalog`。理由：前者是"随通知附一份具体文档"，通知才是主体；
   后者是"简章/章程本身" —— "招生简章"是拿来查的，不是今天要办的事。

   这条 tie-break 让四档数字与统筹给的口径完全一致（529 / 296 / 48 / 320）。

## 和 topic 的关系

`topic` 说"招不招生"，`notice_kind` 说"哪一种"。非 apply 记录一律 `other`（不猜）。
两者都是规则 + 关键词，都带命中词，**都能对用户解释"为什么这样分"**。
"""
import re

EMBA_WORDS = ("EMBA", "MBA", "MPAcc", "高级管理人员", "在职攻读", "非全日制")

CATALOG_WORDS = ("招生目录", "专业目录", "招生计划", "专业一览",
                 "简章", "章程", "报考指南")

# 这两类目录词跟"通知"同时出现时，主体是通知（见上面 tie-break）
DOC_WORDS = ("专业目录", "招生计划")

ANNOUNCE_WORDS = ("推免", "夏令营", "预推免", "报名", "复试", "调剂", "材料",
                  "接收", "选拔", "考核")

_ASCII_RE = re.compile(r"^[A-Za-z]+$")


def _hit(word, text):
    """英文缩写按词边界匹，避免 MBA 命中 "MBAssador" 这类噪音。"""
    if _ASCII_RE.match(word):
        return re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", text) is not None
    return word in text


def classify(rec):
    """返回 `(kind, hits)`。hits = 命中的关键词（最多 8 个），用来解释判定。"""
    if (rec.get("topic") or "") != "apply":
        return "other", []

    title = rec.get("title") or ""
    emba = [w for w in EMBA_WORDS if _hit(w, title)]
    if emba:
        return "emba", emba[:8]

    catalog = [w for w in CATALOG_WORDS if _hit(w, title)]
    announce = [w for w in ANNOUNCE_WORDS if _hit(w, title)]
    # 「…招生计划公告 / …招生工作的通知」：目录词只是**被公告的对象**，主体是通知。
    # 2026-10-19 补：不算这一条的话，中山大学那份「少数民族骨干人才招生计划公告」
    # 会被判 catalog，而推送层（B22）把 catalog 挡在首屏外 —— 演示样例直接空了 5 条。
    if catalog and not announce and "公告" in title:
        announce = ["公告"]

    if catalog and announce:
        # 两类词都在：看目录词是"具体文档"（专业目录/招生计划）还是"简章/章程本身"
        doc = [w for w in catalog if w in DOC_WORDS]
        if doc:
            return "announcement", (announce + doc)[:8]
        return "catalog", (catalog + announce)[:8]
    if catalog:
        return "catalog", catalog[:8]
    if announce:
        return "announcement", announce[:8]
    return "other", []

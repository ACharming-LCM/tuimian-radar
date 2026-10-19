# -*- coding: utf-8 -*-
"""今日更新 / 变更接口（`GET /api/changes`，契约 `contracts/api.md` 第 10 节）。

**它存在的理由**：这是"月度订阅"唯一可演示的落点 —— 用户付钱买的不是"有公告"，
是"哪一条是今天新出现的、哪一条被悄悄改了"。
在此之前的做法是 C 的今日更新页**直接读 `data/out/changes.json`**，跨过了接口层、
直接依赖 A 的内部产物；A 一改那个文件的字段，前端会在没人察觉的时候坏掉。

## 为什么不能直接代理那个文件

磁盘上 `changes.json` 的 `added` / `removed` 嵌的是**完整记录**（含 `body_text`），
所以它一度有 **626 KB**（实测历史版本 `ad553e5`）。直接代理等于把几万字正文
塞进一个"今天有什么变化"的列表响应里。

## 投影拿掉了什么、为什么

按契约要求返回精简字段，这里逐条说明取舍（免得后人以为漏了）：

| 原始 | 接口 | 为什么 |
|---|---|---|
| `added[]` / `removed[]` 完整记录 | 9 个字段 | 列表卡片只需要这些（对齐 `notice.schema` 里列表页必须保留的字段） |
| `changed[].record` 完整记录 | 9 个字段（同上） | 同上 |
| `changed[].fields` 原样 | 原样透传 | **但 `attachments` 例外**，见下 |
| `changed[].hash`（`(old,new)`） | **去掉** | 是 `content_hash`，对用户没意义；`body_changed` 已经表达了同一件事 |
| `reidentified[].record` 完整记录 | 9 个字段 | C 的 `daily.js` 读 `x.record`，保持嵌套 |

**`changed[].fields` 里 `attachments` 是哨兵元组、不是记录。** 对比时
`_att_sig()` 把它压成了 `[(url, text_len), ...]` 的**签名**，所以一对变更里可能
躺着 38 个附件对象（北大那条实测 38 个）。原样透传会让单个字段把响应撑大，
而且那种 `[[url, n], [url, n]]` 的形状前端也渲染不出来。
**处理**：只给两边条数 + `_detail` 标记，形如
`{"attachments": {"old_count": 0, "new_count": 1, "_detail": "附件清单已精简为条数"}}`。
信息没丢（"附件从 0 个变成 1 个"就是用户要看的），体积下来了。

**`changed[].fields.deadline` 的原始形状是 `[null, "2026-11-20"]`**，
也就是一个**两元素数组**。这个契约里就是这么写的（见 api.md 第 418 行），原样保留。
"""
from __future__ import annotations

import json
import os

# 变更条目里 `record` 的精简字段。和公告列表的 9 个必备字段对齐 ——
# 用户在这三处（新增 / 有变化 / 本轮未抓到）看到的卡片信息应当一致。
CHANGE_RECORD_FIELDS = (
    "id", "title", "school", "unit", "url", "publish_date",
    "deadline", "apply_mode", "target_year",
)

# `counts` 的键。契约 api.md 第 412 行。
# `reparsed` 是 2026-10-17 新增的（**纯新增、非破坏性**）：含义是"我们改了抽取代码"，
# 与 `changed`（"学校真的改了通知"）完全不同，见 api.md 第 431 行的对照表。
CHANGE_COUNT_KEYS = ("previous", "current", "added", "changed", "reparsed",
                     "removed", "reidentified", "unchanged")

# 四个"变更有内容"的桶（`previous`/`current`/`unchanged` 是总数，不是桶）
CHANGE_BUCKETS = ("added", "changed", "reparsed", "removed", "reidentified")

# ---------------------------------------------------------------------------
# **桶的语义分类 —— 单一事实来源。**
#
# 契约 api.md §10 的核心区分：哪些桶意味着"**外面的世界变了**"，
# 哪些意味着"**我们的处理变了**"。分成两个常量放在这里，是因为
# `/api/digest`（提醒）和 `/api/changes/history`（历史）都要用它：
#
#   · `digest` 只发 WORLD（把 reparsed 发进提醒等于说谎）
#   · `history.days_with_user_changes` 也只算 WORLD，这样两个接口同口径
#
# 各写一遍就会漂移，而漂移的表现是"history 说那天有变化、digest 不认识那天"，
# 用户会认为两个接口里有一个是坏的。
# ---------------------------------------------------------------------------

# 外面的世界真的变了：出现新公告 / 正文或附件 URL 集合变了
WORLD_CHANGED_BUCKETS = ("added", "changed")

# 我们的处理变了：改了抽取代码 / 调了 id 算法 / 这轮没抓到
# （`removed` 属于这一类 —— 契约 §10 明确它"不是公告被删除，是本轮没抓到"）
OUR_PROCESS_CHANGED_BUCKETS = ("reparsed", "reidentified", "removed")

# `fields` 里需要特殊处理的键（见模块 docstring）
_ATTACHMENT_KEY = "attachments"


def _trim_record(rec):
    """把完整记录压成卡片需要的字段。**不含 `body_text`。**"""
    if not isinstance(rec, dict):
        return {}
    return {k: rec.get(k) for k in CHANGE_RECORD_FIELDS}


def _trim_fields(fields):
    """投影 `changed[].fields` / `reparsed[].fields`。

    普通字段（`deadline` / `publish_date` / `title` / `apply_mode` / `target_year`）
    原样保留成 `[旧值, 新值]`；`attachments` 压成条数。
    """
    if not isinstance(fields, dict):
        return {}
    out = {}
    for k, pair in fields.items():
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            old, new = pair[0], pair[1]
        else:
            # 形状意外时不去猜，原样放着让人能看见
            out[k] = pair
            continue
        if k == _ATTACHMENT_KEY:
            out[k] = {
                "old_count": len(old or []) if isinstance(old, (list, tuple)) else None,
                "new_count": len(new or []) if isinstance(new, (list, tuple)) else None,
                "_detail": "附件清单已精简为条数（原文在公告详情里）",
            }
        else:
            out[k] = [old, new]
    return out


def _project_changed_like(rows, with_why=False):
    """`changed` 和 `reparsed` 的投影。**两者形状一样，语义完全不同：**

    - `changed`  = **学校真的改了通知**（正文变了且抽取器版本没变，或附件 URL 集合变了）
    - `reparsed` = **我们改了抽取代码**（抽取器版本不同 / 只有派生字段不同 / 附件只有 text_len 变）

    契约 api.md 第 431 行专门讲了为什么必须分开：A 修好标题解析器之后，
    日报立刻报出「有变化 22 条」，而**公告一个字都没变** —— 用户会以为学校改了 22 份通知。
    这跟 `reidentified` 是同一类错误：**把"我们的处理变了"说成"外面的世界变了"**。

    更狠的一次是 PDF 表格按行拼接（extract v4）：67 个 PDF 的正文全变了，
    不归因的话日报会说「学校改了 67 份通知」。

    **`with_why`**：`reparsed[]` 在磁盘上多一个 `why` 字段
    （"抽取器版本变了" / "附件抽取结果变了" / "派生字段不同"）。
    契约要求界面如实说明原因，所以必须透传 —— 用户要知道**为什么**这条被判成"我们改了代码"。
    `changed` 没有这个字段（契约里 `changed` 的判据已经表达得够明确了），
    传 `with_why=False` 时就不加，免得凭空多一个 `null` 键。
    """
    out = []
    for ch in (rows or []):
        if not isinstance(ch, dict):
            continue
        item = {
            "id": (ch.get("record") or {}).get("id"),
            "record": _trim_record(ch.get("record")),
            "fields": _trim_fields(ch.get("fields")),
            "body_changed": bool(ch.get("body_changed")),
        }
        if with_why:
            item["why"] = ch.get("why")
        out.append(item)
    return out


class ChangesStore:
    """读 A 的 `data/out/changes.json`，投影成契约要的精简形状。"""

    def __init__(self, path, data, load_error=None):
        self.path = path
        self.data = data or {}
        self.load_error = load_error

    @classmethod
    def load(cls, path):
        if not path or not os.path.exists(path):
            # **文件缺失不该 500。** 变更报告是"增量"能力：基线还没建立、
            # 或者第一轮跑还没出过报告，都是正常状态。
            # 这种情况下四个类别的计数是**真的 0**（确实没有变更可报），
            # 但 `previous` / `current` / `unchanged` 给 None —— 我们没比过。
            return cls(path, _baseline_missing_payload(),
                       load_error="还没有变更报告（基线未建立或本轮未跑）")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls(path, json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            # 文件**坏了**和**还没生成**是两件事：前者我们不知道有没有变更，
            # 报 `added: 0` 会被显示成"今天没有变化" —— 那是误导。
            # 所以这里所有计数一律 None，并且带 `_error`。
            return cls(path, None, load_error=f"变更报告读取失败：{e}")

    def response(self):
        """组装 `/api/changes` 响应。形状见 api.md 第 10 节。"""
        if self.data is None:
            # 读坏了：结构照给（前端不会崩），但计数全是 None + `_error`
            payload = _unknown_payload()
            payload["_error"] = self.load_error
            return payload

        d = self.data or {}
        counts = d.get("counts") or {}

        added = [_trim_record(r) for r in (d.get("added") or [])]
        removed = [_trim_record(r) for r in (d.get("removed") or [])]
        changed = _project_changed_like(d.get("changed"))
        reparsed = _project_changed_like(d.get("reparsed"), with_why=True)

        reidentified = []
        for x in (d.get("reidentified") or []):
            if not isinstance(x, dict):
                continue
            rec = _trim_record(x.get("record"))
            reidentified.append({
                # 契约 api.md 第 421 行写的是 `{id, old_id, title}`；
                # 原始数据里是 `{record, old_id, new_id}`。两个都保留：
                # `id` = 当前的 id，`old_id` / `new_id` 给要展示 id 变迁的前端。
                "id": rec.get("id"),
                "old_id": x.get("old_id"),
                "new_id": x.get("new_id"),
                "record": rec,
                "title": rec.get("title"),
            })

        payload = {
            "generated_at": d.get("generated_at"),
            "counts": {
                # 八个键一个不少（契约 api.md 第 412 行，含 2026-10-17 新增的 `reparsed`）。
                # **缺失的补 None 而不是 0** —— 前端按固定字段读，少一个键会静默显示成
                # undefined；而补 0 会把"没比过"伪装成"没有变化"。
                k: counts.get(k) for k in CHANGE_COUNT_KEYS
            },
            "added": added,
            "changed": changed,
            "reparsed": reparsed,
            "removed": removed,
            "reidentified": reidentified,
        }
        if self.load_error:
            # 读不到就明说。**绝不能让前端把"没读到"显示成"今天没有变化"** ——
            # 那是两个完全不同的结论，而后者会让用户以为可以安心了。
            payload["_error"] = self.load_error
        return payload


def _baseline_missing_payload():
    """基线还没建立：五个桶的计数是真的 0，但 previous/current/unchanged 给 None。

    不能填 0 —— "今天 0 条未变"和"我们根本没比过"是两个结论。
    """
    return {
        "generated_at": None,
        "counts": {"previous": None, "current": None, "added": 0, "changed": 0,
                   "reparsed": 0, "removed": 0, "reidentified": 0, "unchanged": None},
        "added": [], "changed": [], "reparsed": [], "removed": [], "reidentified": [],
    }


def _unknown_payload():
    """报告读坏了：结构照给（前端不会崩），但**所有计数一律 None**。

    不给 0 是刻意的：0 会被显示成"今天没有变化"，
    而实际上我们连有没有变更都不知道。
    """
    return {
        "generated_at": None,
        "counts": {k: None for k in CHANGE_COUNT_KEYS},
        "added": [], "changed": [], "reparsed": [], "removed": [], "reidentified": [],
    }

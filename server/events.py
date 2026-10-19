# -*- coding: utf-8 -*-
"""事件流读取：`data/out/events.jsonl`（契约 api.md 第 16 节）。

## 为什么要有单独一层

`notices.jsonl` 回答"现在库里有什么"，`events.jsonl` 回答
"**历史上发生过哪些值得打扰用户的事**"。推送要的是后者。

两个文件都是 A 的产出，但它们在**时间语义**上完全不同：

- notices 是**快照**，A 每跑一次就整个重写；今天的和昨天的没有可比性
- events 是**只追加**，昨天写的"新公告"事件今天还在，明年还在

所以 `events.jsonl` **不能**用 `NoticeStore` 那套（它按 id 建索引、做投影、算排序键）——
这里要的是"原样读出这些事件，按时间排"，任何多余的加工都会让
"这条事件当时是什么"变得不可考。

## 三个诚实要求

1. **文件不存在 ≠ 没有事件。** 前者是 `source_kind="none"`（管道还没写过），
   后者是 `source_kind="events"` 且列表为空（写了，但今天确实没新公告）。
   两种都返回"0 条"，但用户该看到的话完全不同 —— 这是本项目的头号老毛病。
2. **样例不许自动回退。** `?source=sample` 是**调用方显式要的**，
   响应里 `source.kind="sample"` 会告诉前端"你在看演示数据"。
   自动回退会让"今天没有新公告"悄悄显示成"有一堆新公告"。
3. **坏行跳过来、但要记下来。** 沿用 §15.1 的口径：不因为一行坏了就打不开，
   也不许"看起来完全正常"。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from server import freshness

# 事件类型（与 contracts/events.schema.json 的 enum 一致）
EVENT_TYPES = ("added", "deadline_changed", "attachment_added")

# 这三个字段缺了就**不能用** —— 契约 §16 开头列的"读事件时必须知道的四件事"。
REQUIRED_EVENT_FIELDS = ("event_id", "type", "notice_id", "url")


class EventStore:
    """只追加的事件流。按 `(mtime_ns, size)` 按需重载（和 `live.py` 同一个思路：
    重跑管道之后不用重启服务，接口自己跟上）。
    """

    def __init__(self, path, records, source_kind, bad_lines=None, loaded=None,
                 path_exists=None, written_at=None):
        self.path = path
        self.records = records
        # "events"（真实文件）/ "sample"（演示样例）/ "none"（文件不存在）
        self.source_kind = source_kind
        self.bad_lines = bad_lines or []
        self._loaded = loaded
        self.path_exists = path_exists
        # 文件最后一次被写的时刻（`EventStore.load` 从 mtime 填；测试可注入）。
        # 只在"一条事件都没有"时用来回答"管道上次跑是什么时候" ——
        # 有事件时用事件自己的 `at` 更准（见 `data_time`）。
        self.written_at = written_at

    # ---------- 加载 ----------

    @classmethod
    def load(cls, path):
        """真实事件流。文件不存在时 `source_kind="none"` —— **不是错误**。"""
        if not path or not os.path.exists(path):
            return cls(path, [], "none", path_exists=False)
        records, bad = _read_jsonl(path)
        try:
            written = datetime.fromtimestamp(os.stat(path).st_mtime)
        except OSError:
            written = None
        return cls(path, records, "events", bad_lines=bad, path_exists=True,
                   written_at=written)

    @classmethod
    def load_sample(cls, path):
        """演示样例（`contracts/fixtures/events.sample.json`）。

        **只在 `?source=sample` 时被调用。** 形状是
        `{"_note": …, "events": [...]}`，`_` 开头的键是给人看的标注，不是契约。
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        records = data.get("events") or []
        return cls(path, records, "sample", path_exists=True)

    # ---------- "这批事件代表什么时候的数据" ----------

    def data_time(self):
        """这批事件代表**什么时候的事实**。推送必须知道这个（否则旧数据会伪装成新闻）。

        优先级：**最晚事件的 `at`** → 文件 mtime → `None`。

        为什么 `at` 优先而不是 mtime：`at` 是"我们检测到变化"的时刻，语义就是答案；
        而 mtime 在"补数据/重算"时会变新，那时它会说"数据是刚刚的"——
        可事实是我们刚刚重新生成了一份旧数据。**补抓事件恰恰是这种形态**。

        没有事件时才退回 mtime：那是"管道跑过、但这一轮 0 条"，mtime 是唯一的证据。
        """
        times = []
        for e in self.records:
            at = freshness._parse_dt(e.get("at"))
            if at is not None:
                times.append(at)
        if times:
            return max(times)
        return self.written_at

    def meta(self, now=None):
        """`push.preview` 响应里的 `source` 块。

        **`note` 是给人看的**，而且要能区分几种情况 —— 前端直接显示它，
        不要自己再判断一遍（判断写两处，迟早一处对一处错）。

        ## 这个块存在的唯一理由

        "今天没有新公告"有两种完全不同的成因：

        | 成因 | 该说什么 |
        |---|---|
        | 管道跑了，今天真的没有 | 「今天没有新公告」 |
        | **管道几天没跑** | 「我们**没去看**，所以"没有"这个词现在说不了」 |

        推送的全部价值建立在"今天发生了什么"上。**数据旧了还照常说"今天"，那是把
        "我们没看"说成"没有"** —— 本项目反复在防的那类错误，只是这次藏在时间里。
        """
        now = now or datetime.now()
        data_time = self.data_time()
        age = freshness._age_hours(data_time, now)
        text = freshness.humanize_age(age)

        # `stale` 只在"真实事件流"和"没有文件"时有意义。
        # 演示样例（sample）的"年龄"没有意义 —— 它不是数据，是道具。
        stale = None
        if self.source_kind == "events":
            stale = age is None or age > freshness.STALE_AFTER_HOURS
        elif self.source_kind == "none":
            stale = True

        if self.source_kind == "none":
            note = ("今天没有新公告：事件文件还没被写出来（数据管道今天没跑，"
                    "或者跑了但一条变更都没有）。这不是坏了。")
        elif self.source_kind == "sample":
            note = ("**演示样例事件**，不是今天真实抓到的。"
                    "页面上必须标出来，否则就是用假输入冒充真结果。")
        elif not self.records:
            note = "事件文件读到了，但里面一条事件都没有（今天确实没有新公告）。"
        elif stale:
            # **这句是这一块最重要的**：数据旧的时候，不许说"今天没有新公告"。
            note = ("**数据是 %s**（超过 %g 小时），说明抓取管道这几天没跑 —— "
                    "所以「今天有没有新公告」我们**不知道**，不是「没有」。"
                    % (text, freshness.STALE_AFTER_HOURS))
        else:
            note = "读到 %d 条事件，数据是 %s。" % (len(self.records), text)

        out = {
            "kind": self.source_kind,
            "path": _rel(self.path),
            "total": len(self.records),
            "data_time": data_time.isoformat(timespec="seconds") if data_time else None,
            "data_age_hours": age,
            "data_age_text": text,
            "stale": stale,
            "stale_after_hours": freshness.STALE_AFTER_HOURS,
            "note": note,
        }
        if self.bad_lines:
            out["bad_lines"] = self.bad_lines[:5]
            out["bad_lines_total"] = len(self.bad_lines)
        return out

    def events(self):
        """事件列表，**新的在前**。

        排序键 `(detected_on, at, event_id)` 降序 —— 带 `event_id` 是为了**确定**：
        两个事件同一天同一秒时，不能靠字典序的偶然决定谁在前面
        （门禁会因为"上一次跑的顺序"和"这一次不同"而红，那时没人知道是代码变了）。
        """
        return sorted(
            self.records,
            key=lambda e: (e.get("detected_on") or "", e.get("at") or "",
                           e.get("event_id") or ""),
            reverse=True,
        )


def _read_jsonl(path):
    """读 JSONL，坏行跳过并记下（沿用 §15.1 的口径：能用，但要响）。"""
    records, bad = [], []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                bad.append({"line": lineno, "error": f"不是合法 JSON：{e.msg}",
                            "excerpt": line[:80]})
                continue
            if not isinstance(rec, dict):
                bad.append({"line": lineno, "error": "不是 JSON 对象",
                            "excerpt": line[:80]})
                continue
            missing = [k for k in REQUIRED_EVENT_FIELDS if not rec.get(k)]
            if missing:
                bad.append({"line": lineno, "error": f"缺必填字段 {missing}",
                            "excerpt": line[:80]})
                continue
            if rec.get("type") not in EVENT_TYPES:
                bad.append({"line": lineno,
                            "error": f"未知事件类型 {rec.get('type')!r}",
                            "excerpt": line[:80]})
                continue
            records.append(rec)
    return records, bad


def _rel(path):
    """给响应里的路径用相对形式（绝对路径不该出现在 API 响应里）。"""
    if not path:
        return path
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:              # 跨盘符时 relpath 会抛
        return path

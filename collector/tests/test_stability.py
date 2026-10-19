# -*- coding: utf-8 -*-
"""日更稳定性（`stability.jsonl` + `history/index.json`）的离线测试。不依赖 pytest：

    cd collector && python tests/test_stability.py

**为什么这个文件必须先存在再跑真流程**：这一层负责的是"**每天更新**"这句承诺，
而它是整个产品里**唯一无法靠一次演示验证**的东西 —— 它只认**连续性**。
一旦 `stability.jsonl` 里混进一条"看起来成功、其实没跑到"的记录，
那句承诺就变成了自我安慰，而且**没有任何人看得出来**。

所以这里重点测的不是"能不能写文件"，而是：
  · 抓取没产出时，那一轮**不许**被记成"更新了"（`data_changed`）
  · 连续天数**断了要看得见**
  · 索引里 `reparsed` 那一列**真的在**（B 提了 5 次）
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import common        # noqa: E402
import run_daily as rd   # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        期望 {want!r}  实际 {got!r}")
        FAILED.append(name)


def check_true(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n        {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


class Sandbox:
    """把 `common.OUT_DIR` / HIST / STABILITY 全指到临时目录。

    **测试不许读真实数据** —— 这个项目栽过 5 次"断言绑了磁盘上恰好是什么数据"，
    最近一次是 `low[0]`（第一条恰好是哪条）。
    """

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="stab-")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out, exist_ok=True)
        self.HIST = os.path.join(self.out, "history")
        self.LOG = os.path.join(self.out, "runs")
        self.STABILITY = os.path.join(self.out, "stability.jsonl")
        self._saved = (rd.common.OUT_DIR, rd.HIST, rd.LOG, rd.STABILITY)

    def __enter__(self):
        rd.common.OUT_DIR = self.out
        rd.HIST = self.HIST
        rd.LOG = self.LOG
        rd.STABILITY = self.STABILITY
        return self

    def __exit__(self, *a):
        rd.common.OUT_DIR, rd.HIST, rd.LOG, rd.STABILITY = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_notices(self, recs):
        p = os.path.join(self.out, "notices.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    def write_index(self, days):
        os.makedirs(self.HIST, exist_ok=True)
        with open(os.path.join(self.HIST, "index.json"), "w", encoding="utf-8") as f:
            json.dump({"days": days}, f, ensure_ascii=False)

    def append_stability(self, lines):
        with open(self.STABILITY, "a", encoding="utf-8") as f:
            for ln in lines:
                f.write((ln if isinstance(ln, str) else
                         json.dumps(ln, ensure_ascii=False)) + "\n")


def _steps(*exits):
    return [{"step": f"步{i}", "exit": e, "seconds": 1.0} for i, e in enumerate(exits)]


print("== fingerprint：这一轮到底有没有真跑到新数据 ==")
with Sandbox() as sb:
    p = sb.write_notices([{"id": "a"}, {"id": "b"}])
    f1 = rd.fingerprint()
    check_true("有文件时给得出 sha1", bool(f1 and f1["sha1"]), str(f1))
    check("同一个文件两次指纹相同", rd.fingerprint()["sha1"], f1["sha1"])
    sb.write_notices([{"id": "a"}, {"id": "b"}, {"id": "c"}])
    check_true("文件变了指纹就变", rd.fingerprint()["sha1"] != f1["sha1"])
    os.remove(p)
    check("文件不存在时返回 None（不崩）", rd.fingerprint(), None)

print("\n== fingerprint 只认内容：crawled_at 每轮都在变，它不算数据变了 ==")
# 这一节钉的是 2026-10-17 发现的坑：整文件 sha1 → 每轮必变 → data_changed 恒真，
# 于是"这一轮一条都没变"的告警和 no_new_data 从来不触发（一天都没工作过）。
with Sandbox() as sb:
    base = {"id": "a", "url": "https://x.edu/1.htm", "publish_date": "2026-10-01",
            "content_hash": "sha1:111", "crawled_at": "2026-10-17T10:00:00",
            "field_state": {"deadline": "found"}}
    tail = {"id": "b", "url": "https://x.edu/2.htm", "publish_date": "2026-10-02",
            "content_hash": "sha1:222", "crawled_at": "2026-10-17T10:00:00",
            "field_state": {"deadline": "absent"}}
    sb.write_notices([base, tail])
    f1 = rd.fingerprint()
    check("指纹里带条数", f1["records"], 2)

    # 只改 crawled_at —— crawl.py 每轮都会重写它
    sb.write_notices([{**base, "crawled_at": "2026-10-18T09:00:00"},
                      {**tail, "crawled_at": "2026-10-18T09:00:00"}])
    check("**只有 crawled_at 变 → 指纹不变**（否则 data_changed 恒真）",
          rd.fingerprint()["sha1"], f1["sha1"])

    # 正文真变了
    sb.write_notices([{**base, "content_hash": "sha1:999", "crawled_at": "2026-10-18T09:00:00"},
                      {**tail, "crawled_at": "2026-10-18T09:00:00"}])
    check_true("正文哈希变 → 指纹变", rd.fingerprint()["sha1"] != f1["sha1"])

    # 正文没变，但我们读到了截止日（unknown → found）
    sb.write_notices([{**base, "field_state": {"deadline": "unknown"},
                       "crawled_at": "2026-10-18T09:00:00"},
                      {**tail, "crawled_at": "2026-10-18T09:00:00"}])
    check_true("截止日三态变了 → 指纹也变（正文没变也算数据变了）",
               rd.fingerprint()["sha1"] != f1["sha1"])

    # 行序变了、内容没变
    sb.write_notices([{**tail, "crawled_at": "2026-10-18T09:00:00"},
                      {**base, "crawled_at": "2026-10-18T09:00:00"}])
    check("行序变了不算数据变", rd.fingerprint()["sha1"], f1["sha1"])

with Sandbox() as sb:
    sb.write_notices([{"id": "a", "school": "X"}])
    # 让 archive 能读到一份"差异报告"
    with open(os.path.join(sb.out, "changes.json"), "w", encoding="utf-8") as f:
        json.dump({"counts": {"added": 2, "changed": 1, "reparsed": 7,
                              "removed": 0, "reidentified": 0}}, f)
    counts, n = rd.archive(date(2026, 10, 17))
    check("归档条数", n, 1)
    idx = json.load(open(os.path.join(sb.HIST, "index.json"), encoding="utf-8"))
    day = idx["days"][0]
    check_true("**(关键) days[] 里有 reparsed 列**", "reparsed" in day, str(sorted(day)))
    check("reparsed 值如实透传", day["reparsed"], 7)
    check("date 正确", day["date"], "2026-10-17")

    # 同一天重跑：覆盖，不重复
    rd.archive(date(2026, 10, 17))
    idx = json.load(open(os.path.join(sb.HIST, "index.json"), encoding="utf-8"))
    check("同一天重跑不产生两条", len(idx["days"]), 1)

print("\n== archive：notices.jsonl 不存在时不许把整个流程带崩 ==")
with Sandbox() as sb:
    counts, n = rd.archive(date(2026, 10, 17))   # 没有 notices.jsonl
    check("没有数据文件也能归档，条数为 0", n, 0)
    idx = json.load(open(os.path.join(sb.HIST, "index.json"), encoding="utf-8"))
    check("索引仍然写出来了", idx["days"][0]["records"], 0)

print("\n== record_stability：追加、形状、data_changed ==")
with Sandbox() as sb:
    a = {"sha1": "AAA"}
    b = {"sha1": "AAA"}      # 没变
    line = rd.record_stability(date(2026, 10, 17), __import__("datetime").datetime(2026, 10, 17, 9, 0),
                               12.5, 196, {"added": 0, "changed": 0, "reparsed": 5,
                                           "removed": 0, "reidentified": 0},
                               _steps(0, 0, 0), a, b, data_changed=False)
    arr = [json.loads(x) for x in open(sb.STABILITY, encoding="utf-8") if x.strip()]
    check("追加了一行", len(arr), 1)
    check("data_changed 如实记 False", arr[0]["data_changed"], False)
    check("failed_steps 为空", arr[0]["failed_steps"], [])
    check("ok=True（步骤都成功）", arr[0]["ok"], True)
    check("counts 带回 reparsed", arr[0]["counts"]["reparsed"], 5)

    rd.record_stability(date(2026, 10, 17), __import__("datetime").datetime(2026, 10, 17, 18, 0),
                        9.0, 197, {"added": 1}, _steps(0, 1), {"sha1": "AAA"},
                        {"sha1": "BBB"}, data_changed=True)
    arr = [json.loads(x) for x in open(sb.STABILITY, encoding="utf-8") if x.strip()]
    check("**追加而不是覆盖**（同一天两轮留两条）", len(arr), 2)
    check("失败步被记下来", arr[1]["failed_steps"], ["步1"])
    check("有失败时 ok=False", arr[1]["ok"], False)

print("\n== continuity：'每天更新'的凭据 ==")
with Sandbox() as sb:
    c = rd.continuity(date(2026, 10, 17))
    check("没有文件时 runs=0（不崩）", c["runs"], 0)
    check("没有文件时 streak=0", c["streak"], 0)

with Sandbox() as sb:
    today = date(2026, 10, 17)
    D = today.isoformat()
    Y1 = (today - timedelta(days=1)).isoformat()
    Y2 = (today - timedelta(days=2)).isoformat()
    sb.append_stability([
        {"day": Y2, "ok": True, "data_changed": True},
        {"day": Y1, "ok": True, "data_changed": True},
        {"day": D, "ok": True, "data_changed": True},
    ])
    c = rd.continuity(today)
    check("连续 3 天 → streak=3", c["streak"], 3)
    check("runs=3", c["runs"], 3)

with Sandbox() as sb:
    today = date(2026, 10, 17)
    D = today.isoformat()
    Y1 = (today - timedelta(days=1)).isoformat()
    Y4 = (today - timedelta(days=4)).isoformat()
    sb.append_stability([
        {"day": Y4, "ok": True, "data_changed": True},
        {"day": Y1, "ok": True, "data_changed": True},
        {"day": D, "ok": True, "data_changed": True},
    ])
    c = rd.continuity(today)
    check("中间断了 3 天 → 只数最近的", c["streak"], 2)
    check("但天数照实记 3 天", c["days"], 3)

with Sandbox() as sb:
    today = date(2026, 10, 17)
    Y1 = (today - timedelta(days=1)).isoformat()
    sb.append_stability([{"day": Y1, "ok": True, "data_changed": True}])
    c = rd.continuity(today)
    check("**今天还没跑时，昨天那天也算连续**（不该显示 0）", c["streak"], 1)

with Sandbox() as sb:
    today = date(2026, 10, 17)
    D = today.isoformat()
    sb.append_stability([
        {"day": D, "ok": True, "data_changed": False},
        {"day": D, "ok": True, "data_changed": False},
    ])
    c = rd.continuity(today)
    check("跑成功但产出全没变 → 记进 no_new_data", c["no_new_data"], [D])
    check("但它仍然算跑了（streak 不因此归零）", c["streak"], 1)

print("\n== continuity：半截行不该毁掉整份报表 ==")
# 上次写到一半被打断（断电 / 强杀）会留下半行 JSON。
# 如果这里抛异常，"每天更新"的凭据就在第一次意外之后永久坏掉。
with Sandbox() as sb:
    today = date(2026, 10, 17)
    D = today.isoformat()
    sb.append_stability([
        {"day": D, "ok": True, "data_changed": True},
        '{"day": "2026-10-17", "ok": tr',        # 半截行
    ])
    c = rd.continuity(today)
    check("半截行被跳过，好记录还在", c["runs"], 1)
    check("streak 仍然算得出来", c["streak"], 1)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

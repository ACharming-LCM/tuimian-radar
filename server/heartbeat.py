# -*- coding: utf-8 -*-
"""数据心跳：**"我们最近跑成功了吗"**（契约 §21）。

## 为什么需要这个

定时任务上线之后，出现了一类新的沉默：**任务没跑，页面上一切正常**。
接口 200、形状对、数据是上一次的 —— 它不说谎，它只是过时（`store.py` 的
模块 docstring 里记过同一类 bug）。

`/api/freshness`（§12）回答的是"每所学校上次抓到什么时候"，
`/api/health/data` 回答的是"**我们自己的运行有没有成功**"。两件事：
前者是数据源的状况，后者是我们这条流水线的状况。

## 数据从哪来（以及拿不到时怎么办）

| 字段 | 来源 |
|---|---|
| `last_run` / `runs_24h` / `consecutive_ok` | `data/logs/fast.log`（和 `full.log`，如果存在）的起止标记 |
| `last_full_at` | `data/out/stability.jsonl`（全量轮次的历史记录） |
| `notices` / `events` / `fresh_within_*` | `data/out/notices.jsonl` + `events.jsonl` |

**读不到就给 `ok: null` 并且明说，绝不用 `false` 冒充，也不用 `true` 蒙混。**
这条和 §14 的三态是同一条原则：`false` 的意思是"我们查过，失败了"，
而"日志里没有这一轮"是"我们不知道"。

## 退出码可能不存在

日志是 `run_fast.cmd` 用 `>>` 追加的。那份脚本会写 `crawl exit=%RC%`，
但**实测 2026-10-18 的 fast.log 里一条 `exit=` 都没有**（那一轮不是从脚本起的）。
所以判定顺序是：**显式退出码 → 结构性证据 → `null`**。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOGS_DIR = os.environ.get("TUIMIAN_LOGS", os.path.join(ROOT, "data", "logs"))
STABILITY = os.environ.get(
    "TUIMIAN_STABILITY", os.path.join(ROOT, "data", "out", "stability.jsonl"))
NOTICES = os.environ.get(
    "TUIMIAN_NOTICES", os.path.join(ROOT, "data", "out", "notices.jsonl"))
EVENTS = os.environ.get(
    "TUIMIAN_EVENTS", os.path.join(ROOT, "data", "out", "events.jsonl"))

# 缓存 TTL（秒）。**为什么要有**：这个接口挂在页头上，前端切页/轮询都会打它，
# 而它每次都要把 `notices.jsonl`（2751 条、约 10 MB）整个解析一遍 —— 实测 218ms。
# 页头每几秒重算一次全库统计，是拿用户体验换"绝对实时"，而这里的事实
# （跑没跑成功、多少条）本来就不可能秒级变化。
#
# 3 秒的理由：比页头轮询间隔短（用户感觉不到旧），又足以把连续请求合并成一次计算。
HEARTBEAT_TTL_S = float(os.environ.get("TUIMIAN_HEARTBEAT_TTL", "3"))
_CACHE = {"t": 0.0, "payload": None, "key": None}

_RUN_START = re.compile(r"\[([^\]]*)\]\s*=====\s*(fast|full)\s+start\s*=====")
_RUN_END = re.compile(r"\[([^\]]*)\]\s*=====\s*(fast|full)\s+end\s*=====")
_EXIT = re.compile(r"\[\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}[.\d]*\]\s*(\w+)\s+exit=(-?\d+)")
# `crawl.py` 走到最后才打的一行。没有退出码时用它当"这轮跑完了"的结构性证据。
_RECORDS_LINE = re.compile(r"记录\s*\d+\s*条\s*->")
_NEW_URLS = re.compile(r"新增\s*URL\s*(\d+)\s*条")
_NEW_EVENTS = re.compile(r"追加\s*(\d+)\s*条")

# 没有 end 标记时，开始到现在多久以内算"**正在跑**"（而不是"中途退了"）。
# 取正常 fast 轮次耗时（约 130 秒）的十倍：足够容纳慢站点，
# 又能在真的挂掉时（下一轮早该起来了）及时变红。
RUN_IN_PROGRESS_GRACE = 20 * 60

# 日志里的时间形如 `周日 2026/10/18 11:41:03.59`。
#
# **不能用 `strptime("%a ...")`**：`%a` 认的是英文星期缩写（Sun），
# 而 `run_fast.cmd` 里的 `%date%` 给的是中文「周日」。实测那样解析会**静默返回 None**，
# 于是 `at` / `seconds` / `runs_24h` 全变 null —— 接口看起来"没数据"，
# 其实是解析器不认识那行。所以直接抓数字，不碰星期几。
_TIME_RE = re.compile(
    r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?")


def _parse_time(s):
    m = _TIME_RE.search(s or "")
    if not m:
        return None
    y, mo, d, hh, mm, ss, frac = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    try:
        return datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), micro)
    except ValueError:
        return None


def parse_runs(log_path):
    """切出一轮一轮：起止时间、各步退出码、轮次内正文。

    **"有 start 没有 end" 也算一轮**（`ended=False`）：`run_fast.cmd` 在
    `crawl` 失败时会 `exit /b`，那一轮就不会打 end 标记。那种轮次是最该被看见的。
    """
    if not os.path.exists(log_path):
        return []
    runs, cur = [], None
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _RUN_START.search(line)
            if m:
                cur = {"kind": m.group(2), "started": _parse_time(m.group(1)),
                       "ended": False, "exits": {}, "body": [],
                       "start_line": line.strip()}
                runs.append(cur)
                continue
            if cur is None:
                continue
            m = _RUN_END.search(line)
            if m:
                cur["ended"] = True
                cur["ended_at"] = _parse_time(m.group(1))
                cur["end_line"] = line.strip()
                continue
            m = _EXIT.search(line)
            if m:
                cur["exits"][m.group(1)] = int(m.group(2))
                continue
            cur["body"].append(line.rstrip("\n"))
    for r in runs:
        started, ended = r.get("started"), r.get("ended_at")
        r["seconds"] = (round((ended - started).total_seconds(), 1)
                        if started and ended else None)
        r["ok"] = _run_ok(r)
        r["new_urls"] = _first_int(_NEW_URLS, r["body"])
        r["new_events"] = _first_int(_NEW_EVENTS, r["body"])
    return runs


def _first_int(pattern, lines):
    for line in lines:
        m = pattern.search(line)
        if m:
            return int(m.group(1))
    return None


def _run_ok(run):
    """`True` / `False` / **`None`（不知道）**。三态，不许二值化。

    顺序：显式退出码 → 结构性证据 → `None`。
    """
    if not run.get("ended"):
        # 没跑到 end：这是"中途退了"，是明确的事实，不是"不知道"
        return False
    if run["exits"]:
        return all(v == 0 for v in run["exits"].values())
    if any(_RECORDS_LINE.search(x) for x in run["body"]):
        return True          # 走到了最后一行 —— 结构性证据
    return None              # 既没退出码也没那行：**不知道**


def all_runs(logs_dir=None, now=None):
    """fast + full 两本日志合并，按开始时间升序，并**判好每一轮的状态**。

    `logs_dir` 可注入 —— 门禁要造"两轮失败"的日志，但**不能写进真实的
    `data/logs/`**（那是 A 的目录，而且会污染下一轮的 `consecutive_ok`）。

    **"正在跑"和"中途退了"必须分开。**
    2026-10-18 实测踩到：13:03 又起了一轮 fast，日志最后一行是 start ——
    第一版把这个当成"没有 end 标记 = 中途退了"，于是定时任务每跑一次、
    门禁就红一次，而它其实运行得好好的。**"在跑"是一条正常状态，不是故障。**

    判据：没有 end 标记时看**开始到现在有多久** —— grace 内算"正在跑"
    （`in_progress=True`，`ok=None`），超过 grace 才算真的中途退了（`ok=False`）。
    """
    now = now or datetime.now()
    base = logs_dir or LOGS_DIR
    runs = []
    for name in ("fast.log", "full.log"):
        runs.extend(parse_runs(os.path.join(base, name)))
    runs.sort(key=lambda r: r.get("started") or datetime.min)
    for r in runs:
        if r.get("ended"):
            r["in_progress"] = False
            continue
        started = r.get("started")
        if started and (now - started).total_seconds() <= RUN_IN_PROGRESS_GRACE:
            r["in_progress"] = True
            r["ok"] = None            # 在跑 = 还不知道结果
        else:
            r["in_progress"] = False
            r["ok"] = False           # 起了手、过了 grace 还没结束 = 中途退了
    return runs


def last_full_at():
    """全量轮次的最后时间：优先 `stability.jsonl`，其次 `full.log` 的 end 标记。"""
    if os.path.exists(STABILITY):
        last = None
        with open(STABILITY, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except ValueError:
                    continue
        if last and last.get("at"):
            return last["at"]
    fulls = [r for r in parse_runs(os.path.join(LOGS_DIR, "full.log")) if r.get("ended_at")]
    if fulls:
        return fulls[-1]["ended_at"].isoformat(timespec="seconds")
    return None


def _load_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _age_days(publish_date, today):
    s = (publish_date or "").strip()
    if not s:
        return None
    try:
        return (today - date.fromisoformat(s[:10])).days
    except ValueError:
        return None


def summary(now=None, logs_dir=None, notice_records=None, event_records=None):
    """`GET /api/health/data` 的载荷（契约 §21）。

    `logs_dir` 可注入：门禁要造"两轮失败"的日志，但**不能写进真实的 `data/logs/`**
    （那是 A 的目录，而且会污染下一轮的 `consecutive_ok`）。

    `notice_records` / `event_records` 也是注入点，**目的是省时间**：
    接口层手上已经有加载好的记录（`live_store.records`），没必要把 10 MB 的
    `notices.jsonl` 再解析一遍 —— 那一次就是 200 ms。传进来就跳过读文件。

    **只对"真实路径 + 真实时钟"的调用走缓存**（`now is None and logs_dir is None`）：
    门禁注入的调用必须每次真算，否则时间相关的断言会被上一次的结果污染。
    """
    use_cache = now is None and logs_dir is None
    if use_cache:
        hit = _CACHE.get("payload")
        if hit is not None and (time.monotonic() - _CACHE["t"]) < HEARTBEAT_TTL_S:
            return dict(hit)          # 浅拷贝：调用方改顶层键不会污染缓存

    now = now or datetime.now()
    today = now.date()

    runs = all_runs(logs_dir, now)
    since = now - timedelta(hours=24)
    recent = [r for r in runs if r.get("started") and r["started"] >= since]

    # 连续成功：从最后一轮往回数，遇到"不是 True"就停（`None` 也停 —— 不知道就别算成功）
    # **正在跑既不计数、也不打断连胜**：它还没有结果。
    # 打断的话，每次定时任务在跑的那几十秒里 consecutive_ok 都会掉成 0，
    # 于是这个数在半夜看是 12、白天看是 0 —— 一个自己会抖的指标没人会信。
    consecutive = 0
    for r in reversed(runs):
        if r.get("in_progress"):
            continue
        if r.get("ok") is True:
            consecutive += 1
        else:
            break

    last = runs[-1] if runs else None
    last_run = None
    note = None
    if last is None:
        note = ("读不到任何运行记录（data/logs/ 下没有 fast.log / full.log）—— "
                "这不等于失败，是**不知道**。定时任务可能还没跑过。")
    else:
        last_run = {
            "at": (last["started"].isoformat(timespec="seconds")
                   if last.get("started") else None),
            "kind": last.get("kind"),
            # **三态**：True / False / None
            "ok": last.get("ok"),
            "seconds": last.get("seconds"),
            "new_notices": last.get("new_urls"),
            "new_events": last.get("new_events"),
        }
        if last.get("ok") is None:
            note = ("最近一轮既没有 `crawl exit=` 标记、也没有 `记录 N 条 ->` 那一行 —— "
                    "**无法判断成功与否**，所以 ok 是 null 而不是 true/false。")

    notices = notice_records if notice_records is not None else _load_jsonl(NOTICES)
    events = event_records if event_records is not None else _load_jsonl(EVENTS)
    ages = [_age_days(r.get("publish_date"), today) for r in notices]
    ages = [a for a in ages if a is not None and a >= 0]

    data_age_minutes = None
    if last is not None and last.get("ended_at"):
        data_age_minutes = int((now - last["ended_at"]).total_seconds() // 60)

    payload = {
        "last_run": last_run,
        "runs_24h": len(recent),
        "runs_24h_failed": sum(1 for r in recent if r.get("ok") is False),
        "runs_24h_unknown": sum(1 for r in recent if r.get("ok") is None),
        "consecutive_ok": consecutive,
        "last_full_at": last_full_at(),
        "notices": len(notices),
        "events": len(events),
        "fresh_within_3d": sum(1 for a in ages if a <= 3),
        "fresh_within_7d": sum(1 for a in ages if a <= 7),
        "data_age_minutes": data_age_minutes,
        "_note": note or ("ok=null 表示**我们不知道这一轮成功没有**，不是失败；"
                          "runs_24h_unknown 是这个口径的计数。"),
    }
    if use_cache:
        _CACHE["payload"] = payload
        _CACHE["t"] = time.monotonic()
    return payload

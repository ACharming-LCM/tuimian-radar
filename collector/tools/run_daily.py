# -*- coding: utf-8 -*-
"""每天跑一次的完整流程，并把当天的结果**留档**。

    cd collector && python tools/run_daily.py

为什么必须留档：产品的头号卖点是「每天更新」，但在此之前我们**只有"最新一次"** ——
`changes.json` 每次抓取都被覆盖，没有历史。所以：
  · 演示时被问"你怎么证明每天在跑"，我只有一份 0/0/0；
  · 想看"过去 7 天累计新增多少"也没有依据。

现在起每天归档两份东西（都很小）：
  data/out/history/<日期>.jsonl  当天的**精简快照**（约 25 KB）
     只留身份和变更相关字段，够用来和任何一天做 diff，也是"那天我们确实跑过"的证据
  data/out/history/<日期>.json   当天的差异报告
  data/out/history/index.json    索引：每天几号、多少条、增删改多少

**为什么要跑得早**：历史是攒出来的。今晚跑一次，明天演示就多一天。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)
sys.path.insert(0, COLLECTOR)
sys.path.insert(0, HERE)

import common        # noqa: E402

HIST = os.path.join(common.OUT_DIR, "history")
LOG = os.path.join(common.OUT_DIR, "runs")
STABILITY = os.path.join(common.OUT_DIR, "stability.jsonl")
PY = sys.executable

# 精简快照留哪些字段：够 diff 用（身份 + 变更相关），不塞 body_text。
# **必须带上 `extract`** —— 里面的 version 是"变更归因"的依据：
# 版本不同说明正文差异可能来自我们换了抽取方式，而不是学校改了通知。
# 漏掉它的代价是实测出来的：跨天对比时归因失效，67 个 PDF 的正文改动
# 会被日报说成"学校改了 67 份通知"。
SLIM = ("id", "school", "unit", "title", "publish_date", "url", "deadline",
        "apply_mode", "target_year", "attachments", "content_hash", "extract")


def _row_key(rec):
    """指纹里一条记录取哪几个字段。

    **必须排除 `crawled_at`**：`crawl.py` 每轮都给它重写"这次抓到的时间"，
    只要它在，指纹就每轮必变，`data_changed` 就恒为真。字段选取理由：
      · `id` / `url` / `publish_date`：同一条公告的身份；
      · `content_hash`：正文真的变了没有（学校改了通知 / 我们换了抽取器）；
      · `field_state.deadline`：截止日从 `unknown` 变成 `found` 也算数据变了，
        而它**不**改 `content_hash`（正文没变，是我们读到了）。
    """
    fs = rec.get("field_state") or {}
    if not isinstance(fs, dict):
        fs = {}
    return [rec.get("id"), rec.get("url"), rec.get("publish_date"),
            rec.get("content_hash"), fs.get("deadline")]


def fingerprint():
    """这一轮的数据和上一轮比，**内容**到底变没变（不是文件字节变没变）。

    为什么需要它（这是"每天更新"这个承诺最容易假的地方）：
    `crawl.py` 在"一条都没抓到"时会**保护已有数据不写盘**（这是对的，防止冒烟模式
    把昨天的数据清空）。但那意味着 `run_daily.py` 接下来读到的 `notices.jsonl`
    是**昨天那份**，而它会把这份旧数据当成"今天抓到的 N 条"归档进
    `history/<今天>.jsonl`，于是 `index.json` 里多出一条**看起来完全正常的一天**。
    所以跑之前拍一张、跑之后拍一张，**内容**真的变了才算这一天真的跑过。

    坑（已踩，2026-10-17，代价是这条告警整整一天没工作过）：原来是对
    `notices.jsonl` **整文件**算 sha1。但 `crawl.py` 每轮都会重写每条记录的
    `crawled_at`，所以整文件每轮必变。实测：0 增 / 0 改 / 0 重解析的那一轮，
    `git diff --stat` 仍然是 **284 增 / 286 删**，而 `data_changed` 照样是 True。

    后果两个，都很隐蔽：
      · `run_daily` 那句"这一轮产出一个字节都没变"**从来不触发** ——
        抓取静默失败（改版、封禁、选择器失配）时它也不触发，正好漏掉最该报的那次；
      · `stability.jsonl` 的 `no_new_data` 永远是空的 ——
        而那是"每天更新"这句承诺反面的唯一证据。

    一个恒真的指标比没有指标更危险：它会让人以为"每天都在变"。
    现在改成对**内容投影**算哈希（见 `_row_key`），并排好序，与行序无关。
    """
    p = os.path.join(common.OUT_DIR, "notices.jsonl")
    if not os.path.exists(p):
        return None
    st = os.stat(p)
    rows = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(_row_key(json.loads(line)))
            except json.JSONDecodeError:
                # 半截行：给它一个独特的形状，别让它和别的坏行"看起来一样"
                rows.append(["<bad-line>", len(line), line[:40]])
    rows.sort(key=lambda r: [str(x) for x in r])
    h = hashlib.sha1(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
    return {"size": st.st_size, "sha1": h.hexdigest(), "records": len(rows)}


def run(step, args, timeout=7200):
    """跑一个子步骤。**输出走临时文件，不走管道。**

    坑（已踩，2026-10-18 A16，代价是 2 小时）：原来用 `capture_output=True`，
    等于给子进程接了一根 64KB 的匿名管道，而父进程要等它结束才读。
    抓取量大起来之后（8 页 / 60 详情），crawl 的 stdout 很快超过 64KB，
    **子进程写管道时被阻塞、父进程在等子进程退出** —— 死锁，进程 0.5 秒 CPU 挂两小时。
    现象很像"网络卡住"，其实是自己把自己堵死的。

    改用临时文件：子进程随便写，父进程结束后读尾部。顺带把超时提到 2 小时
    （8 页 × 199 栏的深抓本来就可能超过 1 小时）。
    """
    print(f"\n--- {step} ---")
    t0 = time.time()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".log", mode="w+",
                                      encoding="utf-8", errors="replace")
    try:
        with open(tmp.name, "w", encoding="utf-8", errors="replace") as f:
            r = subprocess.run([PY, *args], cwd=COLLECTOR, stdout=f,
                               stderr=subprocess.STDOUT, text=True, timeout=timeout)
        with open(tmp.name, encoding="utf-8", errors="replace") as f:
            out = f.read()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    dt = time.time() - t0
    tail = (out or "").strip().splitlines()[-6:]
    for line in tail:
        print(f"    {line}")
    if r.returncode != 0:
        print(f"    ❌ {step} 退出码 {r.returncode}")
        for line in (out or "").strip().splitlines()[-8:]:
            print(f"       {line}")
    else:
        print(f"    ✓ {step}  ({dt:.1f}s)")
    return {"step": step, "seconds": round(dt, 1), "exit": r.returncode,
            "tail": tail, "stderr_tail": (out or "").strip().splitlines()[-8:]}


def archive(today, data_changed=True):
    """把当天的差异和精简快照归档，并更新索引。"""
    os.makedirs(HIST, exist_ok=True)
    d = today.isoformat()

    # 差异报告
    src = os.path.join(common.OUT_DIR, "changes.json")
    counts = {}
    if os.path.exists(src):
        with open(src, encoding="utf-8") as f:
            ch = json.load(f)
        counts = ch.get("counts", {})
        with open(os.path.join(HIST, d + ".json"), "w", encoding="utf-8") as f:
            json.dump(ch, f, ensure_ascii=False, indent=1)

    # 精简快照。
    # 坑（已踩）：这里原来直接 open，**没判断文件在不在** —— 第一次跑、或者数据被
    # 清掉时，它会抛 FileNotFoundError 把整个 run_daily 带崩，而前面的抓取其实成功了。
    # 失败要落在具体那一步上，不能让"归档挂掉"看起来像"抓取挂掉"。
    recs = []
    notices_p = os.path.join(common.OUT_DIR, "notices.jsonl")
    if os.path.exists(notices_p):
        with open(notices_p, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    r = json.loads(ln)
                    recs.append({k: r.get(k) for k in SLIM})
        with open(os.path.join(HIST, d + ".jsonl"), "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 索引（同一天重跑就覆盖当天那条，不重复）
    idx_p = os.path.join(HIST, "index.json")
    idx = {"days": []}
    if os.path.exists(idx_p):
        with open(idx_p, encoding="utf-8") as f:
            idx = json.load(f)
    idx["days"] = [x for x in idx.get("days", []) if x.get("date") != d]
    day = {"date": d, "records": len(recs),
           "added": counts.get("added"), "changed": counts.get("changed"),
           "removed": counts.get("removed"),
           "reidentified": counts.get("reidentified"),
           # **`reparsed` 是 B 提了 5 次的那一列。** 没有它的时候
           # `days_with_changes` 看不见"只有 reparsed 的那天"，而两天的
           # added/changed 完全一样、区分是潜伏的（`server/history.py:92` 也记着这件事）。
           "reparsed": counts.get("reparsed")}
    idx["days"].append(day)
    idx["days"].sort(key=lambda x: x["date"])
    idx["_note"] = ("每天一条。这是「每天更新」的凭据，也是月度订阅的素材。"
                    "records 是当天抓到的条数；其余是相对上一次抓取的增删改。"
                    "reparsed 是「我们改了抽取代码」，不是学校改了通知 —— "
                    "绝不要把它算进给用户的提醒。")
    with open(idx_p, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    return counts, len(recs)


def record_stability(day, started, seconds, records, counts, steps,
                     fp_before, fp_after, data_changed):
    """给这一轮运行**追加**一条机器可读记录。

    为什么要单独一份，而不是只靠 `history/index.json`：
    索引是**每天一条**（同一天重跑会覆盖），它回答"这个产品覆盖了哪些天"。
    但"**它今天到底跑成没跑成**"需要的是**每一轮**的记录，包括失败的那几轮 ——
    而且必须能把"跑成功但一条没变"和"根本没跑到"分开。

    `data_changed` 就是那个开关，见 `fingerprint()` 的注释。

    追加而不是覆盖：容错、也留住了"同一天跑了两次"这类信息（第二次通常该是幂等的）。
    """
    line = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "day": day.isoformat(),
        "started": started.isoformat(timespec="seconds"),
        "seconds": round(seconds, 1),
        "records": records,
        # **这一轮产出到底变了没有。** False + 步骤全 0 = 最可疑的那种"假的一天"。
        "data_changed": data_changed,
        "data_sha1_before": (fp_before or {}).get("sha1"),
        "data_sha1_after": (fp_after or {}).get("sha1"),
        "counts": {k: counts.get(k) for k in
                   ("added", "changed", "reparsed", "removed", "reidentified")},
        "steps": [{"step": s["step"], "exit": s["exit"],
                   "seconds": s["seconds"]} for s in steps],
        "failed_steps": [s["step"] for s in steps if s["exit"] != 0],
        "ok": all(s["exit"] == 0 for s in steps),
    }
    os.makedirs(os.path.dirname(STABILITY), exist_ok=True)
    with open(STABILITY, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def continuity(today):
    """从 `stability.jsonl` 算「连续跑到今天多少天」。

    这是"每天更新"这个承诺**唯一能被验证的形态** —— 不是"我们跑过几次"，
    而是"从哪天起没断过"。演示时被问"你怎么证明每天在更新"，答案就是这一行。

    可疑的一天（`data_changed=False` 但步骤全成功）单独列出来：
    那天**看起来跑了**，其实数据一个字节没动 —— 它可能是真的没新公告，
    也可能是抓取静默失败。**这两种在报表上必须长得不一样。**
    """
    if not os.path.exists(STABILITY):
        return {"runs": 0, "days": 0, "streak": 0, "last": None,
                "suspicious": [], "note": "还没有 stability.jsonl（今天这一轮会是第 1 条）"}
    runs, by_day = 0, {}
    with open(STABILITY, encoding="utf-8") as f:
        for ln in f:
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue          # 半截行（上次写到一半被打断）不应毁掉整份报表
            runs += 1
            by_day.setdefault(r.get("day"), []).append(r)

    days = sorted(d for d in by_day if d)
    # 这一天**跑成功了，但产出一个字节没变**。
    #
    # 注意它**不等于失败** —— 学校可能真的没发新公告。它的用途是把两种
    # "没有变化"分开：一种是真的没有，一种是抓取静默出问题（比如某站改版后
    # 选择器失配，抓到的正文全变成 `generic`，条数却还凑得上）。
    # **这两种在报表上必须长得不一样**，否则"每天更新"就成了自我安慰。
    no_new_data = sorted(d for d, rs in by_day.items()
                         if rs and all(r.get("ok") and r.get("data_changed") is False
                                       for r in rs))
    # 连续天数：从最近一次运行往回数，不断就算
    streak = 0
    if days:
        cur = today
        # 允许"最近一次是昨天"（今天的还没跑时不该显示 0）
        if days[-1] != cur.isoformat() and days[-1] == (cur - timedelta(days=1)).isoformat():
            cur = cur - timedelta(days=1)
        s = set(days)
        while cur.isoformat() in s:
            streak += 1
            cur = cur - timedelta(days=1)
    return {"runs": runs, "days": len(days), "streak": streak,
            "last": days[-1] if days else None,
            "no_new_data": no_new_data, "note": None}


def main():
    ap = argparse.ArgumentParser()
    # **A28（2026-10-19）：默认走缓存，全量刷新必须显式传 `--refresh`。**
    # 原来默认就是 `--refresh`：这是每天凌晨 03:07 跑的东西，一旦机器没开、白天补跑，
    # 就会在工作时间把 37 个站点全量重抓一遍（2026-10-19 那次爬了 3 小时）。
    # 日更要的是"有没有新东西"，不是"把历史重读一遍"；全量刷新留给周更或人工。
    ap.add_argument("--refresh", action="store_true",
                    help="忽略缓存、全量重抓（慢；日更默认不用）")
    ap.add_argument("--no-refresh", action="store_true",
                    help=argparse.SUPPRESS)   # 兼容旧脚本：等价于默认行为
    ap.add_argument("--skip-fixtures", action="store_true",
                    help="不重出 fixtures（默认会重出，让 B/C 拿到当天的）")
    a = ap.parse_args()

    today = date.today()
    started = datetime.now()
    print(f"=== 每日流程  {started:%Y-%m-%d %H:%M:%S} ===")
    # 第一行就说清这一轮是什么模式 —— 补跑时看一眼日志就知道会不会打学校服务器。
    print("=== 本轮模式："
          + ("全量刷新（--refresh，忽略缓存）" if a.refresh else "缓存（只抓新 URL，默认）")
          + " ===")

    steps = []
    refresh = ["--refresh"] if a.refresh else []

    # 跑之前拍指纹，用来判断这一轮**到底有没有真跑到新数据**（见 fingerprint()）。
    fp_before = fingerprint()

    # **不再覆盖 `--details`。**
    # 原来这里写死 `--details 3`，而 `sites.json` 的 `details_per_board` 是 6 ——
    # 于是日更每轮只收到约 147 条，而基线是 196，**留存 75% 直接把写盘闸（80%）踩响**，
    # 每跑一次都失败一次。那不是闸的问题，是**日更和全量的配置本来就不是一个口径**。
    # 现在日更用配置里的默认值，和"全量跑一次"是同一个产出规模，闸才有意义。
    # `--max-stale-days 0`：**关掉"400 天过滤"**。
    # A12 外部对照量出：三校漏的 395 条里 127 条是被这个过滤丢掉的，
    # 而它本来是"产品卖今天该看的"这个定位带来的副作用。
    # 决策是"绝不能漏"，所以默认不再丢历史公告 —— 老公告进库后靠 is_fresh/stale 排序，
    # 而不是在采集阶段就扔掉（扔了连"我们有没有"都答不了）。
    steps.append(run("抓公告", ["crawl.py", "--max-stale-days", "0", *refresh]))
    # **freshness 从主库重建**（A27 顺带修）：crawl 自己会在写盘后写 freshness，
    # 但 2026-10-19 06:25 那轮在 `os.replace` 之后、写 freshness 之前就没了 ——
    # 数据是全的，新鲜度却停在 00:03，门禁报"过期快照"。
    # 重建只要几毫秒、只读主库，放在这里就没有"抓住时间差"的问题。
    steps.append(run("重建新鲜度", ["tools/rebuild_freshness.py", "--write"]))
    # 导师层**不每天全量重抓**：
    #   · 师资名单一个月才变一次，每天 `--refresh` 纯粹是打学校服务器
    #   · 而且 `--refresh` 会让个人主页缓存失效，二级抓取受 `--max-level2` 上限约束，
    #     能补到的方向数会**骤降**（实测 379 → 116），把上一轮补好的质量覆盖掉
    # 所以日更走缓存 + 把上限放开（缓存命中很快），**每周单独跑一次带 --refresh 的**。
    # 写盘闸（crawl_faculty.py 里那道）会兜住"质量真的掉了"的情况。
    steps.append(run("抓导师", ["crawl_faculty.py", "--max-level2", "600"]))
    steps.append(run("院系清单", ["tools/make_units.py"]))

    fp_after = fingerprint()
    data_changed = (fp_before or {}).get("sha1") != (fp_after or {}).get("sha1")
    if fp_after is None:
        print("\n    ❌ 跑完之后 notices.jsonl 根本不存在 —— 抓取这一步什么都没产出")
    elif not data_changed:
        # **最重要的一条告警。** 抓取退出码是 0，但产出和跑之前一模一样。
        # 可能是真的没新公告，也可能是抓取静默失败（改版、封禁、选择器失配）。
        # 这两种必须能分开，所以这里**大声说出来**，不假装成功。
        print("\n    ⚠️  这一轮抓到的内容**一条都没变**（正文哈希 / 截止日 / 条数全同）")
        print("        （可能是真的没新公告；也可能是抓取静默失败 —— 去查 runs/ 里的明细）")

    counts, n = archive(today, data_changed=data_changed)
    print(f"\n--- 归档 {today} ---")
    print(f"    精简快照 {n} 条；差异 新增 {counts.get('added')} / "
          f"有变化 {counts.get('changed')} / 我们重解析 {counts.get('reparsed')} / "
          f"未抓到 {counts.get('removed')}")

    if not a.skip_fixtures:
        steps.append(run("重出 fixtures", ["tools/make_fixtures.py", "--as-of",
                                          today.isoformat()]))

    # ---- 派生字段刷新：**必须在生成事件之前** ----
    #
    # 坑（已踩，B 的 issues/010）：A6 把 38 条标题从"天津大学研究生招生网"改成真标题，
    # 但 `relevance` 还是按旧标题算出来的 2 分。而 relevance 是首屏**第一排序键**，
    # 于是那 9 条（天津 6 / 北大 3）排在错误位置，门禁 188/1。
    #
    # 根因不是"某处忘了改一行"，而是**缺少"派生字段统一刷新"这个环节**：
    # 标题、栏目、正文都可能被后处理改掉（标题回退、栏目改名、附件补正文），
    # 凡是**从它们算出来**的字段（relevance / record_quality / topic / is_fresh /
    # admission_year）就必须在同一处一起重算。否则下次修标题、修栏目，
    # 还会犯同一个错，而且 `retain_unrefreshed` 会把旧值一路带下去。
    #
    # `backfill_derived.py` 正好就是干这个的（它已经刷 quality/topic/时效），
    # 现在把 relevance 也纳入，于是这一条命令就是"派生字段的唯一入口"。
    # 位置：抓完、归档完、fixtures 之后，生成事件之前 —— 事件里带的 relevance
    # 必须已经是刷新后的值，否则推送排序又错一次。
    steps.append(run("刷新派生字段", ["tools/backfill_derived.py", "--write"]))

    # ---- 事件流：**必须每天生成，不能靠手跑** ----
    #
    # 坑（已踩，而且是结构性缺陷、不是一次失误）：这段步骤原来根本不存在，
    # `events.jsonl` 只能手动 `make_events.py --write` 追加。于是日更之后事件流
    # 永久落后于 notices.jsonl —— 2026-10-18 实测 1119 条公告里 655 条没有任何事件，
    # 另有 12 条事件指向的公告已经不在产物里。这些公告对"今日更新/推送"两页是隐形的，
    # 因为 B 的推送链路只读 events.jsonl。
    #
    # 接在这里的理由：changes.json 由上面"抓公告"那一步产出，事件要读它；
    # 归档与 record_stability 要看到这一轮的完整结果，所以放在它们之前。
    # `--daily` 一次做三件事：本轮变更事件、存量补事件（缺 added 的补齐）、覆盖/悬空自检；
    # 内部按 event_id 去重，同一天重跑不会重复追加。
    # **覆盖有缺口时这一步返回 1，日更整体变红** —— 不许静默，静默正是这个 bug 的成因。
    steps.append(run("生成事件", ["tools/make_events.py", "--daily", "--write"]))

    # 数据门禁放最后：前面任何一步把数据弄坏了，这里拦住
    steps.append(run("数据门禁", ["tests/test_data_contract.py"]))

    hard = [s for s in steps if s["exit"] != 0]
    dt = (datetime.now() - started).total_seconds()

    st = record_stability(today, started, dt, n, counts, steps,
                          fp_before, fp_after, data_changed)
    cont = continuity(today)

    os.makedirs(LOG, exist_ok=True)
    log_p = os.path.join(LOG, today.isoformat() + ".md")

    # ---- 机器可读的运行记录（`runs/<日期>.json`）----
    #
    # 为什么要两份：`.md` 是**给人看的**，格式说改就改；B 明确说过他不解析它
    # （"你不能把接口绑在别人的排版上"），所以 `/api/admin/crawl` 的 `steps`
    # 一直恒为 `null`。这份 `.json` 就是给接口读的。
    # 形状按 B 在 `contracts/api.md` 第 12 节里写的要求，**字段名照抄别改**：
    #   started_at / finished_at / records / steps[{step,seconds,exit}] / failed_steps
    json_p = os.path.join(LOG, today.isoformat() + ".json")
    with open(json_p, "w", encoding="utf-8") as f:
        json.dump({
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "records": n,
            "steps": [{"step": s["step"], "seconds": s["seconds"], "exit": s["exit"]}
                      for s in steps],
            "failed_steps": [s["step"] for s in hard],
            # 下面几个是附加信息（B 只读上面那些键，多给不破坏兼容）
            "seconds": round(dt, 1),
            "data_changed": data_changed,
            "counts": {k: counts.get(k) for k in
                       ("added", "changed", "reparsed", "removed", "reidentified")},
        }, f, ensure_ascii=False, indent=1)

    with open(log_p, "w", encoding="utf-8") as f:
        f.write(f"# 每日运行 {started:%Y-%m-%d %H:%M:%S}\n\n")
        f.write(f"总耗时 {dt:.0f}s ｜ 记录 {n} 条 ｜ 新增 {counts.get('added')} "
                f"｜ 有变化 {counts.get('changed')} ｜ 我们重解析 {counts.get('reparsed')} "
                f"｜ 本轮未抓到 {counts.get('removed')}\n\n")
        f.write(f"**这一轮产出有没有变**：{'变了' if data_changed else '⚠️ 没变（见 stability.jsonl）'}\n\n")
        f.write("| 步骤 | 耗时 | 退出码 |\n|---|---|---|\n")
        for s in steps:
            f.write(f"| {s['step']} | {s['seconds']}s | "
                    f"{'✅' if s['exit'] == 0 else '❌ ' + str(s['exit'])} |\n")
        for s in hard:
            f.write(f"\n## ❌ {s['step']} 失败\n\n```\n")
            f.write("\n".join(s["stderr_tail"] or s["tail"]))
            f.write("\n```\n")
    print(f"\n=== 完成  耗时 {dt:.0f}s ===")
    print(f"  运行日志 -> {log_p}")
    print(f"  历史归档 -> {HIST}")
    print(f"  稳定性   -> {STABILITY}")

    # 「每天更新」这句话的**唯一凭据**。连续天数比"跑了多少次"重要得多。
    if cont["runs"]:
        print(f"\n  连续更新：**{cont['streak']} 天**"
              f"（共 {cont['runs']} 轮 / {cont['days']} 天，最近一次 {cont['last']}）")
        if cont["no_new_data"]:
            print(f"  ⚠️  这些天跑成功但产出没变，值得看一眼：{cont['no_new_data'][-5:]}")
    else:
        print(f"\n  连续更新：从今天起算（stability.jsonl 第 1 条）")

    if hard:
        print(f"  ❌ 有 {len(hard)} 步失败：{[s['step'] for s in hard]}")
        sys.exit(1)
    print("  ✅ 全部通过")


if __name__ == "__main__":
    main()

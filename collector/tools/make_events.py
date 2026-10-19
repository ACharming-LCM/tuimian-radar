# -*- coding: utf-8 -*-
"""把 `changes.json`（本轮变更）落成**机器可读的事件流** `data/out/events.jsonl`。

    cd collector && python tools/make_events.py            # 预演，打印将产生什么
    cd collector && python tools/make_events.py --write    # 追加进 events.jsonl

契约：`contracts/events.schema.json`。**这是唯一口径**，改形状要先改契约。

## 为什么要有事件流，而不是让后端直接读 changes.json

两者回答的问题不同：

- `changes.json` 是**这一轮**的差异快照，每次抓取被覆盖 —— 它回答"上一轮到现在变了什么"
- `events.jsonl` 是**追加式**的事件流，永不覆盖 —— 它回答"**历史上发生过哪些值得打扰用户的事**"

推送要的是后者。而且推送必须**去重**（同一条变更不能推两次），这要求事件有**稳定 id**：
含生成时间的话，每天重跑都会变成新事件，去重立刻失效。

## 三条不能违反的规则

1. **`reparsed` 绝不产生事件。** 那是"我们改了抽取代码"，公告一个字没变。
   把它做成事件等于天天骗用户"学校更新了"。`changes.json` 里 `reparsed[]` 我**整个忽略**。
2. **`added` / `deadline_changed` / `attachment_added` 都要能回链原文**（`url` 必填）。
   缺 url 的事件直接跳过并报错，不许静默发出去。
3. **`field_state` 必须带全**。推送文案要靠它区分"确实没有"和"我们没读到" ——
   少了它，后端只能看见 `deadline: null`，然后**一定会**把它说成"没有截止日"。
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common      # noqa: E402

CHANGES = os.path.join(common.OUT_DIR, "changes.json")
EVENTS = os.path.join(common.OUT_DIR, "events.jsonl")

# 类型 → 稳定 id 的前缀。改这个会让历史事件和新事件对不上，**不要改**。
KIND_ADDED = "added"
KIND_DEADLINE = "deadline_changed"
KIND_ATTACH = "attachment_added"


def event_id(kind, notice_id, fingerprint):
    """稳定去重键：**不含生成时间**。

    同一个变更无论重跑多少次、隔多久重算，值都不变 —— 后端靠它保证
    "同一条变更不重复推送"。反之如果把 `at` 算进去，每天重跑都变成新事件，
    用户就会收到同一条推送的第二份。
    """
    raw = f"{kind}|{notice_id}|{fingerprint}".encode("utf-8")
    return kind[:4] + "_" + hashlib.sha1(raw).hexdigest()[:16]


def matched_hints(rec):
    """数据层能给的**粗粒度**匹配素材，不做匹配判断。

    匹配是后端的事（它才知道用户画像）。这里只把"这条公告涉及什么"摊开，
    让后端能拼出人话的「为什么推给你」。**relevance 不进这里** ——
    它只用于排序，用来解释"为什么推"会把一个不可靠信号说成理由。
    """
    out = []
    for d in rec.get("degree_types") or []:
        if d not in out:
            out.append(d)
    m = rec.get("apply_mode")
    if m and m not in out:
        out.append(m)
    for x in rec.get("majors") or []:
        if x not in out:
            out.append(x)
    return out[:6]


# 发布日早于检测日多少天，就算"补抓到的旧公告"而不是"新公告"。
#
# 为什么需要这条：2026-10-17 这一轮把 `details_per_board` 从 3 放到 6，
# 多抓出 90 条 —— 它们是**我们之前没抓到的旧文章**（最早的发布日是 2013 年），
# 但在 diff 眼里是 `added`（上一份文件里没有）。如果直接推，「新公告命中画像」
# 就会把「2013 年量子材料论坛成功举办」推给用户。**那不是数据错，是语义错。**
#
# 30 天：足够覆盖"月初发、月底才被发现"的正常滞后，又能挡掉明显的历史内容。
BACKFILL_DAYS = 30

# 不因为"补抓"而屏蔽的类型：`deadline_changed` / `attachment_added` 是**真实变更**，
# 不管这条公告多老，截止日改了或补了附件，用户都该知道。
BACKFILL_TOLERANT = (KIND_DEADLINE, KIND_ATTACH)


def is_backfill(kind, publish_date, detected_on):
    """这条事件是不是"我们多抓到了"，而不是"学校今天新发了"。"""
    if kind in BACKFILL_TOLERANT:
        return False
    if not publish_date or not detected_on:
        return False
    try:
        p = datetime.strptime(publish_date[:10], "%Y-%m-%d")
        d = datetime.strptime(detected_on[:10], "%Y-%m-%d")
    except ValueError:
        return False                  # 日期解析不了就不猜
    return (d - p).days > BACKFILL_DAYS


def base_event(rec, kind, at, detected_on, eid, change=None):
    """把一条记录翻成一个事件。**字段集合必须与 events.schema.json 一致。**"""
    ev = {
        "event_id": eid,
        "type": kind,
        "notice_id": rec.get("id"),
        "at": at,
        "detected_on": detected_on,
        "url": rec.get("url") or "",
        "school": rec.get("school") or "",
        "unit": rec.get("unit") or "",
        "title": rec.get("title") or "",
        "publish_date": rec.get("publish_date") or "",
        "deadline": rec.get("deadline"),
        # **必须带全三态**：后端靠它区分「确实没有」和「我们没读到」
        "field_state": rec.get("field_state") or {},
        "relevance": rec.get("relevance"),
        "is_backfill": is_backfill(kind, rec.get("publish_date"), detected_on),
        "matched": matched_hints(rec),
    }
    if change is not None:
        ev["change"] = change
    return ev


def _urls_of(sig):
    """从 `_att_sig` 的形状里取出 url 列表。

    **防御性解析是有意的**：`changes.json` 是磁盘上的 JSON，可能来自旧版本代码、
    也可能是手改过的。遇到不认识的行形状时**跳过那一条、不要整份崩掉** ——
    否则一份畸形数据会让整个事件流今天完全产不出来，而报出来是"事件生成失败"，
    根本指不到是哪一个附件签名坏了。（"失败必须带原因"在这里的落地方式。）
    """
    out = []
    for item in (sig or []):
        if isinstance(item, (list, tuple)) and item:
            out.append(item[0])
    return out


def build(diff):
    """`changes.json` → 事件列表（**不含 dedupe**，只做形状转换）。"""
    at = diff.get("generated_at") or datetime.now().isoformat(timespec="seconds")
    detected_on = at[:10]
    events, skipped = [], []

    # ① 新公告
    for rec in diff.get("added") or []:
        if not rec.get("url") or not rec.get("id"):
            skipped.append(("added", rec.get("id"), "缺 url 或 id，没法回链"))
            continue
        events.append(base_event(
            rec, KIND_ADDED, at, detected_on,
            event_id(KIND_ADDED, rec["id"], "")))

    # ② 有变化的公告：只从 `changed[]` 取（学校真的动了），**不看 reparsed**
    for item in diff.get("changed") or []:
        rec = item.get("record") or {}
        fields = item.get("fields") or {}
        if not rec.get("url") or not rec.get("id"):
            skipped.append(("changed", rec.get("id"), "缺 url 或 id，没法回链"))
            continue

        # 截止日变了
        if "deadline" in fields:
            try:
                old, new = fields["deadline"]
            except (TypeError, ValueError):
                old, new = None, rec.get("deadline")
            events.append(base_event(
                rec, KIND_DEADLINE, at, detected_on,
                event_id(KIND_DEADLINE, rec["id"], f"{old}->{new}"),
                change={"field": "deadline", "from": old, "to": new}))

        # 新增附件（**只报新增的那几个**，不是全量清单）
        if "attachments" in fields:
            try:
                old_sig, new_sig = fields["attachments"]
            except (TypeError, ValueError):
                old_sig, new_sig = [], []
            old_urls = set(_urls_of(old_sig))
            new_urls = [u for u in _urls_of(new_sig) if u and u not in old_urls]
            if new_urls:
                by_url = {a.get("url"): a for a in (rec.get("attachments") or [])}
                added_atts = [{"name": by_url.get(u, {}).get("name") or u.rsplit("/", 1)[-1],
                               "url": u} for u in new_urls]
                events.append(base_event(
                    rec, KIND_ATTACH, at, detected_on,
                    event_id(KIND_ATTACH, rec["id"], "|".join(sorted(new_urls))),
                    change={"field": "attachments", "attachments": added_atts}))

    return events, skipped, at


def load_seen(path):
    """已有事件里出现过的 event_id。**半截行要跳过** ——
    上次写到一半被打断，不该让整个事件流永久坏掉。"""
    seen = set()
    if not os.path.exists(path):
        return seen
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if not ln.strip():
                continue
            try:
                seen.add(json.loads(ln).get("event_id"))
            except json.JSONDecodeError:
                continue
    return seen


def make_sample(notices_path, out_path):
    """生成**演示用样例事件** `contracts/fixtures/events.sample.json`。

    为什么要单独造一份：磁盘上今天恰好**没有新事件**（`changes.json` 里
    added=0 / changed=0），而演示必须能同时看到 found / absent / unknown 三种截止日
    的**正确措辞**。等真事件出现是等不起的。

    **但一条都不许编造。** 每条都从真实 `notices.jsonl` 里挑出来的记录生成，
    字段值原样搬，只把 `type` / `at` 设成演示值。样例文件顶部写明它是样例。
    """
    with open(notices_path, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]
    if not recs:
        print("❌ notices.jsonl 是空的")
        return 1

    def pick(state, extra=None):
        for r in recs:
            fs = r.get("field_state") or {}
            if fs.get("deadline") != state:
                continue
            if extra and not extra(r):
                continue
            return r
        return None

    at = datetime.now().isoformat(timespec="seconds")
    day = at[:10]
    picks = [
        ("found", pick("found"), KIND_ADDED, None),
        ("absent", pick("absent"), KIND_ADDED, None),
        ("unknown", pick("unknown"), KIND_ADDED, None),
        # 变更类事件也各给一条，让后端能验"变成什么"的文案
        ("deadline_changed", pick("found"), KIND_DEADLINE,
         lambda r: {"field": "deadline",
                    "from": None, "to": r.get("deadline")}),
        ("attachment_added", pick("found", lambda r: r.get("attachments")), KIND_ATTACH,
         lambda r: {"field": "attachments",
                    "attachments": [{"name": a.get("name") or a["url"].rsplit("/", 1)[-1],
                                     "url": a["url"]}
                                    for a in (r.get("attachments") or [])[:1]]}),
    ]

    events = []
    for label, r, kind, change_fn in picks:
        if r is None:
            print(f"  ⚠️ 找不到 {label} 的样本，跳过")
            continue
        eid = event_id(kind, r["id"], f"sample-{label}")
        ev = base_event(r, kind, at, day, eid,
                        change=change_fn(r) if change_fn else None)
        events.append(ev)

    payload = {
        "_note": ("**这是演示样例，不是真实事件流。** 每条都从真实 notices.jsonl 里挑出的记录生成，"
                  "字段值原样搬；只把 type/at 设成演示值，event_id 带 `sample-` 指纹以便和真事件区分。"
                  "真实事件流是 data/out/events.jsonl（append-only）。"
                  "用途：让前端在后端接口就位之前就能把三种截止日措辞的界面做完。"),
        "_as_of": day,
        "_generated_on": day,
        "_how_to_reproduce": "cd collector && python tools/make_events.py --sample",
        "_covers": ["found", "absent", "unknown", "deadline_changed", "attachment_added"],
        "events": events,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"✅ 样例事件 {len(events)} 条 -> {out_path}")
    for e in events:
        print(f"   {e['type']:<16} deadline={e['deadline']} "
              f"state={e['field_state'].get('deadline')} | {e['title'][:30]}")
    return 0


def backfill_corpus(notices_path, events_path, write=False):
    """给**存量公告**补 `added` 事件（事件流是后加的，之前抓到的 196 条没有事件）。

    ## 为什么需要这一步

    事件流 `data/out/events.jsonl` 是 2026-10-17 才引入的。在这之前抓到的公告
    （最初那 196 条，现在 286 条里的大部分）**一条事件都没有** ——
    事件流只记录"从今天起发生的变化"。

    后果不是理论上的：实测 286 条公告里，**发布日 ≥ 30 天内、relevance=2 的有 29 条**
    （中山大学少数民族骨干计划、浙大材料学院选拔通知…），它们**全都不在事件流里**，
    于是推送预览只能推出 5 条，其中 3 条是「nav 页 / 考试安排 / 奖学金公示」这种没法上视频的。

    ## 时间语义（这一步唯一需要小心的地方）

    事件时间 `at` 用记录的 **`crawled_at`**（我们**第一次抓到它**的时刻），
    不是现在、也不是公告的发布日。这是唯一诚实的取值：事件的含义是
    "我们在什么时候把它收进来的"。

    因此 `is_backfill` 的判定也自动正确：
    - 2026-10-13 发布的公告，今天录入 → 距检测日 4 天 → **不是补抓**（真·新公告）
    - 2013-09-27 发布的公告，今天录入 → 距检测日 4700+ 天 → **是补抓**（会被推送层排除）

    ## 幂等

    按 `event_id`（`added|notice_id`）判重，已经有的跳过。跑多少次结果都一样。
    """
    with open(notices_path, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]
    seen = load_seen(events_path)
    fresh, skipped_nofield = [], 0
    for r in recs:
        eid = event_id(KIND_ADDED, r.get("id") or "", "")
        if eid in seen:
            continue
        if not r.get("url") or not r.get("id"):
            skipped_nofield += 1
            continue
        at = r.get("crawled_at") or datetime.now().isoformat(timespec="seconds")
        fresh.append(base_event(r, KIND_ADDED, at, at[:10], eid))

    n_bf = sum(1 for e in fresh if e.get("is_backfill"))
    print(f"存量公告 {len(recs)} 条 → 待补事件 {len(fresh)} 条"
          f"（其中补抓 {n_bf} 条、近 30 天 {len(fresh) - n_bf} 条）")
    if skipped_nofield:
        print(f"  ⚠️ 跳过 {skipped_nofield} 条（缺 url 或 id，没法回链）")
    if not fresh:
        print("（没有需要补的，事件流已经覆盖全部公告）")
        return 0

    if not write:
        for e in fresh[:8]:
            print(f"    {e['publish_date'] or '?'} 补抓={e['is_backfill']!s:5} "
                  f"{e['school']} | {e['title'][:34]}")
        print("\n（预演模式，未写盘。确认无误后加 --write）")
        return 0

    os.makedirs(os.path.dirname(events_path), exist_ok=True)
    with open(events_path, "a", encoding="utf-8") as f:
        for e in fresh:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"\n✅ 追加 {len(fresh)} 条到 {events_path}"
          f"（累计 {len(seen) + len(fresh)} 条）")
    return 0


def daily(changes_path, notices_path, events_path, write=False):
    """日更入口：**一轮把变更事件和存量补事件都做完**，并自检覆盖与悬空。

    为什么要有这个模式，而不是在 `run_daily.py` 里拼两条命令：
    这两件事必须共用**同一份 seen 集合**。分开跑的话，第二条命令读到的是第一条
    刚追加完的文件，看着没事；但一旦有中断，就会出现"变更事件写进去了、覆盖没补全"
    的中间态，而第二天没人知道。合并成一个函数，去重和落盘是一次性的。

    ## 幂等（硬要求）

    - `event_id` **不含生成时间**，同一条变更重跑多少次都是同一个 id；
    - 本次内部再用一个内存 `seen` 兜一层：同一条公告既在 `changes.added`、
      又在存量补事件里时只写一条；
    - 因此"同一天连跑两次日更，events 条数不变"。

    ## 三种计数（日志里必须打出来，这是这个 bug 的验收口径）

    - `新增事件`：这次真正追加了多少条
    - `无事件公告`：此刻仍没有任何事件的公告条数 —— **必须为 0**
    - `悬空事件`：事件指向的 notice_id 已不在 notices.jsonl 里 —— 历史流不删，
      只记账（写到 events_orphans.json）

    返回 0 表示可以继续（覆盖 0 缺口）；返回 1 表示有缺口，日更要红。
    """
    notices = []
    if os.path.exists(notices_path):
        with open(notices_path, encoding="utf-8") as f:
            notices = [json.loads(ln) for ln in f if ln.strip()]
    by_id = {r.get("id"): r for r in notices}

    seen = load_seen(events_path)
    n_before = len(seen)
    batch, dropped = [], []

    # ① 本轮变更（changes.json 在 crawl 那一步已经写好）
    if os.path.exists(changes_path):
        with open(changes_path, encoding="utf-8") as f:
            diff = json.load(f)
        evs, skipped, at = build(diff)
        print(f"changes.json 生成于 {at}，本轮事件 {len(evs)} 个"
              + (f"，跳过 {len(skipped)} 个（缺 url/id）" if skipped else ""))
        for e in evs:
            if e["event_id"] in seen:
                dropped.append(e["event_id"])
                continue
            seen.add(e["event_id"])
            batch.append(e)
    else:
        print(f"⚠️ 没有 {changes_path}，跳过变更事件（只做存量覆盖）")

    # ② 存量覆盖：任何一条没有 added 事件的公告都补上（时间用 crawled_at）
    missing, n_corpus = 0, 0
    for r in notices:
        eid = event_id(KIND_ADDED, r.get("id") or "", "")
        if eid in seen:
            continue
        if not r.get("url") or not r.get("id"):
            missing += 1
            continue
        stamp = r.get("crawled_at") or datetime.now().isoformat(timespec="seconds")
        seen.add(eid)
        batch.append(base_event(r, KIND_ADDED, stamp, stamp[:10], eid))
        n_corpus += 1
    print(f"存量覆盖：公告 {len(notices)} 条 → 本次补 {n_corpus} 条"
          + (f"，{missing} 条缺 id/url 无法回链" if missing else ""))

    if batch:
        if write:
            os.makedirs(os.path.dirname(events_path), exist_ok=True)
            with open(events_path, "a", encoding="utf-8") as f:
                for e in batch:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            print(f"✅ 追加 {len(batch)} 条 → {events_path}（{n_before} → {n_before + len(batch)}）")
        else:
            print(f"（预演）将追加 {len(batch)} 条 → {events_path}")
            for e in batch[:8]:
                print(f"    {e['type']:<16} {e['school']} | {e['title'][:34]}")
    else:
        print("（没有新事件，事件流已覆盖）")

    # ③ 自检：覆盖 + 悬空。**读落盘后的文件**，不拿内存状态自证。
    cover, orphans = audit(notices_path, events_path)
    print(f"自检：events {cover['events']} 条 / 公告 {cover['notices']} 条 / "
          f"无事件公告 {cover['no_event']} 条 / 悬空事件 {len(orphans)} 条")
    if orphans:
        op = os.path.join(os.path.dirname(events_path), "events_orphans.json")
        with open(op, "w", encoding="utf-8") as f:
            json.dump({"_note": ("事件流是 append-only 的历史流：公告从数据源消失是事实，"
                                 "但删事件等于把历史也删了。这里只记账，不删行。"
                                 "notice_missing=true 表示该事件指向的公告已不在 notices.jsonl。"
                                 "cause=id_changed 表示同 URL 还在、只是 id 变了（看 replaced_by）；"
                                 "cause=not_captured 表示本轮没抓到该 URL —— 实测多为栏目选择漂移，"
                                 "不是学校删公告。"),
                       "_generated_on": datetime.now().isoformat(timespec="seconds"),
                       "count": len(orphans), "events": orphans},
                      f, ensure_ascii=False, indent=1)
        print(f"  ⚠️ 悬空事件 {len(orphans)} 条已记账 → {op}")
        for e in orphans[:5]:
            print(f"     {e['notice_id']} {e.get('detected_on')} {e.get('title', '')[:30]}")

    if cover["no_event"]:
        print(f"❌ 仍有 {cover['no_event']} 条公告没有任何事件 —— 覆盖不全，日更必须红")
        return 1
    # 最后一行是给 run_daily 的 tail 用的：它只打印子进程 stdout 的最后 6 行，
    # 悬空明细会把这行挤掉，所以结论必须放在**最后**。
    print(f"生成事件：新增 {len(batch)} 条 / 事件总 {cover['events']} / "
          f"公告 {cover['notices']} / 无事件 {cover['no_event']} / 悬空 {len(orphans)}")
    return 0


def audit(notices_path, events_path):
    """覆盖与悬空对账。返回 ({events, notices, no_event}, orphans)。

    悬空事件必须**说得出是哪一种**，不能只报个数（2026-10-18 A8 的结论）：

    - `id_changed`：同一条 URL 还在 notices 里，只是 **id 变了**。
      实测：北大物理学院《2026 博士招生拟录取名单》原来挂在 `物理学院` 站点
      （id 前缀 `ua789e854`），后来在 `研究生招生网` 站点也被抓到（前缀 `ua8ae400a`）。
      `id = school_slug + 栏目号 + 文章号`，而 school_slug 是按 **school+unit** 算的 ——
      同一条公告换了个 unit，id 就变了。**公告没丢，是事件还指着旧 id。**
    - `not_captured`：URL 已经不在本轮产物里。实测这 12 条原页都还返回 200，
      是我们自己的**栏目选择漂移**（复旦 c15157 栏这轮没被选中；吉大那 6 条当年
      把导航条当成了栏目）。**不是学校删了公告。**
    """
    ids, by_url = set(), {}
    if os.path.exists(notices_path):
        with open(notices_path, encoding="utf-8") as f:
            for ln in f:
                if not ln.strip():
                    continue
                r = json.loads(ln)
                ids.add(r.get("id"))
                if r.get("url"):
                    by_url[(r["url"] or "").rstrip("/")] = r.get("id")
    events, covered, orphans = 0, set(), []
    if os.path.exists(events_path):
        with open(events_path, encoding="utf-8") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                events += 1
                nid = e.get("notice_id")
                covered.add(nid)
                if nid and nid not in ids:
                    same = by_url.get((e.get("url") or "").rstrip("/"))
                    orphans.append({"event_id": e.get("event_id"), "type": e.get("type"),
                                    "notice_id": nid, "detected_on": e.get("detected_on"),
                                    "school": e.get("school"), "title": e.get("title"),
                                    "url": e.get("url"), "notice_missing": True,
                                    "cause": "id_changed" if same else "not_captured",
                                    "replaced_by": same})
    return ({"events": events, "notices": len(ids), "no_event": len(ids - covered)},
            orphans)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真的追加（默认只预演）")
    ap.add_argument("--daily", action="store_true",
                    help="日更模式：变更事件 + 存量补事件 + 覆盖/悬空自检（run_daily 用这个）")
    ap.add_argument("--audit", action="store_true", help="只做覆盖/悬空对账，不写")
    ap.add_argument("--changes", default=CHANGES)
    ap.add_argument("--out", default=EVENTS)
    ap.add_argument("--sample", action="store_true",
                    help="生成演示用样例事件（给 C 在真事件流为空时用）")
    ap.add_argument("--backfill-corpus", action="store_true",
                    help="给存量公告补 added 事件（事件流是后加的，老公告没有事件）")
    a = ap.parse_args()

    if a.audit:
        cover, orphans = audit(os.path.join(common.OUT_DIR, "notices.jsonl"), a.out)
        print(json.dumps({"cover": cover, "orphans": len(orphans)}, ensure_ascii=False))
        for e in orphans:
            print(f"  {e['notice_id']} {e['detected_on']} {e['school']} {e['title'][:30]}")
        return 0

    if a.daily:
        return daily(a.changes, os.path.join(common.OUT_DIR, "notices.jsonl"),
                     a.out, write=a.write)

    if a.backfill_corpus:
        return backfill_corpus(os.path.join(common.OUT_DIR, "notices.jsonl"),
                               a.out, write=a.write)

    if a.sample:
        return make_sample(os.path.join(common.OUT_DIR, "notices.jsonl"),
                           os.path.join(common.ROOT, "contracts", "fixtures",
                                        "events.sample.json"))

    if not os.path.exists(a.changes):
        print(f"❌ 没有 {a.changes} —— 先跑一次 crawl.py（它会产出 changes.json）")
        return 1
    with open(a.changes, encoding="utf-8") as f:
        diff = json.load(f)

    events, skipped, at = build(diff)
    seen = load_seen(a.out)
    fresh = [e for e in events if e.get("event_id") not in seen]
    dup = len(events) - len(fresh)

    print(f"changes.json 生成于 {at}")
    print(f"  counts: {json.dumps(diff.get('counts') or {}, ensure_ascii=False)}")
    print(f"  本轮事件 {len(events)} 个 → 其中**新的** {len(fresh)} 个"
          + (f"（{dup} 个已在事件流里，跳过）" if dup else ""))
    for e in fresh:
        print(f"    {e['type']:<16} {e['school']} | {e['title'][:34]}")
        print(f"      deadline={e['deadline']} state={e['field_state'].get('deadline')} "
              f"url={'有' if e['url'] else '❌缺'}")
    if skipped:
        print(f"  ⚠️ 跳过 {len(skipped)} 条：{skipped[:3]}")

    if not fresh:
        print("\n（没有新事件。这是正常的 —— 没有新公告就没有推送。"
              "演示要用样例事件：contracts/fixtures/events.sample.json）")

    if not a.write:
        print("\n（预演模式，未写盘。确认无误后加 --write）")
        return 0

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "a", encoding="utf-8") as f:
        for e in fresh:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"\n✅ 追加 {len(fresh)} 条到 {a.out}（累计 {len(seen) + len(fresh)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

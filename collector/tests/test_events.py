# -*- coding: utf-8 -*-
"""事件流（`data/out/events.jsonl`）的契约测试。不依赖 pytest：

    cd collector && python tests/test_events.py

## 为什么必须测这个

`events.jsonl` 是推送链路的**唯一输入**。它一旦有下面任何一种毛病，
用户侧的表现都是"收到了一条不该收的推送"或"漏掉了一条该收的"：

  · `reparsed` 变成事件 → 天天骗用户"学校更新了"，而公告一个字没变
  · `event_id` 不稳定 → 每天重跑都变成新事件，**同一条变更推两次**
  · 缺 `url` → 推送点不回原文，整个"可信"卖点断了
  · 缺 `field_state` → 后端只能看见 `deadline: null`，然后**一定会**说成"没有截止日"

全部用**合成数据**测，不读磁盘上的真实产物 —— 这个项目栽过 5 次
"断言绑了磁盘上恰好是什么数据"。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)
sys.path.insert(0, COLLECTOR)
sys.path.insert(0, os.path.join(COLLECTOR, "tools"))

import common        # noqa: E402
import make_events as M   # noqa: E402

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


def rec(i, **kw):
    r = {"id": i, "url": f"https://x.edu/{i}.htm", "school": "X大学", "unit": "研究生院",
         "title": f"标题{i}", "publish_date": "2026-10-17",
         "deadline": None, "relevance": 2,
         "field_state": {"deadline": "unknown", "majors": "found",
                         "attachments": "absent", "body": "ok"},
         "degree_types": ["硕士"], "apply_mode": "推免", "majors": ["物理"],
         "attachments": []}
    r.update(kw)
    return r


def diff_of(**kw):
    d = {"generated_at": "2026-10-17T15:00:00", "counts": {},
         "added": [], "changed": [], "reparsed": [], "removed": []}
    d.update(kw)
    return d


FOUND_FS = {"deadline": "found", "majors": "found", "attachments": "absent", "body": "ok"}

# ---------------------------------------------------------------------------
print("== 三类事件都要产出 ==")
d = diff_of(
    added=[rec("n1", deadline="2026-11-01", field_state=FOUND_FS)],
    changed=[
        {"record": rec("n2", deadline="2026-10-30"),
         "fields": {"deadline": [None, "2026-10-30"]}, "body_changed": True},
        {"record": rec("n3", attachments=[{"name": "细则.pdf", "url": "https://x.edu/a.pdf"}]),
         "fields": {"attachments": ([], [[]])}, "body_changed": True},
    ],
)
# 上面 n3 的旧签名 [] 表示"原来没有附件"，新签名是 [[url, text_len]]
d["changed"][1]["fields"]["attachments"] = ([], [["https://x.edu/a.pdf", 100]])
events, skipped, at = M.build(d)
kinds = sorted(e["type"] for e in events)
check("三类事件都在", kinds, ["added", "attachment_added", "deadline_changed"])
check("没有跳过任何一条", skipped, [])

added_ev = next(e for e in events if e["type"] == "added")
check("added 带 url", bool(added_ev["url"]), True)
check("added 带 field_state", added_ev["field_state"], FOUND_FS)
check("added 带 matched 素材", added_ev["matched"][:3], ["硕士", "推免", "物理"])
check("added 没有 change 键（新增不需要前后值）", "change" in added_ev, False)

dl_ev = next(e for e in events if e["type"] == "deadline_changed")
check("deadline_changed 的 from", dl_ev["change"]["from"], None)
check("deadline_changed 的 to", dl_ev["change"]["to"], "2026-10-30")

att_ev = next(e for e in events if e["type"] == "attachment_added")
check("attachment_added 只报新增的那一个", len(att_ev["change"]["attachments"]), 1)
check("attachment_added 带附件名", att_ev["change"]["attachments"][0]["name"], "细则.pdf")

# ---------------------------------------------------------------------------
print("\n== `reparsed` 绝不产生事件（这是硬规矩）==")
d = diff_of(reparsed=[{"record": rec("n4"), "fields": {"title": ["旧", "新"]},
                       "why": "抽取器版本变了"}])
events, _, _ = M.build(d)
check("reparsed 一条事件都不产生", events, [])

# ---------------------------------------------------------------------------
print("\n== event_id：稳定、唯一、不含生成时间 ==")
d = diff_of(added=[rec("n1")],
            changed=[{"record": rec("n2"),
                      "fields": {"deadline": [None, "2026-10-30"]}, "body_changed": True}])
e1, _, _ = M.build(d)
d2 = json.loads(json.dumps(d))
d2["generated_at"] = "2026-10-18T09:00:00"      # 换一天重算
e2, _, _ = M.build(d2)
check("换生成时间，event_id 不变（去重的前提）",
      [x["event_id"] for x in e1], [x["event_id"] for x in e2])
ids = [x["event_id"] for x in e1]
check("event_id 互不相同", len(set(ids)), len(ids))

# 截止日再变一次 → 必须是**新**事件，不能和上一次合并
d3 = diff_of(changed=[{"record": rec("n2"),
                       "fields": {"deadline": ["2026-10-30", "2026-11-05"]},
                       "body_changed": True}])
e3, _, _ = M.build(d3)
check_true("截止日二次变更产生新 id（否则第二次变更会被去重吃掉）",
           e3[0]["event_id"] != e1[1]["event_id"], f"{e3[0]['event_id']} vs {e1[1]['event_id']}")

# ---------------------------------------------------------------------------
print("\n== 缺 url 的事件要跳过并带原因，不许静默发出去 ==")
d = diff_of(added=[rec("n5", url="")])
events, skipped, _ = M.build(d)
check("缺 url → 不产出事件", events, [])
check_true("缺 url → 记下原因", skipped and "url" in skipped[0][2], str(skipped))

# ---------------------------------------------------------------------------
print("\n== 畸形附件签名不许让整份事件流崩掉 ==")
d = diff_of(changed=[{"record": rec("n6", attachments=[{"name": "a.pdf", "url": "https://x.edu/a.pdf"}]),
                      "fields": {"attachments": ([[], 0], [["https://x.edu/a.pdf", 9]])},
                      "body_changed": True}])
try:
    events, skipped, _ = M.build(d)
    ok = True
    err = ""
except Exception as e:                      # noqa: BLE001
    ok, err, events = False, f"{type(e).__name__}: {e}", []
check_true("畸形签名不抛异常（跳过坏的那部分）", ok, err)
check_true("仍然产出了 attachment_added", any(x["type"] == "attachment_added" for x in events),
           str([x["type"] for x in events]))

# ---------------------------------------------------------------------------
print("\n== 追加式去重：同一条变更不许写两遍 ==")
import tempfile
tmpd = tempfile.mkdtemp(prefix="ev-")
try:
    p = os.path.join(tmpd, "events.jsonl")
    d = diff_of(added=[rec("n1")])
    events, _, _ = M.build(d)
    with open(p, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    seen = M.load_seen(p)
    check("第一次写入后 seen 里有 1 个", len(seen), 1)
    fresh = [e for e in M.build(d)[0] if e["event_id"] not in seen]
    check("同一份 changes.json 再跑，新的 0 个", len(fresh), 0)

    # 半截行不许毁掉整份事件流
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"event_id": "半截')
    check("半截行不影响读取已有 id", len(M.load_seen(p)), 1)
finally:
    import shutil
    shutil.rmtree(tmpd, ignore_errors=True)

# ---------------------------------------------------------------------------
print("\n== 产出必须过契约（events.schema.json）==")
try:
    import jsonschema
    with open(os.path.join(common.ROOT, "contracts", "events.schema.json"),
              encoding="utf-8") as f:
        schema = json.load(f)
    d = diff_of(added=[rec("n1", deadline="2026-11-01", field_state=FOUND_FS)],
                changed=[{"record": rec("n2"),
                          "fields": {"deadline": [None, "2026-10-30"]}, "body_changed": True}])
    events, _, _ = M.build(d)
    bad = []
    for e in events:
        try:
            jsonschema.validate(e, schema)
        except jsonschema.ValidationError as ex:
            bad.append((e.get("notice_id"), list(ex.absolute_path), ex.message[:100]))
    check("每个事件都过 events.schema.json", bad, [])
except ImportError:
    print("  （没装 jsonschema，跳过）")

print("\n== is_backfill：'我们多抓到了' 不许被当成 '学校今天新发了' ==")
# 2026-10-17 实测：details_per_board 从 3 放到 6 之后多抓出 90 条，
# 其中 85 条是旧文章（最老 2013-09-27）。不标记的话，「新公告命中画像」
# 会把「2013 年量子材料论坛成功举办」推给用户。
OLD = "2013-09-27"
NEW = "2026-10-17"
check("老公告（13 年前）→ 补抓", M.is_backfill("added", OLD, NEW), True)
check("当天的公告 → 不是补抓", M.is_backfill("added", NEW, NEW), False)
check("29 天前 → 还在容差内", M.is_backfill("added", "2026-09-18", NEW), False)
check("31 天前 → 算补抓", M.is_backfill("added", "2026-09-16", NEW), True)
check("没有发布日 → 不猜", M.is_backfill("added", "", NEW), False)
check("**截止日变了，再老的公告也要报**（不是补抓）",
      M.is_backfill("deadline_changed", OLD, NEW), False)
check("**补了附件，再老的也要报**（不是补抓）",
      M.is_backfill("attachment_added", OLD, NEW), False)

d = diff_of(added=[rec("old", publish_date="2013-09-27"),
                   rec("new", publish_date="2026-10-17")])
events, _, _ = M.build(d)
by_id = {e["notice_id"]: e for e in events}
check("build 里老公告标成补抓", by_id["old"]["is_backfill"], True)
check("build 里当天公告不标", by_id["new"]["is_backfill"], False)

print("\n== 存量补事件（--backfill-corpus）：幂等 + 时间语义 ==")
# 事件流是 2026-10-17 才引入的，此前的 196 条公告一条事件都没有。
# 补录时**事件时间必须用记录的 `crawled_at`**（我们第一次抓到它的时刻），
# 不能用现在 —— 用了现在的话，2013 年的公告也会被判成"刚录入的新公告"。
import tempfile
tmpd2 = tempfile.mkdtemp(prefix="evc-")
try:
    np = os.path.join(tmpd2, "notices.jsonl")
    ep = os.path.join(tmpd2, "events.jsonl")
    rows = [
        rec("old1", url="https://x.edu/old1.htm", publish_date="2013-09-27",
            crawled_at="2026-10-17T09:13:12"),
        rec("new1", url="https://x.edu/new1.htm", publish_date="2026-10-13",
            crawled_at="2026-10-17T09:13:12"),
        rec("nourl", url="", publish_date="2026-10-13",
            crawled_at="2026-10-17T09:13:12"),
    ]
    with open(np, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    M.backfill_corpus(np, ep, write=True)
    got = [json.loads(l) for l in open(ep, encoding="utf-8") if l.strip()]
    check("补出 2 条（缺 url 的那条被跳过）", len(got), 2)
    by = {e["notice_id"]: e for e in got}
    check("**事件时间用的是 crawled_at，不是现在**",
          by["old1"]["at"], "2026-10-17T09:13:12")
    check("13 年前的公告 → 补抓", by["old1"]["is_backfill"], True)
    check("4 天前的公告 → 不是补抓", by["new1"]["is_backfill"], False)

    M.backfill_corpus(np, ep, write=True)     # 再跑一次
    got2 = [json.loads(l) for l in open(ep, encoding="utf-8") if l.strip()]
    check("**幂等：再跑一次不重复追加**", len(got2), 2)
finally:
    import shutil
    shutil.rmtree(tmpd2, ignore_errors=True)

print("\n== 演示样例必须过契约，而且三种截止日态齐全 ==")
# 样例是 C 在真事件流为空时唯一的依据（今天 added=0，事件流确实是空的）。
# 它一旦有形状问题，C 会照着错的东西把界面做完 —— 那是"看起来做完了"的典型。
import glob
sample_p = os.path.join(common.ROOT, "contracts", "fixtures", "events.sample.json")
check_true("events.sample.json 存在", os.path.exists(sample_p), sample_p)
if os.path.exists(sample_p):
    with open(sample_p, encoding="utf-8") as f:
        sample = json.load(f)
    evs = sample.get("events") or []
    check_true("样例里有事件", len(evs) > 0, str(len(evs)))
    states = {(e.get("field_state") or {}).get("deadline") for e in evs}
    for want in ("found", "absent", "unknown"):
        check_true(f"样例覆盖了 deadline={want}", want in states, str(sorted(x for x in states if x)))
    check_true("每条样例都有 url（回链）", all(e.get("url") for e in evs))
    check_true("每条样例都有 matched（匹配素材）", all("matched" in e for e in evs))
    try:
        import jsonschema
        with open(os.path.join(common.ROOT, "contracts", "events.schema.json"),
                  encoding="utf-8") as f:
            sc = json.load(f)
        bad = []
        for e in evs:
            try:
                jsonschema.validate(e, sc)
            except jsonschema.ValidationError as ex:
                bad.append((e.get("event_id"), list(ex.absolute_path), ex.message[:90]))
        check("每条样例都过 events.schema.json", bad, [])
    except ImportError:
        print("  （没装 jsonschema，跳过 schema 校验）")

print("\n== 真实事件流（如果存在）每一行都必须过契约 ==")
# 今天没有新公告时这个文件不存在，那是正常的（不是失败）。
# 但一旦它存在，就**不许有任何一行是坏的** —— 事件流是 append-only，
# 一行坏数据会在里面躺到天荒地老，而且每轮推送都可能撞上它。
real_p = os.path.join(common.OUT_DIR, "events.jsonl")
if not os.path.exists(real_p):
    print("  （events.jsonl 还不存在 —— 今天没有新事件，这是正常的）")
else:
    with open(real_p, encoding="utf-8") as f:
        raw_lines = [ln for ln in f if ln.strip()]
    parsed, broken = [], []
    for i, ln in enumerate(raw_lines, 1):
        try:
            parsed.append(json.loads(ln))
        except json.JSONDecodeError as e:
            broken.append((i, str(e)[:60]))
    check(f"events.jsonl 每行都是合法 JSON（{len(raw_lines)} 行）", broken, [])
    if parsed:
        ids = [e.get("event_id") for e in parsed]
        dups = sorted({i for i in ids if ids.count(i) > 1})
        check("event_id 不重复（重复会让同一条变更推两次）", dups, [])
        check_true("每条都有 url", all(e.get("url") for e in parsed),
                   str([e.get("notice_id") for e in parsed if not e.get("url")][:3]))
        check_true("每条都有 field_state",
                   all(isinstance(e.get("field_state"), dict) and e["field_state"]
                       for e in parsed))
        try:
            import jsonschema
            with open(os.path.join(common.ROOT, "contracts", "events.schema.json"),
                      encoding="utf-8") as f:
                sc = json.load(f)
            bad = []
            for e in parsed:
                try:
                    jsonschema.validate(e, sc)
                except jsonschema.ValidationError as ex:
                    bad.append((e.get("event_id"), list(ex.absolute_path), ex.message[:80]))
            check("每行都过 events.schema.json", bad, [])
        except ImportError:
            print("  （没装 jsonschema，跳过 schema 校验）")

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

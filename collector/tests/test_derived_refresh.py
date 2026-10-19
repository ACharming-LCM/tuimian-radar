# -*- coding: utf-8 -*-
"""派生字段刷新回归（A13）：标题/栏目改了，派生字段必须跟着重算。

    cd collector && python tests/test_derived_refresh.py

为什么必须有：B 的 issues/010 —— A6 把 38 条标题改成真标题，
但 `relevance` 还是按旧标题算的 2 分。relevance 是首屏第一排序键，
于是那 9 条排在错误位置，门禁 188/1。

根因不是"忘了一处"，是**没有"派生字段统一刷新"这个环节**。
这条测试造一份"标题被改过、派生字段还是旧值"的输入，跑一次刷新，要求它被纠正。
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import fields      # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILED.append(name)


# 一条"标题被改过、但 relevance 还是旧值"的记录：
# 旧标题是站点名（relevance 算出来会偏低/偏高），新标题是招生简章。
REC = {
    "id": "n_stale", "school": "天津大学", "unit": "研究生院",
    "board": "招生信息", "title": "天津大学2027年硕士研究生招生简章",
    "url": "https://example.edu/1.htm", "publish_date": "2026-10-01",
    "body_text": "正文" * 400, "record_quality": "ok", "quality_hits": [],
    "topic": "apply", "topic_hits": ["招生"], "relevance": 0,      # ← 旧值，错的
    "deadline": None, "field_state": {"deadline": "absent", "majors": "absent",
                                      "attachments": "absent", "body": "ok",
                                      "materials": "absent", "contact": "absent"},
    "crawled_at": "2026-10-01T00:00:00", "content_hash": "sha1:x",
    "extract": {"method": "rule", "version": "v6", "confidence": 0.5},
}

want = fields.relevance_of(REC["title"], REC["board"], "apply")
check("这条的**正确** relevance 与旧值不同（否则测不出东西）", want != REC["relevance"],
      f"旧={REC['relevance']} 正确={want}")

d = tempfile.mkdtemp()
p = os.path.join(d, "notices.jsonl")
with open(p, "w", encoding="utf-8") as f:
    f.write(json.dumps(REC, ensure_ascii=False) + "\n")

print("\n== 跑一次派生字段刷新 ==")
r = subprocess.run([sys.executable, "tools/backfill_derived.py", "--write", "--out", p],
                   cwd=HERE, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
check("刷新命令成功", r.returncode == 0, f"exit={r.returncode} {r.stderr[-200:]}")
check("日志里能看到修正条数", "relevance 修正 1 条" in (r.stdout or ""),
      (r.stdout or "")[-200:])

with open(p, encoding="utf-8") as f:
    got = json.loads(f.readline())
check("**旧 relevance 被纠正成正确值**", got.get("relevance") == want,
      f"期望 {want} 实际 {got.get('relevance')}")

print("\n== 再跑一次：幂等，不该再改 ==")
r2 = subprocess.run([sys.executable, "tools/backfill_derived.py", "--write", "--out", p],
                    cwd=HERE, capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
check("第二次修正 0 条", "relevance 修正 0 条" in (r2.stdout or ""), (r2.stdout or "")[-160:])

print("\n== run_daily 里必须有这一步 ==")
daily = open(os.path.join(HERE, "tools", "run_daily.py"), encoding="utf-8").read()
check("run_daily 调用 backfill_derived.py", "tools/backfill_derived.py" in daily)
check("步骤名叫「刷新派生字段」", "刷新派生字段" in daily)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

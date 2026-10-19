# -*- coding: utf-8 -*-
"""`--fast` 的前置校验回归（A9）。

    cd collector && python tests/test_fast_guard.py

为什么必须有：fast 只读每栏第 1 页，其余记录靠"带过上一轮"。上一轮产物一旦
缺失/截断/被清空，fast 会写出一个只有几百条的产物，而**退出码是 0** ——
"看起来正常的半份数据"比直接报错危险得多。
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import crawl      # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILED.append(name)


def rec(i):
    return {"id": f"n{i}", "url": f"https://x.edu/{i}.htm", "school": "某大学",
            "unit": "研究生院", "title": f"第{i}条", "publish_date": "2026-10-01",
            "body_text": "正文" * 200}


d = tempfile.mkdtemp()
full = os.path.join(d, "full.jsonl")
with open(full, "w", encoding="utf-8") as f:
    for i in range(crawl.FAST_MIN_PREV + 20):
        f.write(json.dumps(rec(i), ensure_ascii=False) + "\n")

print("== 正常基线 ==")
check("完整基线通过校验", crawl._check_fast_baseline(full) is None,
      str(crawl._check_fast_baseline(full)))

print("\n== 基线缺失 ==")
missing = os.path.join(d, "missing.jsonl")
why = crawl._check_fast_baseline(missing)
check("文件不存在 → 拒绝", why is not None, str(why))
check("原因里说清是“不存在”", "不存在" in (why or ""), str(why))

print("\n== 基线被清空 ==")
empty = os.path.join(d, "empty.jsonl")
open(empty, "w").close()
why = crawl._check_fast_baseline(empty)
check("空文件 → 拒绝", why is not None, str(why))

print("\n== 基线被截断 ==")
trunc = os.path.join(d, "trunc.jsonl")
with open(full, encoding="utf-8") as src:
    data = src.read()
open(trunc, "w", encoding="utf-8").write(data[: len(data) // 2])   # 从中间切断
why = crawl._check_fast_baseline(trunc)
check("截断 → 拒绝", why is not None, str(why))
check("原因里说清是“损坏/截断”", "损坏" in (why or "") or "换行" in (why or ""), str(why))

print("\n== 条数太少 ==")
few = os.path.join(d, "few.jsonl")
with open(few, "w", encoding="utf-8") as f:
    for i in range(50):
        f.write(json.dumps(rec(i), ensure_ascii=False) + "\n")
why = crawl._check_fast_baseline(few)
check("50 条 → 拒绝", why is not None, str(why))

print("\n== 真跑一次：没有基线时 --fast 必须非 0 退出 ==")
out2 = os.path.join(d, "nope.jsonl")
r = subprocess.run([sys.executable, "crawl.py", "--fast", "--out", out2],
                   cwd=HERE, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
check("退出码非 0", r.returncode != 0, f"exit={r.returncode}")
check("**没有写出产物文件**（不许降级成半份数据）", not os.path.exists(out2),
      f"exists={os.path.exists(out2)}")
check("日志说明了原因", "拒绝启动" in (r.stdout or ""), (r.stdout or "")[-160:])

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

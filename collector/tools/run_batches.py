# -*- coding: utf-8 -*-
"""A18 分批抓取：按学校逐个跑，跑完一批就合并进主文件。

    cd collector && python tools/run_batches.py            # 全部学校
    cd collector && python tools/run_batches.py --start 6  # 从第 7 个学校接着跑

为什么分批：一次全量 20 分钟起，崩一次全丢（2026-10-18 真发生）。
分批之后最坏只丢当前这一所；合并是"按 URL 并入主文件"，所以每批都能单独提交。
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)
sys.path.insert(0, COLLECTOR)

import common      # noqa: E402

PY = sys.executable
BATCH_DIR = os.path.join(common.ROOT, "_scratch", "batch")

# 档案深的学校才开 8 页 / 60 详情。其余用 sites.json 的默认（4 页 / 30 详情），
# 否则 29 所全深抓要 2 小时以上 —— 为了整齐而让所有学校都慢，是拿时间换整齐。
DEEP_SCHOOLS = {"四川大学", "北京大学", "吉林大学", "南京大学", "中南大学", "郑州大学"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0, help="从第几个学校开始（0 基）")
    ap.add_argument("--end", type=int, default=999)
    ap.add_argument("--skip", default="", help="逗号分隔，跳过的学校（已跑过的）")
    a = ap.parse_args()

    cfg = json.load(open(os.path.join(COLLECTOR, "sites.json"), encoding="utf-8"))
    schools = []
    for s in cfg["schools"]:
        if s["school"] not in schools:
            schools.append(s["school"])
    skip = {x for x in a.skip.split(",") if x}
    todo = [x for x in schools[a.start:a.end] if x not in skip]
    os.makedirs(BATCH_DIR, exist_ok=True)
    print(f"共 {len(schools)} 所学校，本次跑 {len(todo)} 所：{todo}", flush=True)

    for i, school in enumerate(todo, start=a.start):
        out = os.path.join(BATCH_DIR, f"{school}.jsonl")
        t0 = time.time()
        log = os.path.join(BATCH_DIR, f"{school}.log")
        with open(log, "w", encoding="utf-8", errors="replace") as f:
            cmd = [PY, "-u", "crawl.py", "--school", school,
                   "--max-stale-days", "0", "--no-diff", "--out", out]
            if school in DEEP_SCHOOLS:
                cmd += ["--pages", "8", "--details", "60"]
            r = subprocess.run(cmd,
                               cwd=COLLECTOR, stdout=f, stderr=subprocess.STDOUT)
        dt = time.time() - t0
        n = 0
        if os.path.exists(out):
            n = sum(1 for _ in open(out, encoding="utf-8"))
        print(f"[{i + 1}/{len(schools)}] {school}: exit={r.returncode} "
              f"{dt:.0f}s 记录 {n}", flush=True)
        m = subprocess.run([PY, "tools/merge_batch.py", out, "--write"],
                           cwd=COLLECTOR, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        for line in (m.stdout or "").strip().splitlines():
            print("    " + line, flush=True)
        if m.returncode != 0:
            print("    !! 合并失败", (m.stderr or "")[-200:], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

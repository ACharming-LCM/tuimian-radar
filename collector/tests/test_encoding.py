# -*- coding: utf-8 -*-
"""编码韧性回归：私用区字符不许崩，也不许落盘。

    cd collector && python tests/test_encoding.py

背景（真事）：2026-10-18 11:41 的计划任务第一次跑就崩了 ——

    UnicodeEncodeError: 'gbk' codec can't encode character '\ue603'
    crawl.py line 433: print(f"  -> [{b['score']:>2}] {b['label']}  {b['url']}")

北大研招网的菜单文字里带 7 个图标字体码位（`\ue603 硕士招生 \ue61e 博士招生 …`），
计划任务环境是 GBK，一个 `print()` 就把整轮抓取废掉了。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import common      # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILED.append(name)


PUA = "\ue603"


def run_gbk(code):
    env = dict(os.environ, PYTHONIOENCODING="gbk")
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=HERE)


print("== 私用区字符清洗 ==")
check("清洗后不含私用区", PUA not in common.clean_text("硕士招生" + PUA + "博士招生"),
      repr(common.clean_text("硕士招生" + PUA + "博士招生")))
check("正常文字不受影响", common.clean_text("2027年硕士研究生招生简章"), "2027年硕士研究生招生简章")
check("空串安全", common.clean_text("") == "", repr(common.clean_text("")))
check("None 安全", common.clean_text(None) is None, repr(common.clean_text(None)))
check("北大真实栏目名清洗后是干净菜单",
      common.clean_text("\ue603 硕士招生 \ue61e 博士招生 \ue886 港澳台招生"),
      " 硕士招生  博士招生  港澳台招生")

print("\n== GBK 环境下 print 不许崩 ==")
r = run_gbk("import sys; sys.path.insert(0, r'%s'); import common; "
            "common.safe_stdout(); print('x\\ue603y'); sys.exit(0)" % HERE)
check("装了口径兜底后，打印私用区字符不抛异常", r.returncode == 0,
      f"exit={r.returncode} err={r.stderr.strip()[:80]}")

r = run_gbk("print('x\\ue603y')")
check("**没装兜底时会崩**（证明这条测试有效，不是空跑）", r.returncode != 0,
      f"exit={r.returncode}")

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

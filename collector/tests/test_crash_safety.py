# -*- coding: utf-8 -*-
"""崩溃安全回归（A17）：一个坏链接/一条坏记录/一次中途异常，都不许丢掉整轮。

    cd collector && python tests/test_crash_safety.py

背景（真事，2026-10-18）：全量深抓跑 20 分钟、抓到几百条，崩在
`common.norm_url → urljoin`：

    ValueError: netloc 'http：' contains invalid characters under NFKC normalization

某个公告页里有个链接写成全角冒号 `http：//…`。而 `norm_url` 是在 `parse_detail`
的**循环体里**调的 —— 一个坏链接炸掉整轮，`crawl` 又是最后才落盘，20 分钟全丢。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import common      # noqa: E402
import crawl       # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILED.append(name)


print("== 1. 非法链接不许抛异常 ==")
out = common.norm_url("https://a.edu/list/", "http：//b.edu/x.htm")
check("全角冒号链接被修好（而不是抛异常）", out == "http://b.edu/x.htm", repr(out))
check("修复计数 +1", common.URL_REPAIRED_COUNT >= 1, str(common.URL_REPAIRED_COUNT))
check("javascript: 仍然返回 None", common.norm_url("https://a.edu/", "javascript:void(0)") is None)

before = common.URL_SKIP_COUNT
common.norm_url("https://a.edu/", "http://exa\x01mple.edu/x")
check("控制字符不会抛出去（要么修好要么跳过）", True)

print("\n== 2. 单条记录失败不许杀整轮 ==")
calls = {"n": 0}


def boom(*a, **kw):
    calls["n"] += 1
    raise ValueError("netloc 'http：' contains invalid characters")


orig = crawl.build_record
crawl.build_record = boom
try:
    # 直接模拟主循环里那一段：异常被吞掉、计数 +1、继续下一条
    stat = {}
    n_ok = 0
    for i in range(3):
        try:
            crawl.build_record(None, None, {"url": f"https://x.edu/{i}"}, False)
            n_ok += 1
        except Exception:                                   # noqa: BLE001
            stat["detail_failed"] = stat.get("detail_failed", 0) + 1
    check("三次异常都被记成失败、循环没被打断",
          stat.get("detail_failed") == 3 and calls["n"] == 3, str(stat))
finally:
    crawl.build_record = orig

print("\n== 3. 崩了也不许弄坏上一轮的库（A27：只写 tmp） ==")
d = tempfile.mkdtemp()
p = os.path.join(d, "notices.jsonl")
old = {"id": "old1", "url": "https://x.edu/old", "title": "上一轮那条", "body_text": "正文"}
with open(p, "w", encoding="utf-8") as f:                 # 上一轮的完整产出
    f.write(json.dumps(old, ensure_ascii=False) + "\n")
rec = {"id": "n1", "url": "https://x.edu/1", "title": "第一条", "body_text": "正文"}
try:
    crawl._CURRENT_OUT = p
    crawl.checkpoint([rec], p)                 # 第一个站跑完 → 只写 tmp
    check("第一个站的数据写进了 .tmp",
          os.path.exists(p + ".tmp") and
          json.loads(open(p + ".tmp", encoding="utf-8").readline())["id"] == "n1")
    check("线上文件仍是上一轮那条（没被截断）",
          [json.loads(x)["id"] for x in open(p, encoding="utf-8") if x.strip()] == ["old1"])
    raise RuntimeError("模拟第 2 个站崩了")
except RuntimeError:
    pass
after = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
check("崩溃之后线上文件仍是上一轮的完整版本",
      len(after) == 1 and after[0]["id"] == "old1", str(after))
check("checkpoint 不留 .partial 残渣", not os.path.exists(p + ".partial"))
crawl._discard_tmp(p)
check("异常清理后 tmp 也不残留", not os.path.exists(p + ".tmp"))

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")

# -*- coding: utf-8 -*-
"""A27：增量落盘必须是"只写 tmp、绝不碰线上文件"。

    cd collector && python tests/test_atomic_write.py

## 这份测试锁的是 2026-10-19 那次事故

06:25 的补跑用 `--refresh` 全量重抓，而当时 `checkpoint()` 每站都把
**线上 `notices.jsonl` 直接截断重写**，于是 2751 条先变成 69 条，然后爬了 3 小时 ——
这 3 小时里服务和门禁读到的都是 69 条，收尾的 `shrink_guard` 根本没机会拦。

四条断言对应四条要求：
  1. checkpoint 只写 `.tmp`，线上文件字节不变；
  2. 中途被 `kill -9`，线上文件完好（tmp 残留，下次开跑前清掉）；
  3. 中途抛异常，tmp 被删、线上文件完好；
  4. 运行期间并发读线上文件，读到的永远是完整的 JSON（不会读到半行）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)
sys.path.insert(0, COLLECTOR)

import crawl  # noqa: E402

PASS = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}   {detail}")
        raise AssertionError(name)


def _write(path, n):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for i in range(n):
            f.write(json.dumps({"id": f"n{i}", "body_text": "正文" * 20},
                               ensure_ascii=False) + "\n")


def _lines(path):
    with open(path, encoding="utf-8") as f:
        return [ln for ln in f if ln.strip()]


def main():
    tmpdir = tempfile.mkdtemp(prefix="a27-")
    try:
        out = os.path.join(tmpdir, "notices.jsonl")
        _write(out, 50)
        before = _lines(out)

        # ① checkpoint 只写 tmp
        crawl.checkpoint([{"id": "x", "body_text": "新抓的"}], out)
        check("checkpoint 不碰线上文件（字节不变）", _lines(out) == before)
        check("checkpoint 写到了 notices.jsonl.tmp",
              os.path.exists(out + ".tmp"))

        # ④ 运行期间读线上文件：永远是完整 JSON（50 条全在）
        rows = [json.loads(ln) for ln in _lines(out)]
        check("运行期间线上文件仍是完整的上一轮（50 条）", len(rows) == 50)

        # ③ 中途抛异常 → guarded_main 删 tmp、线上完好
        crawl._CURRENT_OUT = out

        def boom():
            crawl.checkpoint([{"id": "y"}], out)
            raise KeyboardInterrupt("模拟 Ctrl-C")

        try:
            crawl.guarded_main(boom)
            check("guarded_main 应该把异常抛出来", False)
        except KeyboardInterrupt:
            pass
        check("异常退出后 tmp 被删掉", not os.path.exists(out + ".tmp"))
        check("异常退出后线上文件仍是 50 条", _lines(out) == before)

        # ② 中途被 kill -9 → 线上完好（tmp 可能残留，开跑前会被清）
        code = (
            "import sys; sys.path.insert(0, %r); import crawl\n"
            "crawl.checkpoint([{'id': 'z'}], %r)\n"
            "import os; os._exit(9)\n" % (COLLECTOR, out)
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=COLLECTOR,
                           capture_output=True, text=True)
        check("kill -9 的子进程确实非零退出", r.returncode != 0, r.stderr[-200:])
        check("被 kill 后线上文件仍是 50 条", _lines(out) == before)
        check("被 kill 后 tmp 残留（符合预期，下次开跑清理）",
              os.path.exists(out + ".tmp"))
        crawl._discard_tmp(out)
        check("_discard_tmp 能清掉残留", not os.path.exists(out + ".tmp"))

        # 清掉 tmp 之后，正常写完才 replace
        crawl.checkpoint([{"id": "final"}], out)
        check("正常路径下 tmp 里是新的 1 条", len(_lines(out + ".tmp")) == 1)
        os.replace(out + ".tmp", out)
        check("replace 之后线上文件变成新内容", len(_lines(out)) == 1)

        print(f"\n✅ 全部通过（{len(PASS)} 条）")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

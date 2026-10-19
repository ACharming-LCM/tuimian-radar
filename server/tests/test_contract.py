# -*- coding: utf-8 -*-
"""pytest 包装层：把 `run.py` 里的契约用例逐条暴露成 pytest 测试。

跑法（项目根目录）：
    python -m pytest server/tests -q

**为什么是包装层而不是另写一份：**
我第一版另写了一份 pytest 版本，和 `run.py` 有 ~500 行重复断言。
两份"同一批断言"必然漂移 —— 改了一边忘了另一边，测试就从"保证"变成"安慰"。
现在断言的**唯一出处是 `run.py`**（本机没装 pytest，所以它必须能独立跑），
这里只做适配：把 `run.py` 的用例逐个变成 pytest 用例，谁也不会漏。

`run.py` 的用例名是中文 —— pytest 完全支持（Python 3 标识符允许中文），
跑出来报告里直接是可读的中文用例名。

**本机没装 pytest**，而 `server/tests/` 下同时有 `run.py`（可独立跑）和本文件。
如果本文件直接 `import pytest`，没装 pytest 时 pytest 连收集都做不了
（ImportError 会把整个测试目录弄红）。所以下面有个极小的 shim：
没 pytest 时让模块能安全导入、只注册 0 条用例，而不是炸掉。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import pytest
    _HAVE_PYTEST = True
except ImportError:                      # 本机就是这种情况
    _HAVE_PYTEST = False

    class _Mark:
        @staticmethod
        def parametrize(*_a, **_kw):
            def deco(fn):
                return fn
            return deco

    class _PytestShim:
        mark = _Mark()

    pytest = _PytestShim()               # type: ignore[assignment]

# 直接 import run 模块会执行它的模块级加载（读数据 + 建 TestClient + 读 fixtures），
# 这是故意的：数据读不到就该在这里炸，而不是等某个用例跑到一半。
import run as contract_cases          # noqa: E402


if _HAVE_PYTEST:
    @pytest.mark.parametrize(
        "case_fn", contract_cases.CASES,
        ids=[c.__name__ for c in contract_cases.CASES])
    def test_契约用例(case_fn):
        case_fn()

    def test_用例数量不为零():
        """防止 import 路径出问题导致"0 passed"被当成成功。"""
        assert len(contract_cases.CASES) >= 30, len(contract_cases.CASES)

else:
    # 没有 pytest 时也提供等价入口：`python server/tests/test_contract.py` 直接跑全部用例。
    def test_契约用例():                 # noqa: D103
        return contract_cases.main()

    def test_用例数量不为零():
        assert len(contract_cases.CASES) >= 30, len(contract_cases.CASES)

    if __name__ == "__main__":
        sys.exit(contract_cases.main())

# -*- coding: utf-8 -*-
"""按需重载：数据文件被重写之后，接口不能还返回旧数据。

## 为什么需要这个

C 在走查里报的（`server/store.py:87`）：`NoticeStore` 是**启动时读一次**进内存的，
之后再也不看磁盘。后果是 **A 每重跑一次管道，`/api/notices` 就还在返回旧数据** ——
而且看起来完全正常（HTTP 200、形状对、就是内容旧的），
属于"最难发现"的那类 bug：**它不说谎，它只是过时。**

演示时这个问题会被放大：台上刚跑完 `run_daily.py`，切回浏览器还是老数据。

## 做法

**只 stat，不读字节。** 比对 `(st_mtime_ns, st_size)`，变了才真的重新加载。
所以每次请求的代价是一次 `os.stat`（微秒级），而不是读几十上百 KB 的文件。

这比 `/api/changes` 那边更进一步：那边也是 mtime+size，但这里因为数据量更大
（`notices.jsonl` 有 860 KB）所以更要紧。

## 为什么不做"每次请求都重读"

读 860 KB + JSON 解析 78 行，每次请求都做一遍，是拿 CPU 换一个**已经用 stat 拿到了的
同样信息**。`stat` 就够判断"要不要重读"，没必要每次都重读。

## 已知边界

`(mtime, size)` 不是内容哈希：如果有人在**同一纳秒**内改写文件、且**字节数完全不变**，
这里会漏掉。实际不可能 —— 抓取一轮的输出不可能和上一轮一模一样还刚好撞上 mtime。
真要绝对可靠得上内容哈希，代价是每次请求读全文件，不值。
"""
from __future__ import annotations

import os
import threading


class LiveStore:
    """包一层"按需重载"。

    用法：`LiveStore(path, lambda: NoticeStore.load(path))`。
    之后 `live.records` / `live.get(...)` 都直接转发给当前这份实例，
    读取时如果发现文件变了就**先换掉再转发** —— 调用方完全不用知道这件事。
    """

    def __init__(self, path, loader, initial=None):
        self.path = path
        self._loader = loader
        self._lock = threading.Lock()
        # `initial` 允许调用方传入已经加载好的实例，省掉一次重复加载
        self._inner = initial if initial is not None else loader()
        self._stamp = self._file_stamp()
        self.reload_count = 0          # 测试和 /api/health 用得上

    def _file_stamp(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def reload_if_stale(self):
        """文件变了就换一份新的。返回是否真的重载了。"""
        stamp = self._file_stamp()
        if stamp == self._stamp and self._inner is not None:
            return False
        with self._lock:
            # 双重检查：拿锁期间可能已经有别的线程换过了
            stamp = self._file_stamp()
            if stamp == self._stamp and self._inner is not None:
                return False
            self._inner = self._loader()
            self._stamp = stamp
            self.reload_count += 1
            return True

    # ---- 转发给当前实例 ----

    @property
    def inner(self):
        self.reload_if_stale()
        return self._inner

    def __getattr__(self, name):
        # 只有 `inner` 之类没在实例字典里找到的才会走到这
        return getattr(self.inner, name)

    def __bool__(self):
        # 有 `__getattr__` 的类默认 truthy 就够；显式给一个免得被误判成空
        return True

    def __repr__(self):
        return f"<LiveStore {os.path.basename(self.path)} reloads={self.reload_count}>"


def as_live(obj, path, loader):
    """`create_app` 用：传进来的是实例就原样用，是工厂就包成 LiveStore。

    这样测试可以继续注入固定实例（不重载、结果可预测），
    生产路径则自动获得"文件改了就跟上"的行为。

    **`path` 的取值有讲究**：优先用传进来的那个，但如果传进来的实例/工厂
    指向的是**另一个文件**（测试就爱这么干：注入一份指向临时文件的加载器），
    就必须盯那个文件。做法是——包装前先问一次加载器"你的 `path` 是什么"，
    问到就用它覆盖。

    踩过的坑：一开始 `create_app` 直接写死 `as_live(store, config.NOTICES_PATH, ...)`，
    于是测试注入的临时文件被忽略、LiveStore 一直盯着工作区里那份**没变**的真实文件，
    `reload_count` 永远是 0 —— 表面上"重载没生效"，实际上是**盯错了文件**。
    """
    if obj is not None and not (callable(obj) and not hasattr(obj, "records")):
        return obj                       # 已经是实例：原样用，不重载

    factory = obj if callable(obj) else loader
    if obj is None:
        factory = loader

    # 先实例化一次，拿到它自己认为的数据路径
    first = factory()
    real_path = getattr(first, "path", None) or path
    return LiveStore(real_path, factory, initial=first)

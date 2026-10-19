# -*- coding: utf-8 -*-
"""演示前清掉巡检/测试留下的用户（只清指定的那些，**不是重置整个站**）。

## 为什么要有这个脚本

QA 按 `status/QA-README.md` 巡检时会调 `PUT /api/profile`，于是
`server/var/state.json` 里留下 `u_qa` / `u_qa_std` / `u_qa_std2`。
上台前如果不处理，用户下拉框/演示数据里就会冒出几个"巡检用"的账号。

**为什么不直接 `del server\\var\\state.json`**：那会把 `u_demo`、
`u_num_demo` 这些演示人设的画像、订阅、已推记录一起清掉 ——
"清掉三个测试用户"和"重置整个站"是两件事，别用大炮打蚊子。

## 默认是干跑

不带 `--apply` 只报告会删什么（用户/画像/关注/推送记录各几条），
一个字节都不写。确认无误再加 `--apply`。

    D:\\python.3.10.11\\python.exe server\\tests\\clean_demo_state.py
    D:\\python.3.10.11\\python.exe server\\tests\\clean_demo_state.py --apply

## 三条安全措施

1. **先备份**：写到 `state.json.bak-<时间戳>`，删错了还能捞回来；
2. **服务在跑就警告**：8000 那个进程内存里还留着旧状态，之后一次保存
   会把清理结果覆盖回去 —— 先停服务再清；
3. **不动** `orders`（订阅/付款记录）。钱相关的记录不做静默删除，
   要清得人工确认 —— 而这个脚本的定位是"清测试账号"，不是"抹账"。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import socket
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from server.state import DEMO_USER_IDS, StateStore  # noqa: E402

DEFAULT_STATE = os.path.join(ROOT, "server", "var", "state.json")
DEFAULT_PREFIXES = ("u_qa",)


def _port_busy(port=8000):
    """8000 上有没有人在听。有的话提醒先停服务（否则内存里的旧状态会覆盖清理）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _match_any(uid, ids, prefixes):
    return uid in ids or any(uid.startswith(p) for p in prefixes)


def collect(state, ids, prefixes):
    """返回要删的用户 id 集合 + 各表的命中条数（只读，不改 state）。"""
    users = state.get("users") or {}
    profiles = state.get("profiles") or {}
    watches = state.get("watches") or []
    pushes = state.get("pushes") or []

    hit_users = {u for u in users if _match_any(u, ids, prefixes)}
    hit_profiles = {u for u in profiles if _match_any(u, ids, prefixes)}
    hit_watches = [w for w in watches if _match_any(w.get("user_id") or "", ids, prefixes)]
    hit_pushes = [p for p in pushes if _match_any(p.get("user_id") or "", ids, prefixes)]
    targets = hit_users | hit_profiles | {w.get("user_id") for w in hit_watches} \
        | {p.get("user_id") for p in hit_pushes}
    targets.discard(None)
    return {
        "users": sorted(hit_users),
        "profiles": sorted(hit_profiles),
        "watches": len(hit_watches),
        "pushes": len(hit_pushes),
        "targets": sorted(targets),
    }


def apply_clean(path, report):
    """真正删。先备份，再逐个表删，最后走 `StateStore.save()`（原子替换）。"""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = f"{path}.bak-{stamp}"
    shutil.copy2(path, backup)
    store = StateStore.load(path)
    st = store.state
    targets = set(report["targets"])
    for u in targets:
        st.get("users", {}).pop(u, None)
        st.get("profiles", {}).pop(u, None)
    st["watches"] = [w for w in st.get("watches") or []
                     if (w.get("user_id") or "") not in targets]
    st["pushes"] = [p for p in st.get("pushes") or []
                    if (p.get("user_id") or "") not in targets]
    store.save()
    return backup


def main():
    ap = argparse.ArgumentParser(description="清掉巡检/测试用户（默认干跑）")
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只报告）")
    ap.add_argument("--state", default=DEFAULT_STATE, help="状态文件路径")
    ap.add_argument("--user", action="append", default=[],
                    help="额外指定要删的用户 id（可重复）")
    ap.add_argument("--prefix", action="append", default=[],
                    help="额外指定用户 id 前缀（可重复；默认已有 u_qa）")
    ap.add_argument("--keep-demo-only", action="store_true",
                    help="只保留演示用户（u_demo / u_num_demo），其余全清 —— "
                         "交接前用这个（B26）")
    args = ap.parse_args()

    path = os.path.abspath(args.state)
    if not os.path.exists(path):
        print("没有 %s —— 本来就没什么可清的。" % path)
        print("（服务第一次启动时会自动生成演示状态，见 server/state.py 的 load_or_seed）")
        return 0

    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    if args.keep_demo_only:
        # 交接模式：除了演示身份，其余一律当测试残留清掉
        demo = set(DEMO_USER_IDS)
        all_ids = set((state.get("users") or {}).keys())
        all_ids |= set((state.get("profiles") or {}).keys())
        all_ids |= {w.get("user_id") or "" for w in state.get("watches") or []}
        all_ids |= {p.get("user_id") or "" for p in state.get("pushes") or []}
        all_ids -= {""}
        report = collect(state, all_ids - demo, ())
    else:
        prefixes = tuple(DEFAULT_PREFIXES) + tuple(args.prefix)
        report = collect(state, set(args.user), prefixes)

    print("状态文件：%s（%d 字节）" % (path, os.path.getsize(path)))
    if args.keep_demo_only:
        print("匹配规则：除演示用户（%s）之外全部清掉" % " / ".join(DEMO_USER_IDS))
    else:
        print("匹配规则：id ∈ %s 或 前缀 ∈ %s"
              % (sorted(set(args.user)) or "-", list(prefixes)))
    print("命中：用户 %d 个 / 画像 %d 份 / 关注 %d 条 / 推送记录 %d 条"
          % (len(report["users"]), len(report["profiles"]),
             report["watches"], report["pushes"]))
    for u in report["targets"]:
        print("  - %s%s%s" % (u,
                             "（有画像）" if u in report["profiles"] else "",
                             "（有账号）" if u in report["users"] else ""))
    if not report["targets"]:
        print("没有要清的，收工。")
        return 0

    if not args.apply:
        print("\n这是**干跑**，什么都没改。确认无误后加 --apply：")
        print("  %s %s --apply" % (os.path.basename(sys.executable), os.path.relpath(__file__, os.getcwd())))
        return 0

    if _port_busy():
        print("\n⚠️ 8000 端口有人在听：那个进程内存里还是旧状态，"
              "下一次保存会把清理结果覆盖回去。**先停服务再清**。")
    backup = apply_clean(path, report)
    print("\n已清掉 %d 个用户；原文件备份在：%s" % (len(report["targets"]), backup))
    print("注意：`orders`（订阅记录）没动 —— 钱相关的记录不做静默删除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

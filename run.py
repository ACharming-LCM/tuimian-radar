# -*- coding: utf-8 -*-
"""一键启动（演示入口）。`run.bat` 调它，也可以直接跑：

    D:\\python.3.10.11\\python.exe run.py
    D:\\python.3.10.11\\python.exe run.py --port 8080
    D:\\python.3.10.11\\python.exe run.py --check     # 只做体检，不起服务

## 为什么不把逻辑写在 `run.bat` 里

`cmd.exe` **按控制台代码页解析 .bat 文件**（中文 Windows 上是 GBK）。
把中文写进 .bat 会被解析成乱码命令，脚本直接崩 —— 这个坑我踩了：
第一版 `run.bat` 存成 UTF-8，里面的中文注释和 `echo` 全变成
`'默认' 不是内部或外部命令` 这种碎片。`chcp 65001` 也救不了，
因为**解析发生在 chcp 生效之前**。

所以：`.bat` 只留 ASCII 并负责选 Python、传参；中文全在这里打（Python 自己管编码）。

## 它做三件事

1. **先体检再起服务**：查依赖、查端口、查数据文件。
   比起来之后报一堆看不懂的错，不如起来之前就说清。
2. 起 uvicorn，**并把 `web/` 同源挂上**（`server/app.py` 里做的），
   所以 C 的页面和接口在同一个地址下，没有跨域。
3. 退出时给一个明确的原因，而不是让窗口一闪而过。
"""
from __future__ import annotations

import argparse
import io
import os
import socket
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Windows 控制台默认可能不是 UTF-8，中文会乱。这里强制一下。
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

LINE = "=" * 66


def say(msg=""):
    print(msg, flush=True)


def check_deps():
    missing = []
    for mod in ("fastapi", "uvicorn"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return missing


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def check_data():
    """数据体检。**缺数据不是致命错误** —— 服务设计成"读不到也能起来"，
    只是会返回空列表。所以这里只提醒，不拦。

    路径走 `server.config` 的解析：有 `data/out` 用 `data/out`，
    没有就用仓库自带的脱敏样例 `data/sample/out`（公开仓库不带原始数据）。"""
    from server import config
    checks = [
        (config.NOTICES_PATH, "公告", True),
        (config.FACULTY_PATH, "导师", False),
        (config.CHANGES_PATH, "变更报告", False),
        (config.HISTORY_INDEX_PATH, "更新历史", False),
    ]
    rows = []
    for path, name, required in checks:
        rel = os.path.relpath(path, ROOT)
        if not os.path.exists(path):
            rows.append((name, rel, "缺失" + ("  ← 公告缺失会让列表为空" if required else "")))
            continue
        if rel.endswith(".jsonl"):
            with open(path, encoding="utf-8") as f:
                n = sum(1 for ln in f if ln.strip())
            rows.append((name, rel, f"{n} 条"))
        else:
            rows.append((name, rel, f"{os.path.getsize(path) / 1024:.1f} KB"))
    return rows


def main():
    ap = argparse.ArgumentParser(add_help=True)
    # 云平台（魔搭创空间 / Docker）通过环境变量指定端口和监听地址
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--reload", action="store_true", help="开发模式：改文件自动重启")
    ap.add_argument("--check", action="store_true", help="只做体检，不起服务")
    args = ap.parse_args()

    say()
    say(LINE)
    say("  推免雷达 —— 服务端")
    say(LINE)
    say(f"  项目根 : {ROOT}")
    say(f"  Python : {sys.executable}")
    say(f"  地址   : http://{args.host}:{args.port}")
    say()

    # ---- 体检 ----
    missing = check_deps()
    if missing:
        say(f"  [错误] 缺依赖：{', '.join(missing)}")
        say(f"         装一下：\"{sys.executable}\" -m pip install fastapi uvicorn")
        say()
        return 2
    say("  [ok] 依赖 fastapi / uvicorn 都在")

    if port_in_use(args.port):
        say(f"  [警告] 端口 {args.port} 已被占用 —— 换个端口：run.bat 8080")
        say(f"         想看谁占的：netstat -ano | findstr :{args.port}")
        say("         （下面还是会试着起，起不来就是这个原因）")
    else:
        say(f"  [ok] 端口 {args.port} 空闲")

    say("  [ok] 数据：")
    for name, rel, info in check_data():
        say(f"        {name:6s} {info:12s} {rel}")

    web_dir = os.path.join(ROOT, "web")
    if os.path.isdir(web_dir):
        say(f"  [ok] web/ 存在，会同源挂载（C 的页面不用碰 CORS）")
    else:
        say("  [警告] 没找到 web/ 目录 —— 接口能跑，但首页打不开")
    say()

    if args.check:
        say("  只体检，不起服务（--check）")
        say(LINE)
        say()
        return 0

    # ---- 首次运行：自动建一份演示状态（B26，2026-10-19）----
    #
    # `server/var/` 在 .gitignore 里，新克隆的仓库没有 state.json。
    # 不建的话，第一次跑门禁/演示会看到"没有 u_demo"，甚至直接 FileNotFound。
    from server import config as _config
    from server.state import StateStore as _StateStore
    _had_state = os.path.exists(_config.STATE_PATH)
    _StateStore.load_or_seed(_config.STATE_PATH)
    if _had_state:
        say(f"  [ok] 状态：{_config.STATE_PATH}（已存在，未改动）")
    else:
        say("  [ok] 首次运行：已生成演示状态（u_demo 免费 / u_num_demo 完整画像）")
        say(f"        {_config.STATE_PATH}")


    if _config.ADMIN_TOKEN_FROM_ENV:
        say("  [ok] 管理口令：来自环境变量 TUIMIAN_ADMIN_TOKEN")
    else:
        # 只打印在本机控制台，不经任何接口返回
        say(f"  [注意] 未设 TUIMIAN_ADMIN_TOKEN，本次随机管理口令：{_config.ADMIN_TOKEN}")
        say("         部署到公网前请设置环境变量；每次重启都会换一个")

    say(LINE)
    say("  起来了。演示前 30 秒自检：")
    say(f"    curl http://{args.host}:{args.port}/api/health")
    say(f"    浏览器打开 http://{args.host}:{args.port}/        （C 的首页）")
    say(f"                 http://{args.host}:{args.port}/docs    （接口文档，可点着试）")
    say()
    say("  停止：这个窗口按 Ctrl+C")
    say(LINE)
    say()

    import uvicorn
    try:
        uvicorn.run("server.app:app", host=args.host, port=args.port,
                    reload=args.reload, log_level="info")
    except KeyboardInterrupt:
        say()
        say("  已手动停止。")
        return 0
    except OSError as e:
        say()
        say(f"  [错误] 起不来：{e}")
        say(f"         最常见的原因是端口被占用 —— 换一个：run.bat 8080")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

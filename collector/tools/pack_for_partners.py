# -*- coding: utf-8 -*-
"""给远程伙伴打包。

设计要点（都是踩过坑才这么写的）：
  1. **用 git clone 而不是直接压目录** —— 包里带的是干净的提交历史，而且能顺手删掉
     指向本机 `D:\\tuimian-remote.git` 的 origin。否则伙伴那边 `git push` 会报一个
     他们完全看不懂的错（"Please make sure you have the correct access rights"）。
  2. **伙伴 2 的包不带 `data/raw`**（46 MB）。他只需要点开 tsv 里的 url 看公告，
     不需要 315 个页面缓存。伙伴 1 做解析器则**必须**要，那是他能离线开发的前提。
  3. 压缩包写在仓库**外面** —— 它是传输产物，不该进版本库。
  4. 用 Python 不用 PowerShell：仓库里所有工具都是 Python，
     而且 Windows 默认执行策略会拒绝运行未签名的 .ps1（实测踩过）。

用法（在 collector 目录下）：
    python tools/pack_for_partners.py                # 两份都打
    python tools/pack_for_partners.py --for partner1 # 只打伙伴 1
"""
import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
OUT_ROOT = os.path.dirname(REPO)          # 仓库外面
JUNK = ("_scratch", os.path.join("server", "var"), ".venv")

PARTNERS = {
    "partner1": {
        "with_raw": True,
        "note": "你的任务：把导师解析器从「一个院系」扩成「多校可用」"
                "（`docs/PARTNER-1-导师泛化.md`）",
    },
    "partner2": {
        "with_raw": False,
        "note": "你的任务：给数据质量做一份人工基准（`docs/PARTNER-2-数据质量基准.md`）",
    },
}


def git(*args, cwd=REPO, check=True):
    """跑一个 git 命令。

    坑（已踩）：原来用 `capture_output=True` 而不打 stderr，clone 失败时
    只看到一个 `CalledProcessError: ... exit status 128`，**真实原因（比如
    "destination path already exists"）被吞掉了**，只能手工重跑才知道。
    工具自己的可诊断性也是可诊断性 —— 现在失败时把 git 的 stderr 一起打出来。
    """
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（exit {r.returncode}）\n"
            f"  cwd : {cwd}\n"
            f"  err : {(r.stderr or '').strip()[:600]}")
    return r.stdout.strip()


def clone_at_snapshot(stage, sha):
    # 用**每次唯一的**临时目录，避免上次失败的残留把这次挡住。
    # 坑（已踩）：固定目录名 + ignore_errors=True 的 rmtree，删不掉时静默继续，
    # 然后 clone 报一个和现场毫无关系的 exit 128。
    if os.path.exists(stage):
        shutil.rmtree(stage, ignore_errors=True)
    if os.path.exists(stage):
        raise RuntimeError(f"临时目录删不掉，可能被占用：{stage}\n"
                           f"手工删掉它再跑一次。")
    # 克隆的是已提交的 HEAD；工作区未提交的东西不会进包（这是刻意的）
    git("clone", "--quiet", REPO, stage)
    git("remote", "remove", "origin", cwd=stage)
    git("checkout", "--quiet", sha, cwd=stage)


def write_manifest(stage, name, sha, subject, note, with_raw):
    size = sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(stage) for f in fs)
    raw_note = ("✅ 包含（约 46 MB，可**离线**对着同一批真实页面开发解析器，"
                "不用重新抓、没有网络差异）" if with_raw
                else "❌ 不含（你只需要点开 tsv 里的 url 看公告）")
    md = f"""# 交付清单 — {name}

- **快照点**：`{sha}` — {subject}
- **打包时间**：{datetime.now():%Y-%m-%d %H:%M:%S}
- **未压缩体积**：{size / 1024 / 1024:.1f} MB
- **`data/out/` 的说明**：这个目录用的是**打包那一刻的最新版**，可能比快照点提交的内容新
  （里面有刚生成还没提交的产物，比如标注模板）。**文件本身没有版本号，用之前先看生成时间。**

## {note}

## data/raw（网页与附件缓存）

{raw_note}

## 包里刻意没有的东西

| 没放 | 为什么 |
|---|---|
| `_scratch/` | 调试垃圾（一次性脚本、日志、对照基准），对任务无意义 |
| `server/var/` | 后端运行时状态（别人机器上的用户数据） |
| `__pycache__/` | 编译缓存 |
| `.venv/` | 虚拟环境 |

## 交回来之前

1. 先读 `docs/PARTNER-SHARED.md`（快照规矩、边界、交付方式）
2. 再读你自己那份任务书（`docs/PARTNER-1-*.md` 或 `docs/PARTNER-2-*.md`）
3. 改了代码就先跑测试，红了不许提交
4. 用 `git bundle create partner-X.bundle main` 或
   `git format-patch {sha} -o patches/` 交回来

**注意：包里的 git 没有配远程库**（本机那个路径对你没有意义）。
你自己本地提交没问题，交回来按上面的方式。
"""
    with open(os.path.join(stage, "MANIFEST.md"), "w", encoding="utf-8") as f:
        f.write(md)


def build(name, cfg, sha, subject):
    print(f"\n=== 打包 {name} ===")
    stage = os.path.join(os.environ.get("TEMP", "/tmp"),
                         f"tuimian-pkg-{name}-{os.getpid()}")
    clone_at_snapshot(stage, sha)

    # 覆盖成**最新的** data/out —— 可能有刚生成还没提交的产物
    live_out = os.path.join(REPO, "data", "out")
    if os.path.isdir(live_out):
        dst = os.path.join(stage, "data", "out")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(live_out, dst)

    if cfg["with_raw"]:
        src = os.path.join(REPO, "data", "raw")
        dst = os.path.join(stage, "data", "raw")
        if os.path.isdir(src):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
            n_html = len([f for f in os.listdir(src) if f.endswith(".html")])
            print(f"  data/raw: {n_html} 个网页 + 附件已带上")

    for junk in JUNK:
        p = os.path.join(stage, junk)
        if os.path.exists(p):
            shutil.rmtree(p, ignore_errors=True)
    for root, dirs, _ in os.walk(stage):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)

    write_manifest(stage, name, sha, subject, cfg["note"], cfg["with_raw"])

    zip_base = os.path.join(OUT_ROOT, f"tuimian-{name}-{sha}")
    if os.path.exists(zip_base + ".zip"):
        os.remove(zip_base + ".zip")
    shutil.make_archive(zip_base, "zip", stage)
    shutil.rmtree(stage, ignore_errors=True)

    mb = os.path.getsize(zip_base + ".zip") / 1024 / 1024
    print(f"  -> {zip_base}.zip  ({mb:.1f} MB)")
    return zip_base + ".zip"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--for", dest="which", default="all",
                    choices=["all", "partner1", "partner2"])
    a = ap.parse_args()

    sha = git("rev-parse", "--short", "HEAD")
    subject = git("log", "-1", "--format=%s")
    print("=== 快照点确认 ===")
    print(f"  HEAD   : {sha}  {subject}")
    dirty = git("status", "--short")
    if dirty:
        print("  ⚠️ 工作区有未提交内容 —— 下面这些**不会**进包（走的是 git clone）：")
        for line in dirty.splitlines():
            print(f"      {line}")
        print("      （data/out 例外：那个目录会被最新版覆盖进去，")
        print("        因为标注模板这类产物刚生成就得出门）")
    else:
        print("  工作区 : 干净")

    names = list(PARTNERS) if a.which == "all" else [a.which]
    made = [build(n, PARTNERS[n], sha, subject) for n in names]

    print("\n=== 完成 ===")
    for p in made:
        print(f"  {p}")
    print(f"  快照点 {sha} —— 记住：**发这个包等于承诺「之后不再改契约」**。")
    print("  要更新就补发新包或 git format-patch 补丁，别让对方拿旧包硬撑。")


if __name__ == "__main__":
    sys.exit(main())

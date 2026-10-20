# -*- coding: utf-8 -*-
"""从当前工作区生成同源的「交付包」和「部署包」。

    python tools/make_release.py

两个包共用同一份代码和同一份 data/sample/out，避免出现
「交付包是 2646、部署包是 2561」这种交叉状态。

产出（项目根目录 dist/）：
    tuimian-delivery-<date>.zip   给队友/评委：代码 + 数据 + BP + demo + docs
    tuimian-deploy-<date>.zip     给魔搭/容器：最小运行集（含 run.bat 与 README）

判据：打包前检查 data/sample/out/notices.jsonl 条数 > 0，
并在输出里打印两个包的条数、文件数、SHA256。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
SAMPLE = os.path.join(ROOT, "data", "sample", "out", "notices.jsonl")

EXCLUDE_DIRS = {".git", "dist", "data/out", "data/raw", "deploy_data",
                "_work", "server/var", "__pycache__", "_scratch"}
EXCLUDE_FILES = {".ms_token"}
EXCLUDE_SUFFIX = {".pyc"}

DEPLOY_ITEMS = ["Dockerfile", "requirements.txt", "requirements-dev.txt",
                "run.py", "run.bat", "README.md", "DEPLOY.md",
                "server", "collector", "contracts", "web", "data/sample/out"]


def _rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def _skip(rel):
    if rel in EXCLUDE_FILES:
        return True
    if os.path.splitext(rel)[1] in EXCLUDE_SUFFIX:
        return True
    return any(rel == d or rel.startswith(d + "/") for d in EXCLUDE_DIRS)


def _walk(base, rel_base=""):
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        rel = (rel_base + "/" + name).lstrip("/") if rel_base else name
        if _skip(rel):
            continue
        if os.path.isdir(p):
            yield from _walk(p, rel)
        else:
            yield p, rel


def _zip(stage, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in _walk(stage):
            z.write(p, arcname="tuimian-radar/" + rel)
    h = hashlib.sha256(open(out, "rb").read()).hexdigest()
    return h


def _copy(items, stage):
    for item in items:
        src = os.path.join(ROOT, item.replace("/", os.sep))
        if not os.path.exists(src):
            raise SystemExit(f"缺少 {item}，拒绝打包")
        dst = os.path.join(stage, item.replace("/", os.sep))
        if os.path.isdir(src):
            shutil.copytree(src, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)


def main():
    if not os.path.exists(SAMPLE):
        raise SystemExit("缺少 data/sample/out/notices.jsonl")
    n = sum(1 for ln in open(SAMPLE, encoding="utf-8") if ln.strip())
    stamp = date.today().isoformat()
    os.makedirs(DIST, exist_ok=True)

    tmp = tempfile.mkdtemp(prefix="tuimian_release_")
    try:
        # 交付包：整个工作区（排除运行产物）
        d_stage = os.path.join(tmp, "delivery", "tuimian-radar")
        os.makedirs(d_stage)
        for p, rel in _walk(ROOT):
            dst = os.path.join(d_stage, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
        d_out = os.path.join(DIST, f"tuimian-delivery-{stamp}.zip")
        d_hash = _zip(d_stage, d_out)

        # 部署包：最小运行集（必须含 run.bat，门禁要它）
        s_stage = os.path.join(tmp, "deploy", "tuimian-radar")
        os.makedirs(s_stage)
        _copy(DEPLOY_ITEMS, s_stage)
        s_out = os.path.join(DIST, f"tuimian-deploy-{stamp}.zip")
        s_hash = _zip(s_stage, s_out)

        def count(z):
            with zipfile.ZipFile(z) as zf:
                return len(zf.namelist())
        print(json.dumps({
            "notices": n,
            "delivery": {"path": d_out, "files": count(d_out), "sha256": d_hash},
            "deploy": {"path": s_out, "files": count(s_out), "sha256": s_hash},
        }, ensure_ascii=False, indent=1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

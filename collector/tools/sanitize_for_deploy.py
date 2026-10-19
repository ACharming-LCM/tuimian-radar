# -*- coding: utf-8 -*-
"""给公网部署出一份**脱敏数据**（不改原始 data/out）。

    python collector/tools/sanitize_for_deploy.py --src data/out --dst deploy_data/out

为什么要有这一步
----------------
抓下来的公告正文里有学校公示的**考生名单**（姓名 / 学号 / 考生编号，个别还有证件号格式的数字），
导师数据里有手机号。这些在学校官网上是公开的，但一般过一段时间就撤下；
我们把它们汇总后放到公网上，就成了长期可检索的个人信息。所以部署前：

1. **名单类公告**（标题像"拟录取名单 / 公示 / 递补 / 答辩 / 评审结果"，且正文里有表格或"姓名"）
   → 正文整段替换成一句说明，让用户点原文看。结构化字段（截止日、三态、类型）保持不变。
2. **其他公告**里若有"姓名 / 考生"表头的表格 → 只删表格行，保留文字说明。
3. 所有正文、联系方式里的**手机号**、**证件号格式的 18 位数字**打码。
4. 导师 `phone` 置空（`email` 是官网公开的教职工作邮箱，套磁信功能要用，保留）。

只处理字符串，不重算任何派生字段 —— 排序和三态在脱敏前后一致。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

# 服务端会读的产出文件（见 server/config.py），其余报告类文件不部署
NEEDED = [
    "notices.jsonl", "faculty.jsonl", "events.jsonl", "units.json",
    "changes.json", "freshness.json", "stability.jsonl",
]
NEEDED_DIRS = ["history", "runs"]

LIST_TITLE = re.compile(r"名单|公示|拟录取|录取结果|递补|答辩|送审|评审结果|名册|获奖|入选|考生")
TABLE_PEOPLE_HEADER = re.compile(r"姓名|考生")
MOBILE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
IDCARD = re.compile(
    r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])")

# 自检时跳过的机器字段（哈希 / id / 链接）
MACHINE_KEYS = {"id", "notice_id", "event_id", "content_hash", "url", "homepage",
                "source_url", "replaced_by", "duplicate_of"}

REDACTED_BODY = ("【正文含公示名单（考生姓名等个人信息），部署版已隐去。"
                 "公告的截止日、类型等信息不受影响；完整内容请点「看原文」到学校官网查看。】")
# 前缀和服务端门禁共用一份（server/config.py: REDACTED_BODY_PREFIX），这里只做一致性检查
assert REDACTED_BODY.startswith("【正文含公示名单")

stats = {"list_bodies": 0, "table_rows_removed": 0, "mobile": 0, "idcard": 0,
         "faculty_phone": 0}


def _mask(s: str) -> str:
    s, n = IDCARD.subn("[证件号已隐去]", s)
    stats["idcard"] += n
    s, n = MOBILE.subn(r"\1****\2", s)
    stats["mobile"] += n
    return s


def _has_people_table(body: str) -> bool:
    return any("|" in ln and TABLE_PEOPLE_HEADER.search(ln) for ln in body.splitlines())


def sanitize_body(title: str, body: str) -> str:
    if not body:
        return body
    if LIST_TITLE.search(title or "") and ("|" in body or "姓名" in body):
        stats["list_bodies"] += 1
        return REDACTED_BODY
    if _has_people_table(body):
        kept = []
        for ln in body.splitlines():
            if "|" in ln:
                stats["table_rows_removed"] += 1
                continue
            kept.append(ln)
        body = "\n".join(kept)
        # 删完表格只剩几句话：与其留个空壳，不如整段说明 + 引导看原文
        if len(body.strip()) < 300:
            stats["list_bodies"] += 1
            return REDACTED_BODY
    return _mask(body)


def walk(obj, *, faculty=False):
    """递归处理：任何带 body_text 的对象都脱敏（history/*.json 里也嵌着公告记录）。"""
    if isinstance(obj, dict):
        if isinstance(obj.get("body_text"), str):
            obj["body_text"] = sanitize_body(obj.get("title") or "", obj["body_text"])
        c = obj.get("contact")
        if isinstance(c, dict):
            for k in ("phone", "email"):
                # email 也要过：有的联系邮箱本地部分就是手机号（1xxxxxxxxxx@…）
                if isinstance(c.get(k), str):
                    c[k] = _mask(c[k])
        if faculty and obj.get("phone"):
            obj["phone"] = None
            stats["faculty_phone"] += 1
        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk(v)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, faculty=faculty)
    return obj


def _jsonl(src, dst, faculty=False):
    with open(src, encoding="utf-8") as fi, open(dst + ".tmp", "w", encoding="utf-8", newline="\n") as fo:
        for ln in fi:
            if not ln.strip():
                continue
            fo.write(json.dumps(walk(json.loads(ln), faculty=faculty), ensure_ascii=False) + "\n")
    os.replace(dst + ".tmp", dst)


def _json(src, dst):
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    with open(dst + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        json.dump(walk(data), f, ensure_ascii=False, indent=1)
    os.replace(dst + ".tmp", dst)


def _copy(src, dst):
    if src.endswith(".jsonl"):
        _jsonl(src, dst, faculty=os.path.basename(src) == "faculty.jsonl")
    elif src.endswith(".json"):
        _json(src, dst)
    else:
        shutil.copyfile(src, dst)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default="data/out")
    ap.add_argument("--dst", default="data/sample/out")
    a = ap.parse_args(argv)

    src, dst = os.path.abspath(a.src), os.path.abspath(a.dst)
    if src == dst:
        sys.exit("--dst 不能和 --src 相同：这个脚本不改原始数据")
    if not os.path.exists(os.path.join(src, "notices.jsonl")):
        sys.exit(f"找不到 {src}/notices.jsonl")
    os.makedirs(dst, exist_ok=True)

    for name in NEEDED:
        p = os.path.join(src, name)
        if os.path.exists(p):
            _copy(p, os.path.join(dst, name))
    for d in NEEDED_DIRS:
        sd = os.path.join(src, d)
        if not os.path.isdir(sd):
            continue
        os.makedirs(os.path.join(dst, d), exist_ok=True)
        for name in os.listdir(sd):
            _copy(os.path.join(sd, name), os.path.join(dst, d, name))

    # 自检：脱敏后人能读的文本里不应再有证件号格式的数字和完整手机号。
    # 哈希 / id / URL 是机器字符串，里面碰巧有 11 位数字不算（实测全是这类误报）。
    left = 0

    def _scan(o, key=""):
        nonlocal left
        if isinstance(o, dict):
            for k, v in o.items():
                _scan(v, k)
        elif isinstance(o, list):
            for v in o:
                _scan(v, key)
        elif isinstance(o, str) and key not in MACHINE_KEYS:
            left += len(IDCARD.findall(o)) + len(MOBILE.findall(o))

    for root, _, files in os.walk(dst):
        for name in files:
            p = os.path.join(root, name)
            with open(p, encoding="utf-8") as f:
                if name.endswith(".jsonl"):
                    for ln in f:
                        if ln.strip():
                            _scan(json.loads(ln))
                elif name.endswith(".json"):
                    _scan(json.load(f))
                else:
                    t = f.read()
                    left += len(IDCARD.findall(t)) + len(MOBILE.findall(t))
    print(json.dumps({"dst": dst, **stats, "left_after_check": left}, ensure_ascii=False, indent=1))
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
